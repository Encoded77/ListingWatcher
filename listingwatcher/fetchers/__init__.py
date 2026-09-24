"""Source registry. To add a source: a module with a class deriving from BaseFetcher,
then an entry in REGISTRY (key = section name under config.yaml > sources).

Each watch gets its own fetchers (its searches), but the transport (polite leboncoin HTTP client,
eBay client and token) is shared by every watch in the process: never two leboncoin requests
in parallel, and a page already read during this scan is not read again."""
from __future__ import annotations

import logging
from typing import Any

from ..config import watch_source_cfg
from .base import BaseFetcher, MissingCredentials
from .ebay import EbayFetcher
from .leboncoin import LeboncoinFetcher

log = logging.getLogger("listingwatcher.fetchers")

REGISTRY: dict[str, type[BaseFetcher]] = {
    "leboncoin": LeboncoinFetcher,
    "ebay": EbayFetcher,
}


def build_fetchers(cfg: dict[str, Any], watch: dict[str, Any], only: str | None = None,
                   transports: dict[str, Any] | None = None) -> list[BaseFetcher]:
    """Fetchers of one watch. `transports` (shared dict, filled along the way) holds the common clients."""
    for key in cfg.get("sources") or {}:
        if key not in REGISTRY:
            log.warning("unknown source in config.yaml: %s", key)
    out: list[BaseFetcher] = []
    for key, cls in REGISTRY.items():
        scfg = watch_source_cfg(cfg, watch, key)
        if scfg is None:
            continue
        if only and only not in (key, cls.name):
            continue
        try:
            if transports is not None:
                if key not in transports:
                    transports[key] = cls.make_transport(scfg)
                out.append(cls(scfg, cfg, transport=transports[key]))
            else:
                out.append(cls(scfg, cfg))
        except MissingCredentials as e:
            log.warning("source %s disabled for watch %s: %s", key, watch.get("name"), e)
    return out


def reset_transports(transports: dict[str, Any]) -> None:
    """Start of a scan: clears the page caches of the shared transports."""
    for t in transports.values():
        reset = getattr(t, "reset", None)
        if callable(reset):
            reset()


__all__ = ["BaseFetcher", "MissingCredentials", "REGISTRY", "build_fetchers", "reset_transports",
           "EbayFetcher", "LeboncoinFetcher"]
