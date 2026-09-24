"""Service loop: scans at the configured slots (with jitter), daily digest, web interface,
never two scans in parallel (lock shared between the scheduler and the "Scanner maintenant" button).

Settings saved from the interface rebuild the `App` in place, under the same lock: right away if no
scan is running, otherwise when the current scan ends. The scheduler re-reads its slots on every
loop and wakes up when the config changes."""
from __future__ import annotations

import logging
import random
import signal
import threading
import time
from datetime import datetime, time as dtime, timedelta
from typing import Any, Callable, Optional

from .config import env
from .pipeline import App, run_all, run_digests
from .settings import SettingsManager
from .store import Store
from .web import WebApp, WebServer

log = logging.getLogger("listingwatcher.scheduler")


def _parse_hhmm(s: str) -> dtime:
    h, m = str(s).split(":")
    return dtime(int(h), int(m))


def next_scan(now: datetime, scan_times: list[str], jitter_minutes: float, rnd: random.Random | None = None) -> datetime:
    """Next slot strictly after `now`, shifted by a random ±jitter. Deterministic for a given slot
    (the jitter is seeded from day + slot time) so that a restart does not change the planned time."""
    slots = sorted(_parse_hhmm(s) for s in scan_times) or [dtime(8, 0)]
    for day in (0, 1):
        base_day = (now + timedelta(days=day)).date()
        for slot in slots:
            candidate = datetime.combine(base_day, slot, tzinfo=now.tzinfo)
            r = rnd or random.Random(f"{base_day}{slot}")
            candidate += timedelta(minutes=r.uniform(-jitter_minutes, jitter_minutes))
            if candidate > now:
                return candidate
    return now + timedelta(hours=6)  # pragma: no cover


def next_digest(now: datetime, digest_time: str, last_sent_date: Optional[str]) -> datetime:
    slot = _parse_hhmm(digest_time or "08:00")
    today = datetime.combine(now.date(), slot, tzinfo=now.tzinfo)
    if today > now and last_sent_date != now.date().isoformat():
        return today
    return today + timedelta(days=1)


class ScanRunner:
    """Runs the scans (all watches) under a lock; a manual scan started from the web runs in
    its own thread. Also holds the current `App`, hot-swapped by `reload`."""

    def __init__(self, app: App, rebuild: Optional[Callable[[dict[str, Any], App], App]] = None):
        self.app = app
        self.rebuild = rebuild
        self.lock = threading.Lock()
        self.generation = 0                         # incremented on every applied reload
        self._pending: Optional[dict[str, Any]] = None
        self._pending_lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.lock.locked()

    def run_blocking(self) -> None:
        with self.lock:
            try:
                run_all(self.app)
            finally:
                self._apply_pending()

    def trigger(self) -> bool:
        if not self.lock.acquire(blocking=False):
            return False

        def job():
            try:
                run_all(self.app)
            except Exception:  # noqa: BLE001
                log.exception("manual scan failed")
            finally:
                self._apply_pending()
                self.lock.release()

        threading.Thread(target=job, name="scan-manual", daemon=True).start()
        return True

    def reload(self, cfg: dict[str, Any]) -> str:
        """New validated config: "applied" right away, "pending" if a scan is running."""
        if self.rebuild is None:
            return "unsupported"
        with self._pending_lock:
            self._pending = cfg
        if not self.lock.acquire(blocking=False):
            log.info("settings: reload deferred until the current scan ends")
            return "pending"
        try:
            return self._apply_pending()
        finally:
            self.lock.release()

    def _apply_pending(self) -> str:
        """Called with the lock held. The old App is not closed explicitly: the scheduler or a
        request may still hold it for a moment, the garbage collector will close its SQLite connection."""
        with self._pending_lock:
            cfg, self._pending = self._pending, None
        if cfg is None or self.rebuild is None:
            return "none"
        try:
            self.app = self.rebuild(cfg, self.app)
        except Exception as e:  # noqa: BLE001 - the config was validated; keep the old App
            log.exception("settings: rebuild failed, keeping the old config")
            return f"error: {e}"
        self.generation += 1
        log.info("settings applied: %s", ", ".join(f"{c.watch} ({'notifying' if c.notifier.enabled else 'silent'})"
                                                     for c in self.app.contexts))
        return "applied"


def describe_watches(app: App) -> dict[str, dict[str, Any]]:
    return {
        c.watch: {"title": c.title, "unit_divisor": c.profile.unit_divisor, "unit_label": c.profile.unit_label,
                  "attr_labels": c.profile.attr_labels, "notify_enabled": c.notifier.enabled}
        for c in app.contexts
    }


def build_web(app: App, runner: ScanRunner | None = None, settings: SettingsManager | None = None) -> Optional[WebServer]:
    wcfg = app.cfg.get("web") or {}
    if not wcfg.get("enabled", True):
        return None
    port = int(env("LISTINGWATCHER_HTTP_PORT", str(wcfg.get("port", 8080))))
    host = str(wcfg.get("host", "0.0.0.0"))
    db_path = app.store.path
    current = (lambda: runner.app) if runner else (lambda: app)
    web = WebApp(
        store_factory=lambda: Store(db_path, history_days=current().store.history_days),
        title=lambda: str((current().cfg.get("web") or {}).get("title") or "Annonces"),
        watches=lambda: describe_watches(current()),
        scan_trigger=runner.trigger if runner else None,
        scan_running=(lambda: runner.running) if runner else None,
        source_labels=(app.cfg.get("notify") or {}).get("source_labels") or {},
        settings=settings,
    )
    if settings is not None and runner is not None:
        settings.on_change = runner.reload
    return WebServer(web, host, port)


class Service:
    def __init__(self, app: App, runner: ScanRunner | None = None, settings: SettingsManager | None = None):
        self.stop = False
        self.runner = runner or ScanRunner(app)
        self.settings = settings

    @property
    def app(self) -> App:
        return self.runner.app

    def _schedule(self) -> tuple[list[str], float, str]:
        sched = self.app.cfg.get("schedule") or {}
        return (list(sched.get("scan_times") or ["08:00", "13:00", "19:00"]), float(sched.get("jitter_minutes", 10)),
                str(sched.get("digest_time") or "08:00"))

    def _handle_signal(self, *_):
        log.info("stop requested")
        self.stop = True

    def run(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._handle_signal)
            except (ValueError, OSError):  # pragma: no cover - outside the main thread
                pass
        web = build_web(self.app, self.runner, self.settings)
        if web:
            web.start()
        log.info("watches: %s", ", ".join(f"{c.watch} ({'notifying' if c.notifier.enabled else 'silent'})"
                                           for c in self.app.contexts))
        if bool((self.app.cfg.get("schedule") or {}).get("run_on_start", True)):
            log.info("initial scan")
            self.runner.run_blocking()
        while not self.stop:
            tz = self.app.tz
            scan_times, jitter, digest_time = self._schedule()
            generation = self.runner.generation
            now = datetime.now(tz)
            scan_at = next_scan(now, scan_times, jitter)
            digest_at = next_digest(now, digest_time, self.app.store.get_meta("digest_date"))
            wake = min(scan_at, digest_at)
            log.info("next scan %s, next digest %s", scan_at.strftime("%d/%m %H:%M"), digest_at.strftime("%d/%m %H:%M"))
            while not self.stop and datetime.now(tz) < wake and self.runner.generation == generation:
                time.sleep(min(15, max(1, (wake - datetime.now(tz)).total_seconds())))
            if self.stop:
                break
            if self.runner.generation != generation:
                continue                              # settings reloaded: recompute the slots
            now = datetime.now(tz)
            if now >= digest_at:
                run_digests(self.app)
            if now >= scan_at:
                self.runner.run_blocking()
        if web:
            web.stop()
        log.info("service stopped")
