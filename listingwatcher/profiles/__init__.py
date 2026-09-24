"""Registre des profils. watches.<veille>.profile.type choisit le profil ; sa section porte le même nom."""
from __future__ import annotations

from typing import Any

from .base import Profile
from .hdd import HddProfile
from .keywords import KeywordsProfile
from .pc import PcProfile

REGISTRY: dict[str, type[Profile]] = {
    "hdd": HddProfile,
    "keywords": KeywordsProfile,
    "pc": PcProfile,
}


def load_profile(cfg: dict[str, Any]) -> Profile:
    """`cfg` est une veille (clé `profile`) ou la config complète (première veille de `watches`)."""
    if "profile" not in cfg and cfg.get("watches"):
        cfg = next(iter(cfg["watches"].values()))
    pcfg = cfg.get("profile") or {}
    kind = str(pcfg.get("type") or "hdd")
    cls = REGISTRY.get(kind)
    if cls is None:
        raise ValueError(f"profil inconnu : {kind} (disponibles : {', '.join(REGISTRY)})")
    return cls(pcfg.get(kind) or {})


__all__ = ["Profile", "HddProfile", "KeywordsProfile", "PcProfile", "REGISTRY", "load_profile"]
