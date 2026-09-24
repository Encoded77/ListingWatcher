"""Shared types: normalized listing, classification result, filtering decision.

The core is generic (listings, not only hard drives): whatever is domain-specific
lives in a profile (listingwatcher/profiles/) and travels here through `ModelInfo.attrs`."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# The higher the value, the worse the status. Used to merge two contradictory
# observations of the same listing (cached result pages).
STATUS_RANK = {"active": 0, "gone": 1, "pending": 2, "sold": 3}


def worst_status(a: str, b: str) -> str:
    return a if STATUS_RANK.get(a, 0) >= STATUS_RANK.get(b, 0) else b


@dataclass
class Listing:
    source: str                      # "lbc" | "ebay"
    listing_id: str
    url: str
    title: str
    price: float                     # item price, EUR
    shipping: Optional[float] = None  # shipping to France; None = unknown
    shipping_estimated: bool = False
    fees: float = 0.0                # buyer fees (leboncoin protection, etc.)
    currency: str = "EUR"
    description: str = ""
    condition: str = ""              # original label
    condition_code: str = "unknown"  # new | refurb | used | parts | unknown
    seller_name: str = ""
    seller_type: str = ""            # pro | private
    seller_rating: Optional[float] = None   # in %
    seller_reviews: Optional[int] = None
    location: str = ""
    country: str = "FR"
    delivery: Optional[bool] = None
    secure_payment: Optional[bool] = None
    quantity: Optional[int] = None   # stock declared by the platform
    returns: str = ""
    status: str = "active"           # active | pending | sold
    posted_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def delivered_price(self) -> float:
        return round(self.price + (self.shipping or 0.0) + (self.fees or 0.0), 2)

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.description}"


@dataclass
class ModelInfo:
    """Result of a listing's classification by a profile.

    `family` = family / category (IronWolf, « vélo route »…), `model` = precise reference,
    `attrs` = profile-specific attributes (capacities, SMART hours, size…)."""
    verdict: str                     # accept | reject
    family: Optional[str] = None
    model: Optional[str] = None
    brand: Optional[str] = None
    reasons: list[str] = field(default_factory=list)   # reject reasons
    flags: list[str] = field(default_factory=list)     # model_unknown, ref_missing, ref_unverified, low_tier, lot, multi_capacity
    attrs: dict[str, Any] = field(default_factory=dict)
    quantity: int = 1                # item count inferred from the title (lot of N)

    @property
    def accepted(self) -> bool:
        return self.verdict == "accept"

    @property
    def label(self) -> str:
        if self.family and self.model:
            return f"{self.family} {self.model}"
        if self.family:
            return f"{self.family} (réf. ?)"
        if self.model:
            return self.model
        if self.brand:
            return f"{self.brand} modèle inconnu"
        return "Modèle inconnu"


@dataclass
class Decision:
    keep: bool
    tier: str                        # urgent | default | low | ignore | reject
    priority: str                    # urgent | default | low | none
    unit_price: float                # delivered price per item
    per_unit: Optional[float] = None  # delivered price per profile unit (€/To…), None if the profile has none
    suspicious: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class FetchResult:
    source: str
    listings: list[Listing]
    complete: bool = True            # False if the source could not be browsed entirely
    errors: list[str] = field(default_factory=list)
