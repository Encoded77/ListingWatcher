"""Scoring of a classified listing: delivered-price tiers, priority, age, anti-scam heuristics.
Generic: the profile's only contribution is the derived metric (€/To…) and the market key."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from .models import Decision, Listing, ModelInfo
from .profiles.base import Profile

_LOW_FLAGS = {"model_unknown", "low_tier", "ref_unverified"}


def listing_age_days(posted_at: str | None, now: datetime | None = None) -> Optional[int]:
    """Age in days of a publication date ('2026-09-06 14:59:56' leboncoin, ISO 8601 eBay)."""
    if not posted_at:
        return None
    s = posted_at.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T", 1) if " " in s and "T" not in s else s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, ((now or datetime.now(timezone.utc)) - dt).days)


class MarketStats(Protocol):
    def median_unit_price(self, model: Optional[str], family: Optional[str]) -> tuple[Optional[float], int]:
        """Rolling median of the delivered price per item, and number of samples."""


class NoMarket:
    def median_unit_price(self, model, family):
        return None, 0


class Filter:
    def __init__(self, cfg: dict[str, Any], market: MarketStats | None = None, profile: Profile | None = None):
        self.thresholds = sorted(cfg.get("thresholds", []), key=lambda t: t["max_delivered"])
        notify = cfg.get("notify", {}) or {}
        self.require_delivery = bool(notify.get("require_delivery", False))
        self.max_age_days = int(notify.get("max_age_days", 0) or 0)
        mk = cfg.get("market", {}) or {}
        self.min_samples = int(mk.get("min_samples", 4))
        self.reference_unit_price = float(mk.get("reference_unit_price", 0) or 0)
        scam = cfg.get("scam", {}) or {}
        self.below_median_ratio = float(scam.get("below_median_ratio", 0.55))
        self.zero_feedback_below_market = bool(scam.get("zero_feedback_below_market", True))
        self.max_shipping_eur = float(scam.get("max_shipping_eur", 40))
        self.max_shipping_ratio = float(scam.get("max_shipping_ratio", 0.5))
        self.market = market or NoMarket()
        self.profile = profile or Profile()

    def decide(self, listing: Listing, info: ModelInfo) -> Decision:
        qty = max(1, info.quantity)
        notes: list[str] = []
        if qty > 1 and "multi_capacity" in info.flags:
            # mixed lot (8 TB + other capacities): dividing the price by the item count would be wrong
            notes.append(f"lot mixte de {qty} articles, prix total non divisé")
            qty = 1
        delivered = listing.delivered_price
        unit = round(delivered / qty, 2)
        per_unit = self.profile.per_unit(unit)
        if listing.shipping is None:
            notes.append("port inconnu")
        elif listing.shipping_estimated:
            notes.append("port estimé")

        if not info.accepted:
            return Decision(False, "reject", "none", unit, per_unit, [], notes)

        tier = "ignore"
        priority = "none"
        tags: list[str] = []
        for th in self.thresholds:
            if unit <= float(th["max_delivered"]):
                tier = str(th.get("priority", "default"))
                priority = tier
                tags = list(th.get("tags", []) or [])
                break

        if tier == "ignore":
            return Decision(False, "ignore", "none", unit, per_unit, [], notes)
        age = listing_age_days(listing.posted_at)
        if self.max_age_days and age is not None and age > self.max_age_days:
            notes.append(f"annonce publiée il y a {age} jours")
            return Decision(False, "ignore", "none", unit, per_unit, [], notes)
        if self.require_delivery and listing.delivery is False:
            notes.append("pas de livraison")
            return Decision(False, "ignore", "none", unit, per_unit, [], notes)

        if (_LOW_FLAGS | self.profile.low_flags) & set(info.flags):
            priority = "low"
        if "model_unknown" in info.flags or "ref_missing" in info.flags:
            notes.append("demander la référence exacte" + (" et une capture SMART" if self.profile.name == "hdd" else ""))
        if "ref_unverified" in info.flags:
            notes.append("référence hors catalogue : vérifier avant d'acheter")
        if "low_tier" in info.flags:
            notes.append("gamme inférieure")
        for flag in info.flags:
            if flag in self.profile.flag_notes:
                notes.append(self.profile.flag_notes[flag])
        if qty > 1:
            notes.append(f"lot de {qty} : {delivered:.0f} € au total")
        if "multi_capacity" in info.flags:
            notes.append("plusieurs capacités citées")

        suspicious = self._suspicious(listing, info, unit)
        return Decision(True, tier, priority, unit, per_unit, suspicious, notes, tags)

    # ------------------------------------------------------------------ anti-scam
    def _suspicious(self, listing: Listing, info: ModelInfo, unit: float) -> list[str]:
        out: list[str] = []
        fine, broad = self.profile.market_key(info)
        median, n = self.market.median_unit_price(fine, broad)
        if median is None or n < self.min_samples:
            median = self.reference_unit_price or None
        if median and unit < median * self.below_median_ratio:
            out.append(f"prix {unit:.0f} € < {self.below_median_ratio:.0%} de la médiane ({median:.0f} €)")
        reviews = listing.seller_reviews
        if self.zero_feedback_below_market and (reviews is None or reviews == 0) and median and unit < median:
            out.append("vendeur sans évaluation et prix sous le marché")
        ship = listing.shipping or 0.0
        if ship > self.max_shipping_eur or (listing.price > 0 and ship > listing.price * self.max_shipping_ratio):
            out.append(f"frais de port aberrants ({ship:.0f} €)")
        if listing.delivery is False and listing.secure_payment is False and not reviews:
            out.append("ni livraison, ni paiement sécurisé, vendeur sans historique")
        return out
