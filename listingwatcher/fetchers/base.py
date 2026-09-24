"""Common interface of the sources."""
from __future__ import annotations

from typing import Any

from ..models import FetchResult, Listing


class MissingCredentials(Exception):
    """Missing credentials: the source is cleanly disabled at startup."""


class BaseFetcher:
    name: str = "base"            # short identifier stored in the database (lbc, ebay...)
    supports_enrich: bool = False  # can complete a kept listing (description, stock, returns)

    def __init__(self, scfg: dict[str, Any], cfg: dict[str, Any]):
        self.scfg = scfg or {}
        self.cfg = cfg

    @staticmethod
    def make_transport(scfg: dict[str, Any]) -> Any:
        """Network client shared by every watch using this source (a single polite queue,
        a single token). None if the source does not need one."""
        return None

    def fetch(self) -> FetchResult:
        """Walks the source and returns the deduplicated listings.
        Raises BlockedError (fetchers.http) if the source refuses access."""
        raise NotImplementedError

    def enrich(self, listing: Listing) -> bool:
        """Second pass, reserved for listings kept by the filter. Returns True if the listing was completed."""
        return False
