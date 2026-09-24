"""Profil générique piloté par la config, sans code : mots-clés requis / interdits, familles par
mots-clés, référence par expression régulière, quantité de lot. Suffit pour surveiller un objet
précis (une carte graphique, un vélo, un appareil photo) en quelques lignes de YAML :

profile:
  type: keywords
  keywords:
    unit_label: ""                      # ou "€/Go" avec unit_divisor
    unit_divisor: null
    require_any: ["rtx 3080", "3080"]   # au moins un (titre + description), sinon rejet `no_match`
    reject: ["pour pieces", "hs", "bloc alim"]          # titre + description
    reject_title: ["boitier", "pc complet"]             # titre seulement
    families:                            # premier qui matche → family
      - {name: "RTX 3080 Ti", any: ["3080 ti", "3080ti"]}
      - {name: "RTX 3080", any: ["3080"]}
    model_regex: "\\b(?:TUF|ROG|GAMING X|VENTUS)[ \\w-]{0,12}\\b"   # optionnel, référence/variante
    lot_regex: "\\blot de (\\d{1,2})\\b"                            # optionnel
"""
from __future__ import annotations

import re
from typing import Any

from ..models import ModelInfo
from ..normalize import _kw_regex, norm
from .base import Profile


class KeywordsProfile(Profile):
    name = "keywords"

    def __init__(self, pcfg: dict[str, Any] | None = None):
        super().__init__(pcfg)
        c = self.cfg
        self.unit_label = str(c.get("unit_label") or "")
        self.unit_divisor = float(c["unit_divisor"]) if c.get("unit_divisor") else None
        self.require_any = _kw_regex(list(c.get("require_any") or []))
        self.has_require = bool(c.get("require_any"))
        self.reject = _kw_regex(list(c.get("reject") or []))
        self.reject_title = _kw_regex(list(c.get("reject_title") or []))
        self.families = [(str(f["name"]), _kw_regex(list(f.get("any") or []))) for f in (c.get("families") or [])]
        self.model_regex = re.compile(c["model_regex"], re.I) if c.get("model_regex") else None
        self.lot_regex = re.compile(c["lot_regex"], re.I) if c.get("lot_regex") else re.compile(r"\blot de (\d{1,2})\b")
        self.attr_labels = dict(c.get("attr_labels") or {})

    def classify(self, title: str, description: str = "", condition_code: str = "") -> ModelInfo:
        t, d = norm(title), norm(description)
        full = f"{t} {d}".strip()
        info = ModelInfo(verdict="accept")
        if condition_code == "parts":
            info.verdict = "reject"; info.reasons.append("dead"); return info
        m = self.reject_title.search(t) or self.reject.search(full)
        if m:
            info.verdict = "reject"; info.reasons.append(f"reject:{m.group(0)}"); return info
        if self.has_require and not self.require_any.search(full):
            info.verdict = "reject"; info.reasons.append("no_match"); return info
        for name, rx in self.families:
            if rx.search(full):
                info.family = name
                break
        if self.model_regex:
            mm = self.model_regex.search(title) or self.model_regex.search(description)
            if mm:
                info.model = mm.group(0).strip()
        if not info.family and not info.model:
            info.flags.append("model_unknown")
        lm = self.lot_regex.search(t)
        if lm:
            try:
                q = int(lm.group(1))
                if 2 <= q <= 50:
                    info.quantity = q
                    info.flags.append("lot")
            except (IndexError, ValueError):
                pass
        return info
