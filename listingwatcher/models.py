"""Types partagés : annonce normalisée, résultat de classification, décision de filtrage.

Le cœur est générique (annonces, pas seulement disques durs) : ce qui est spécifique au domaine
vit dans un profil (listingwatcher/profiles/) et transite ici par `ModelInfo.attrs`."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Plus la valeur est haute, plus le statut est défavorable. Utilisé pour fusionner
# deux observations contradictoires de la même annonce (pages de résultats en cache).
STATUS_RANK = {"active": 0, "gone": 1, "pending": 2, "sold": 3}


def worst_status(a: str, b: str) -> str:
    return a if STATUS_RANK.get(a, 0) >= STATUS_RANK.get(b, 0) else b


@dataclass
class Listing:
    source: str                      # "lbc" | "ebay"
    listing_id: str
    url: str
    title: str
    price: float                     # prix article, EUR
    shipping: Optional[float] = None  # port vers la France ; None = inconnu
    shipping_estimated: bool = False
    fees: float = 0.0                # frais acheteur (protection leboncoin, etc.)
    currency: str = "EUR"
    description: str = ""
    condition: str = ""              # libellé d'origine
    condition_code: str = "unknown"  # new | refurb | used | parts | unknown
    seller_name: str = ""
    seller_type: str = ""            # pro | private
    seller_rating: Optional[float] = None   # en %
    seller_reviews: Optional[int] = None
    location: str = ""
    country: str = "FR"
    delivery: Optional[bool] = None
    secure_payment: Optional[bool] = None
    quantity: Optional[int] = None   # stock déclaré par la plateforme
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
    """Résultat de classification d'une annonce par un profil.

    `family` = gamme / catégorie (IronWolf, « vélo route »…), `model` = référence précise,
    `attrs` = attributs propres au profil (capacités, heures SMART, taille…)."""
    verdict: str                     # accept | reject
    family: Optional[str] = None
    model: Optional[str] = None
    brand: Optional[str] = None
    reasons: list[str] = field(default_factory=list)   # motifs de rejet
    flags: list[str] = field(default_factory=list)     # model_unknown, ref_missing, ref_unverified, low_tier, lot, multi_capacity
    attrs: dict[str, Any] = field(default_factory=dict)
    quantity: int = 1                # nombre d'articles déduit du titre (lot de N)

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
    unit_price: float                # prix rendu par article
    per_unit: Optional[float] = None  # prix rendu ramené à l'unité du profil (€/To…), None si le profil n'en a pas
    suspicious: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class FetchResult:
    source: str
    listings: list[Listing]
    complete: bool = True            # False si la source n'a pas pu être parcourue entièrement
    errors: list[str] = field(default_factory=list)
