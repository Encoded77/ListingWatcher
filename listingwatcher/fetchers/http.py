"""Polite HTTP client for sites without an API: browser TLS fingerprint, one call at a time per domain,
random delay between requests, robots.txt, exponential backoff on 403/429/503 without a retry storm."""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Optional
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from curl_cffi import requests as cffi_requests

log = logging.getLogger("listingwatcher.http")


class FetchError(Exception):
    pass


class BlockedError(FetchError):
    """Persistent 403/429/503: the source refuses us. Handle with a pause, not with retries."""


class PoliteClient:
    def __init__(self, min_delay: float = 6.0, max_delay: float = 11.0, respect_robots: bool = True,
                 impersonate: str = "chrome", timeout: float = 30.0, max_retries: int = 2,
                 backoff_base: float = 45.0, ua_token: str = "ListingWatcher",
                 accept_language: str = "fr-FR,fr;q=0.9,en;q=0.5"):
        self.min_delay, self.max_delay = min_delay, max_delay
        self.respect_robots = respect_robots
        self.timeout, self.max_retries, self.backoff_base = timeout, max_retries, backoff_base
        self.ua_token = ua_token
        self.headers = {"Accept-Language": accept_language}
        self.session = cffi_requests.Session(impersonate=impersonate)
        self._last: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._robots: dict[str, Optional[RobotFileParser]] = {}
        self._global = threading.Lock()
        self.request_count = 0

    # ------------------------------------------------------------------ internal
    def _lock(self, host: str) -> threading.Lock:
        with self._global:
            return self._locks.setdefault(host, threading.Lock())

    def _pace(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            wait = random.uniform(self.min_delay, self.max_delay) - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last[host] = time.monotonic()

    def _raw_get(self, url: str, headers: dict[str, str] | None = None):
        self.request_count += 1
        return self.session.get(url, headers={**self.headers, **(headers or {})}, timeout=self.timeout)

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        host = parts.netloc
        if host not in self._robots:
            rp: Optional[RobotFileParser] = None
            try:
                self._pace(host)
                r = self._raw_get(f"{parts.scheme}://{host}/robots.txt")
                if r.status_code == 200:
                    rp = RobotFileParser()
                    rp.parse(r.text.splitlines())
                else:
                    log.info("robots.txt %s → HTTP %s, assuming access allowed", host, r.status_code)
            except Exception as e:  # noqa: BLE001
                log.info("robots.txt %s unreadable (%s), assuming access allowed", host, e)
            self._robots[host] = rp
        rp = self._robots[host]
        return True if rp is None else rp.can_fetch(self.ua_token, url)

    # ------------------------------------------------------------------ API
    def get(self, url: str, headers: dict[str, str] | None = None):
        host = urlsplit(url).netloc
        with self._lock(host):
            if not self.allowed(url):
                raise FetchError(f"robots.txt interdit {url}")
            for attempt in range(self.max_retries + 1):
                self._pace(host)
                try:
                    r = self._raw_get(url, headers)
                except Exception as e:  # noqa: BLE001 - curl network errors
                    if attempt >= self.max_retries:
                        raise FetchError(f"réseau: {e}") from e
                    time.sleep(self.backoff_base * (2 ** attempt) * random.uniform(0.5, 1.0))
                    continue
                if r.status_code in (403, 429, 503):
                    if attempt >= self.max_retries:
                        raise BlockedError(f"HTTP {r.status_code} sur {url}")
                    delay = self.backoff_base * (2 ** attempt) + random.uniform(0, 10)
                    log.warning("HTTP %s on %s, retrying in %.0f s", r.status_code, host, delay)
                    time.sleep(delay)
                    continue
                if r.status_code >= 400:
                    raise FetchError(f"HTTP {r.status_code} sur {url}")
                return r
        raise FetchError(f"abandon {url}")  # pragma: no cover
