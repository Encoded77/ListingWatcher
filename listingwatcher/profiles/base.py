"""A profile = everything specific to the kind of object being watched.

The core (sources, store, price filtering, notifications, web UI) only knows this
interface. To watch something other than hard drives: write a profile (or use
`keywords`, fully configurable) and name it in config.yaml > watches.<watch>.profile.type."""
from __future__ import annotations

from typing import Any, Optional

from ..models import Listing, ModelInfo


class Profile:
    name: str = "base"
    #: optional comparison unit: the delivered price per item is divided by `unit_divisor`
    #: and displayed with `unit_label` (e.g. 8 and « €/To »). None = no derived metric.
    unit_divisor: Optional[float] = None
    unit_label: str = ""
    #: display labels of the attributes set in ModelInfo.attrs (notification, UI)
    attr_labels: dict[str, str] = {}
    #: flags that lower the notification priority (on top of model_unknown, low_tier, ref_unverified)
    low_flags: frozenset[str] = frozenset()
    #: note added to the decision when a flag is present (« demander la RAM »…)
    flag_notes: dict[str, str] = {}

    def __init__(self, pcfg: dict[str, Any] | None = None):
        self.cfg = pcfg or {}

    def classify(self, title: str, description: str = "", condition_code: str = "") -> ModelInfo:
        raise NotImplementedError

    def per_unit(self, unit_price: float) -> Optional[float]:
        if not self.unit_divisor:
            return None
        return round(unit_price / self.unit_divisor, 2)

    def format_per_unit(self, value: Optional[float]) -> str:
        """Short text for a notification title, empty if the profile has no metric."""
        if value is None:
            return ""
        s = f"{value:.2f}".replace(".", ",")
        return f"{s[:-3] if s.endswith(',00') else s} {self.unit_label}".strip()

    def attr_lines(self, info: ModelInfo) -> list[str]:
        """« Label : value » lines for a notification body."""
        out = []
        for key, label in self.attr_labels.items():
            v = info.attrs.get(key)
            if v in (None, "", [], {}):
                continue
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            out.append(f"{label} : {v}")
        return out

    def market_key(self, info: ModelInfo) -> tuple[Optional[str], Optional[str]]:
        """(fine, broad) keys for the market median: by default reference, then family."""
        return info.model, info.family

    def describe(self, listing: Listing, info: ModelInfo) -> str:  # pragma: no cover, optional override
        return info.label
