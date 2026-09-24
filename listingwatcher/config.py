"""Chargement de config.yaml : substitution ${VAR} / ${VAR:-défaut} depuis l'environnement, puis
normalisation en **veilles** (`watches`).

Une veille = un objet surveillé : profil, paliers de prix, marché, anti-arnaque, recherches par source,
toggle de notification. Le transport (délais, robots, identifiants, marketplaces) reste commun sous
`sources`. Un config.yaml à l'ancien format (profil et recherches à la racine) est enveloppé dans une
veille unique nommée `hdd`, celle qui porte les données déjà en base."""
from __future__ import annotations

import copy
import os
import re
from typing import Any

from ruamel.yaml import YAML

_ENV = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}")

LEGACY_WATCH = "hdd"

#: clés d'une section `sources.<source>` qui décrivent *quoi* chercher (par veille) et non le transport
SEARCH_KEYS = {"searches", "queries", "category_ids", "condition_ids", "price_min", "price_max"}


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _legacy_watch(cfg: dict[str, Any]) -> dict[str, Any]:
    """Ancien format : tout à la racine → une veille `hdd`."""
    pcfg = cfg.get("profile") or {"type": "hdd"}
    sources = {}
    for key, scfg in (cfg.get("sources") or {}).items():
        sources[key] = {k: copy.deepcopy(v) for k, v in (scfg or {}).items() if k in SEARCH_KEYS}
    return {
        "title": pcfg.get("title") or (cfg.get("web") or {}).get("title") or "Annonces",
        "profile": copy.deepcopy(pcfg),
        "thresholds": copy.deepcopy(cfg.get("thresholds") or []),
        "market": {}, "scam": {}, "notify": {},
        "sources": sources,
    }


def normalize_watches(cfg: dict[str, Any]) -> dict[str, Any]:
    """Complète chaque veille avec les valeurs par défaut de la racine (notify, market, scam, thresholds)."""
    watches = cfg.get("watches")
    if not watches:
        watches = {LEGACY_WATCH: _legacy_watch(cfg)}
    out: dict[str, dict[str, Any]] = {}
    for name, w in watches.items():
        w = dict(w or {})
        if not w.get("enabled", True):
            continue
        w["name"] = str(name)
        w["title"] = str(w.get("title") or name)
        w.setdefault("profile", {"type": "keywords"})
        w["thresholds"] = list(w.get("thresholds") or cfg.get("thresholds") or [])
        for section in ("notify", "market", "scam"):
            w[section] = {**(cfg.get(section) or {}), **(w.get(section) or {})}
        w["notify"].setdefault("enabled", True)
        w["sources"] = {k: dict(v or {}) for k, v in (w.get("sources") or {}).items()}
        out[str(name)] = w
    if not out:
        raise ValueError("config.yaml : aucune veille active (section `watches`)")
    cfg["watches"] = out
    return cfg


def watch_source_cfg(cfg: dict[str, Any], watch: dict[str, Any], key: str) -> dict[str, Any] | None:
    """Section de source effective pour une veille : transport commun + recherches de la veille.
    None si la veille n'utilise pas cette source ou si la source est désactivée."""
    base = (cfg.get("sources") or {}).get(key)
    wsrc = watch.get("sources", {}).get(key)
    if base is None or wsrc is None:
        return None
    if not (base.get("enabled", True) and wsrc.get("enabled", True)):
        return None
    merged = {**base, **wsrc}
    merged["enabled"] = True
    return merged


def load_config(path: str | None = None) -> dict[str, Any]:
    path = path or os.environ.get("LISTINGWATCHER_CONFIG", "config.yaml")
    with open(path, encoding="utf-8") as f:
        cfg = YAML(typ="safe", pure=True).load(f) or {}   # même dialecte (YAML 1.2) que l'édition web
    cfg = _expand(cfg)
    cfg.setdefault("sources", {})
    cfg.setdefault("schedule", {})
    cfg.setdefault("notify", {})
    cfg["_path"] = path
    return normalize_watches(cfg)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()
