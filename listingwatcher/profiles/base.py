"""Un profil = tout ce qui est spécifique au type d'objet surveillé.

Le cœur (sources, store, filtrage par prix, notifications, interface web) ne connaît que cette
interface. Pour surveiller autre chose que des disques durs : écrire un profil (ou utiliser
`keywords`, entièrement configurable) et le désigner dans config.yaml > watches.<veille>.profile.type."""
from __future__ import annotations

from typing import Any, Optional

from ..models import Listing, ModelInfo


class Profile:
    name: str = "base"
    #: unité de comparaison optionnelle : le prix rendu par article est divisé par `unit_divisor`
    #: et affiché avec `unit_label` (ex. 8 et « €/To »). None = pas de métrique dérivée.
    unit_divisor: Optional[float] = None
    unit_label: str = ""
    #: libellés d'affichage des attributs posés dans ModelInfo.attrs (notification, interface)
    attr_labels: dict[str, str] = {}
    #: drapeaux qui abaissent la priorité de notification (en plus de model_unknown, low_tier, ref_unverified)
    low_flags: frozenset[str] = frozenset()
    #: note ajoutée à la décision quand un drapeau est présent (« demander la RAM »…)
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
        """Texte court pour un titre de notification, vide si le profil n'a pas de métrique."""
        if value is None:
            return ""
        s = f"{value:.2f}".replace(".", ",")
        return f"{s[:-3] if s.endswith(',00') else s} {self.unit_label}".strip()

    def attr_lines(self, info: ModelInfo) -> list[str]:
        """Lignes « Libellé : valeur » pour le corps d'une notification."""
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
        """Clés (fine, large) pour la médiane de marché : par défaut référence puis gamme."""
        return info.model, info.family

    def describe(self, listing: Listing, info: ModelInfo) -> str:  # pragma: no cover — surcharge optionnelle
        return info.label
