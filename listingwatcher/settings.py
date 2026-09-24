"""Settings editable from the web interface.

The image's config.yaml is a **seed**: on the service's first start it is copied into the data
volume (`LISTINGWATCHER_LIVE_CONFIG`, /data/config.yaml in the container), and that live copy is
authoritative from then on. The interface edits it round-trip (ruamel.yaml): comments and key order
survive, and so do the keys the form does not know about. Every write is validated (profile, tiers,
searches, slots) before being applied, the previous version is kept in `config-history/`, then the
service rebuilds itself hot.

`${VAR}` values stay as-is in the file: the interface sees and writes the raw text, never the
expanded config."""
from __future__ import annotations

import copy
import hashlib
import io
import logging
import os
import re
import shutil
import threading
import unicodedata
from datetime import datetime
from typing import Any, Callable, Optional

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import CommentMark
from ruamel.yaml.tokens import CommentToken

from .config import _expand, env, normalize_watches

log = logging.getLogger("listingwatcher.settings")

WATCH_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
PRIORITIES = ("min", "low", "default", "high", "urgent")
HISTORY_KEEP = 10

#: root keys exposed in the "General" tab (source transport stays out of the interface)
GLOBAL_FIELDS: dict[str, dict[str, str]] = {
    "schedule": {"run_on_start": "bool", "scan_times": "times", "jitter_minutes": "float",
                 "digest_time": "time", "digest_send_if_empty": "bool"},
    "notify": {"enabled": "bool", "price_drop_pct": "float", "suspicious": "suspicious",
               "require_delivery": "bool", "max_age_days": "int"},
    "market": {"history_days": "int", "min_samples": "int"},
    "scam": {"below_median_ratio": "float", "zero_feedback_below_market": "bool",
             "max_shipping_eur": "float", "max_shipping_ratio": "float"},
    "web": {"title": "str"},
}
#: per-watch overrides; an empty value = inherited from the root
WATCH_FIELDS: dict[str, dict[str, str]] = {
    "notify": {"enabled": "bool", "topic": "str", "label": "str", "price_drop_pct": "float",
               "suspicious": "suspicious", "require_delivery": "bool", "max_age_days": "int"},
    "market": {"reference_unit_price": "float", "min_samples": "int"},
    "scam": GLOBAL_FIELDS["scam"],
}


class SettingsError(ValueError):
    """Rejected settings: the message is displayed as-is in the interface."""


class ConflictError(SettingsError):
    """The file changed since it was read (another tab, manual edit)."""


def _yaml() -> YAML:
    y = YAML()                      # round-trip: comments, order, styles
    y.preserve_quotes = True
    y.width = 4096                  # never fold a string in the middle of a flow list
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def plain(node: Any) -> Any:
    """CommentedMap/Seq → plain dict/list (for JSON and validation)."""
    if isinstance(node, dict):
        return {str(k): plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [plain(v) for v in node]
    return node


def _scalar_ish(v: Any) -> bool:
    return not isinstance(v, (dict, list)) or (isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v))


def _flow(node: Any) -> Any:
    """Compact style for data coming from the interface: scalar lists and small maps in flow
    style (`[a, b]`, `{category: …, slug: …}`), like the original config.yaml."""
    if isinstance(node, dict):
        out = CommentedMap((k, _flow(v)) for k, v in node.items())
        if len(node) <= 3 and all(_scalar_ish(v) for v in node.values()):
            out.fa.set_flow_style()
        return out
    if isinstance(node, list):
        out = CommentedSeq(_flow(v) for v in node)
        if all(not isinstance(v, (dict, list)) for v in node):
            out.fa.set_flow_style()
        return out
    return node


def _trailing(cm: CommentedMap, key: Any) -> Optional[CommentToken]:
    ent = cm.ca.items.get(key)
    return ent[2] if ent and len(ent) > 2 and isinstance(ent[2], CommentToken) else None


def _split_trailing(tok: CommentToken) -> tuple[str, str]:
    """Comment after a value: (end of line, full lines that follow). The full lines actually
    belong to the next key ("# Plafond 250 €" before `thresholds`)."""
    first, nl, rest = tok.value.partition("\n")
    return first + nl, rest


def _inline(v: Any) -> bool:
    return not isinstance(v, (dict, list)) or (hasattr(v, "fa") and bool(v.fa.flow_style()))


def _tail(cm: CommentedMap, key: Any) -> tuple[CommentedMap, Any]:
    """(map, key) carrying the comment that follows the value of cm[key]: the key itself for an
    inline value, otherwise the last key of the block map, recursively."""
    v = cm[key]
    if isinstance(v, CommentedMap) and len(v) and not v.fa.flow_style():
        return _tail(v, next(reversed(v)))
    return cm, key


def _attach(cm: CommentedMap, key: Any, rest: str) -> None:
    """Attach full-line comments after the value of cm[key] (or drop them if it is a block)."""
    tm, tk = _tail(cm, key)
    if not _inline(tm[tk]):
        return
    tok = _trailing(tm, tk)
    if tok is None:
        tm.ca.items[tk] = [None, None, CommentToken("\n" + rest, CommentMark(0), None), None]
    else:
        tok.value = tok.value.rstrip("\n") + "\n" + rest


def _set_new(cm: CommentedMap, key: Any, value: Any) -> None:
    """Append a key at the end of a map; the full-line comments that followed the previous last key
    precede whatever comes after the map, so they slide under the new key."""
    last = next(reversed(cm), None) if len(cm) else None
    cm[key] = value
    if last is None or not _inline(value):          # a block map carries its comments elsewhere
        return
    tm, tk = _tail(cm, last)
    tok = _trailing(tm, tk) if _inline(tm[tk]) else None
    if tok is None:
        return
    head, rest = _split_trailing(tok)
    if rest.strip():
        tok.value = head if head.strip() else "\n"
        _attach(cm, key, rest)


def _delete(cm: CommentedMap, key: Any) -> None:
    """Delete a key without losing the full-line comments that followed its value."""
    keys = list(cm)
    idx = keys.index(key)
    tm, tk = _tail(cm, key)
    tok = _trailing(tm, tk) if _inline(tm[tk]) else None
    del cm[key]
    if tok is None or idx == 0:
        return
    rest = _split_trailing(tok)[1]
    if rest.strip():
        _attach(cm, keys[idx - 1], rest)


def assign(target: CommentedMap, key: str, value: Any) -> None:
    """Set `value` under `target[key]` while keeping the comments of what does not change:
    recursive merge of maps, lists replaced only when they differ."""
    if value is None:
        if key in target:
            _delete(target, key)
        return
    cur = target.get(key)
    if isinstance(value, dict) and isinstance(cur, CommentedMap):
        for k, v in value.items():                 # additions first: they take over the trailing comments
            assign(cur, k, v)
        for k in [k for k in cur if k not in value]:
            _delete(cur, k)
        return
    if cur is not None and plain(cur) == value:
        return
    new = _flow(value) if isinstance(value, (dict, list)) else value
    if key in target:
        target[key] = new
    else:
        _set_new(target, key, new)


def _align_comments(text: str) -> str:
    """An extracted subtree keeps the original column of its full-line comments: realign them on
    the data line that follows, for a readable editor."""
    lines = text.split("\n")
    nxt = 0
    for i in range(len(lines) - 1, -1, -1):
        s = lines[i].lstrip()
        if s.startswith("#"):
            lines[i] = " " * nxt + s
        elif s:
            nxt = len(lines[i]) - len(s)
    return "\n".join(lines)


def _file_sha(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ---------------------------------------------------------------------- field coercion

def _coerce(kind: str, raw: Any, where: str) -> Any:
    """Form value → YAML value. None/"" = absent (inherited)."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    try:
        if kind == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "oui", "on", "yes")
        if kind == "int":
            return int(float(raw))
        if kind == "float":
            v = float(str(raw).replace(",", "."))
            return int(v) if v.is_integer() else v
        if kind == "str":
            return str(raw).strip()
        if kind == "suspicious":
            v = str(raw).strip()
            if v not in ("separate", "never"):
                raise ValueError("separate ou never")
            return v
        if kind == "time":
            return _hhmm(str(raw))
        if kind == "times":
            items = raw if isinstance(raw, list) else re.split(r"[,;\s]+", str(raw))
            out = sorted({_hhmm(str(x)) for x in items if str(x).strip()})
            if not out:
                raise ValueError("au moins un créneau")
            return out
    except (TypeError, ValueError) as e:
        raise SettingsError(f"{where} : valeur invalide « {raw} » ({e})") from None
    raise SettingsError(f"{where} : type de champ inconnu {kind}")  # pragma: no cover


def _hhmm(s: str) -> str:
    m = re.fullmatch(r"\s*(\d{1,2})[:hH](\d{2})\s*", s)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("format HH:MM")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _section(fields: dict[str, str], values: dict[str, Any] | None, where: str) -> dict[str, Any]:
    values = values or {}
    out = {}
    for key, kind in fields.items():
        v = _coerce(kind, values.get(key), f"{where}.{key}")
        if v is not None:
            out[key] = v
    return out


def _thresholds(rows: Any, where: str) -> list[dict[str, Any]]:
    out = []
    for i, r in enumerate(rows or []):
        if not isinstance(r, dict):
            raise SettingsError(f"{where} : palier {i + 1} invalide")
        mx = _coerce("float", r.get("max_delivered"), f"{where}[{i + 1}].max_delivered")
        if mx is None:
            continue
        prio = str(r.get("priority") or "default").strip()
        if prio not in PRIORITIES:
            raise SettingsError(f"{where}[{i + 1}] : priorité « {prio} » inconnue ({', '.join(PRIORITIES)})")
        tags = r.get("tags") or []
        if isinstance(tags, str):
            tags = [t for t in re.split(r"[,\s]+", tags) if t]
        out.append({"max_delivered": mx, "priority": prio, "tags": [str(t) for t in tags]})
    out.sort(key=lambda t: t["max_delivered"])
    return out


# ---------------------------------------------------------------------- manager

class SettingsManager:
    """Read / write the live copy, validate, and call the reload callback."""

    def __init__(self, live_path: str, image_path: Optional[str] = None,
                 on_change: Optional[Callable[[dict[str, Any]], str]] = None):
        self.path = live_path
        self.image_path = image_path
        self.on_change = on_change
        self.lock = threading.Lock()
        self.history_dir = os.path.join(os.path.dirname(os.path.abspath(live_path)), "config-history")
        self.seed_file = os.path.join(os.path.dirname(os.path.abspath(live_path)), ".config-image.sha256")

    # ---- seed
    @staticmethod
    def ensure_live(live_path: str, image_path: str) -> bool:
        """Copy the image's config.yaml to the live copy if it does not exist yet."""
        if os.path.exists(live_path):
            return False
        os.makedirs(os.path.dirname(os.path.abspath(live_path)) or ".", exist_ok=True)
        shutil.copyfile(image_path, live_path)
        with open(os.path.join(os.path.dirname(os.path.abspath(live_path)), ".config-image.sha256"), "w") as f:
            f.write(_file_sha(image_path))
        log.info("config: live copy created %s (from %s)", live_path, image_path)
        return True

    def image_changed(self) -> bool:
        """The image's config.yaml changed since the seed (new deployment)."""
        if not self.image_path or not os.path.exists(self.image_path) or not os.path.exists(self.seed_file):
            return False
        with open(self.seed_file) as f:
            return f.read().strip() != _file_sha(self.image_path)

    # ---- reading
    def _read(self) -> tuple[CommentedMap, str]:
        with open(self.path, "rb") as f:
            data = f.read()
        doc = _yaml().load(data.decode("utf-8")) or CommentedMap()
        return doc, hashlib.sha256(data).hexdigest()[:16]

    def _text(self) -> str:
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def version(self) -> str:
        return self._read()[1]

    def snapshot(self) -> dict[str, Any]:
        """Everything the tab needs: root, watches (including disabled ones), metadata."""
        from .profiles import REGISTRY as PROFILES
        doc, ver = self._read()
        raw = plain(doc)
        watches = []
        for name, w in (raw.get("watches") or {}).items():
            watches.append(self._watch_view(doc, str(name), w or {}))
        return {
            "version": ver, "path": self.path, "image_path": self.image_path,
            "image_changed": self.image_changed(),
            "global": {sec: {k: (raw.get(sec) or {}).get(k) for k in fields} for sec, fields in GLOBAL_FIELDS.items()},
            "thresholds": (raw.get("thresholds") or []),
            "watches": watches, "profile_types": list(PROFILES), "priorities": list(PRIORITIES),
            "history": self.history(),
        }

    def _watch_view(self, doc: CommentedMap, name: str, w: dict[str, Any]) -> dict[str, Any]:
        prof = w.get("profile") or {}
        ptype = str(prof.get("type") or "keywords")
        wnode = (doc.get("watches") or {}).get(name)
        pnode = wnode.get("profile") if isinstance(wnode, dict) else None
        section = pnode.get(ptype) if isinstance(pnode, dict) else None
        src = w.get("sources") or {}
        lbc, eb = src.get("leboncoin"), src.get("ebay")
        return {
            "name": name, "title": w.get("title") or name, "enabled": bool(w.get("enabled", True)),
            **{sec: {k: (w.get(sec) or {}).get(k) for k in fields} for sec, fields in WATCH_FIELDS.items()},
            "thresholds": w.get("thresholds") or [],
            "leboncoin": None if lbc is None else {
                "enabled": bool(lbc.get("enabled", True)),
                "searches": [{"category": s.get("category", ""), "slug": s.get("slug", "")}
                             for s in (lbc.get("searches") or []) if isinstance(s, dict)]},
            "ebay": None if eb is None else {
                "enabled": bool(eb.get("enabled", True)), "queries": list(eb.get("queries") or []),
                "category_ids": eb.get("category_ids"), "price_min": eb.get("price_min"),
                "price_max": eb.get("price_max"), "condition_ids": eb.get("condition_ids")},
            "profile_type": ptype,
            "profile_yaml": _align_comments(self._dump(section)),
            "yaml": _align_comments(self._dump(wnode)),
        }

    @staticmethod
    def _dump(node: Any) -> str:
        if node is None:
            return ""
        buf = io.StringIO()
        _yaml().dump(node, buf)
        return buf.getvalue()

    @staticmethod
    def _load_yaml(text: str, where: str, indent: int = 0) -> Any:
        """`indent` = column where the subtree will live in the file: full-line comments keep the
        column they were read at, so read them at the right place from the start. (End-of-line
        comments are in absolute column, already correct.)"""
        if indent and text:
            text = "\n".join((" " * indent + ln) if ln.lstrip().startswith("#") else ln for ln in text.split("\n"))
        try:
            return _yaml().load(text) if text and text.strip() else None
        except Exception as e:  # noqa: BLE001 - ruamel syntax errors, message useful as-is
            raise SettingsError(f"{where} : YAML invalide\n{e}") from None

    # ---- building a watch from the form
    def build_watch(self, doc: CommentedMap, name: str, form: dict[str, Any]) -> CommentedMap:
        """Apply the form to the existing (or new) raw watch; return the modified watch.
        Keys unknown to the form are preserved."""
        watches = doc.setdefault("watches", CommentedMap())
        w = watches.get(name)
        if not isinstance(w, CommentedMap):
            w = CommentedMap()
        w = copy.deepcopy(w)
        where = f"veille {name}"
        title = str(form.get("title") or "").strip()
        if not title:
            raise SettingsError(f"{where} : titre obligatoire")
        assign(w, "title", title)
        assign(w, "enabled", None if form.get("enabled", True) else False)
        for sec, fields in WATCH_FIELDS.items():
            vals = _section(fields, form.get(sec), f"{where}.{sec}")
            cur = w.get(sec)
            keep = {k: v for k, v in (plain(cur) or {}).items() if k not in fields} if isinstance(cur, dict) else {}
            assign(w, sec, {**keep, **vals} or None)
        assign(w, "thresholds", _thresholds(form.get("thresholds"), f"{where}.thresholds") or None)

        sources = plain(w.get("sources")) or {}
        lbc = form.get("leboncoin")
        if lbc is None:
            sources.pop("leboncoin", None)
        else:
            cur = sources.get("leboncoin") or {}
            searches = []
            for s in lbc.get("searches") or []:
                cat, slug = str(s.get("category") or "").strip(), str(s.get("slug") or "").strip().strip("/")
                if not cat and not slug:
                    continue
                if not re.fullmatch(r"[a-z0-9_]+", cat) or not re.fullmatch(r"[a-z0-9-]+", slug):
                    raise SettingsError(f"{where}.leboncoin : recherche « {cat}/{slug} » invalide "
                                        "(catégorie en minuscules_soulignées, slug en minuscules-tirets)")
                searches.append({"category": cat, "slug": slug})
            cur["searches"] = searches
            if lbc.get("enabled", True):
                cur.pop("enabled", None)
            else:
                cur["enabled"] = False
            sources["leboncoin"] = cur
        eb = form.get("ebay")
        if eb is None:
            sources.pop("ebay", None)
        else:
            cur = sources.get("ebay") or {}
            queries = eb.get("queries") or []
            if isinstance(queries, str):
                queries = queries.splitlines()
            cur["queries"] = [q.strip() for q in queries if str(q).strip()]
            for key, kind in (("price_min", "float"), ("price_max", "float")):
                v = _coerce(kind, eb.get(key), f"{where}.ebay.{key}")
                if v is None:
                    cur.pop(key, None)
                else:
                    cur[key] = v
            cat = str(eb.get("category_ids") or "").strip()
            if cat and not re.fullmatch(r"\d+(,\d+)*", cat.replace(" ", "")):
                raise SettingsError(f"{where}.ebay.category_ids : identifiants numériques séparés par des virgules")
            if cat:
                cur["category_ids"] = cat.replace(" ", "")
            else:
                cur.pop("category_ids", None)
            cond = eb.get("condition_ids")
            if isinstance(cond, str):
                cond = [c for c in re.split(r"[,\s]+", cond) if c]
            if cond:
                try:
                    cur["condition_ids"] = [int(c) for c in cond]
                except ValueError:
                    raise SettingsError(f"{where}.ebay.condition_ids : entiers attendus") from None
            else:
                cur.pop("condition_ids", None)
            if eb.get("enabled", True):
                cur.pop("enabled", None)
            else:
                cur["enabled"] = False
            sources["ebay"] = cur
        assign(w, "sources", sources or None)

        ptype = str(form.get("profile_type") or "keywords")
        section = self._load_yaml(str(form.get("profile_yaml") or ""), f"{where}.profile.{ptype}", indent=8)
        if section is not None and not isinstance(section, dict):
            raise SettingsError(f"{where}.profile.{ptype} : une table YAML (clé: valeur) est attendue")
        prof = w.get("profile")
        if not isinstance(prof, CommentedMap):
            prof = CommentedMap()
            w["profile"] = prof
        for k in [k for k in prof if k not in ("type", ptype)]:
            del prof[k]
        prof["type"] = ptype
        if section is None:
            prof.pop(ptype, None)
        elif plain(prof.get(ptype)) != plain(section):
            prof[ptype] = section                        # ruamel node: typed comments survive
        return w

    # ---- validation
    @staticmethod
    def validate(doc: CommentedMap) -> dict[str, Any]:
        """Raw config → normalized config ready for build_app; SettingsError otherwise."""
        from .filtering import Filter
        from .profiles import load_profile
        from .scheduler import _parse_hhmm
        raw = _expand(plain(doc))
        raw.setdefault("sources", {})
        raw.setdefault("schedule", {})
        raw.setdefault("notify", {})
        try:
            cfg = normalize_watches(copy.deepcopy(raw))
            # disabled watches are validated too: re-enabling them must not break the service
            every = copy.deepcopy(raw)
            for w in (every.get("watches") or {}).values():
                if isinstance(w, dict):
                    w.pop("enabled", None)
            every = normalize_watches(every)
        except ValueError as e:
            raise SettingsError(str(e)) from None
        except (TypeError, AttributeError) as e:
            raise SettingsError(f"structure inattendue : {e}") from None
        sched = cfg.get("schedule") or {}
        try:
            for s in list(sched.get("scan_times") or []) + [sched.get("digest_time") or "08:00"]:
                _parse_hhmm(s)
        except (TypeError, ValueError):
            raise SettingsError("schedule : créneaux au format HH:MM") from None
        for name, w in every["watches"].items():
            if not WATCH_NAME.fullmatch(name):
                raise SettingsError(f"veille « {name} » : nom invalide (minuscules, chiffres, - et _, 32 caractères)")
            try:
                profile = load_profile(w)
                Filter(w, profile=profile)
                for t in w["thresholds"]:
                    float(t["max_delivered"])
            except SettingsError:
                raise
            except Exception as e:  # noqa: BLE001 - invalid regex, missing key, unexpected type…
                raise SettingsError(f"veille {name} : {type(e).__name__}: {e}") from None
            if not w["thresholds"]:
                raise SettingsError(f"veille {name} : aucun palier de prix (ni dans la veille, ni à la racine)")
            if not any(k in (cfg.get("sources") or {}) for k in w["sources"]):
                raise SettingsError(f"veille {name} : aucune source (leboncoin ou eBay)")
        cfg["_path"] = "<settings>"
        return cfg

    # ---- writing
    def _write(self, doc: CommentedMap, expected_version: Optional[str], reason: str,
               raw_text: Optional[str] = None) -> dict[str, Any]:
        """`raw_text`: content to write as-is (restore), `doc` then being only its parsed version."""
        cfg = self.validate(doc)
        text = raw_text if raw_text is not None else self._dump(doc)
        with self.lock:
            current, cur_ver = self._read()
            if expected_version and expected_version != cur_ver:
                raise ConflictError("les réglages ont changé depuis leur ouverture : recharger l'onglet")
            if text == self._dump(current) or text == self._text():    # nothing changed (comments included)
                snap = self.snapshot()
                snap["reload"] = "unchanged"
                return snap
            self._backup(reason)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            os.replace(tmp, self.path)
        log.info("settings saved (%s)", reason)
        reload = self.on_change(cfg) if self.on_change else "none"
        snap = self.snapshot()
        snap["reload"] = reload if isinstance(reload, str) else "applied"
        return snap

    def _backup(self, reason: str) -> None:
        os.makedirs(self.history_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        ascii_reason = unicodedata.normalize("NFKD", reason).encode("ascii", "ignore").decode()
        slug = re.sub(r"[^a-z0-9-]+", "-", ascii_reason.lower()).strip("-")[:40]
        shutil.copyfile(self.path, os.path.join(self.history_dir, f"{stamp}_{slug}.yaml"))
        for old in self.history()[HISTORY_KEEP:]:
            try:
                os.remove(os.path.join(self.history_dir, old["file"]))
            except OSError:
                pass

    def history(self) -> list[dict[str, str]]:
        if not os.path.isdir(self.history_dir):
            return []
        files = sorted((f for f in os.listdir(self.history_dir) if f.endswith(".yaml")), reverse=True)
        out = []
        for f in files:
            stamp, _, reason = f[:-5].partition("_")
            try:
                when = datetime.strptime(stamp, "%Y%m%d-%H%M%S-%f").isoformat(timespec="seconds")
            except ValueError:
                when = stamp
            out.append({"file": f, "when": when, "reason": reason.replace("-", " ")})
        return out

    def history_text(self, file: str) -> str:
        if os.path.basename(file) != file or not file.endswith(".yaml"):
            raise SettingsError("fichier d'historique invalide")
        path = os.path.join(self.history_dir, file)
        if not os.path.exists(path):
            raise SettingsError("version introuvable")
        with open(path, encoding="utf-8") as f:
            return f.read()

    # ---- operations exposed to the API
    def save_watch(self, name: str, payload: dict[str, Any], create: bool = False) -> dict[str, Any]:
        doc, _ = self._read()
        watches = doc.setdefault("watches", CommentedMap())
        if create:
            if not WATCH_NAME.fullmatch(name):
                raise SettingsError("nom de veille invalide : minuscules, chiffres, - et _ (32 caractères max)")
            if name in watches:
                raise SettingsError(f"la veille « {name} » existe déjà")
        elif name not in watches:
            raise SettingsError(f"veille inconnue : {name}")
        if payload.get("yaml") is not None:
            w = self._load_yaml(str(payload["yaml"]), f"veille {name}", indent=4)
            if not isinstance(w, dict):
                raise SettingsError(f"veille {name} : une table YAML est attendue")
        else:
            w = self.build_watch(doc, name, payload.get("form") or {})
        watches[name] = w
        return self._write(doc, payload.get("version"), f"{'création' if create else 'veille'} {name}")

    def duplicate_watch(self, name: str, new_name: str, version: Optional[str]) -> dict[str, Any]:
        doc, _ = self._read()
        watches = doc.get("watches") or {}
        if name not in watches:
            raise SettingsError(f"veille inconnue : {name}")
        if not WATCH_NAME.fullmatch(new_name) or new_name in watches:
            raise SettingsError(f"nom « {new_name} » invalide ou déjà pris")
        w = copy.deepcopy(watches[name])
        w["title"] = f"{w.get('title') or name} (copie)"
        w["enabled"] = False                            # a copy does not scan until it has been reviewed
        watches[new_name] = w
        return self._write(doc, version, f"copie {name} vers {new_name}")

    def set_enabled(self, name: str, enabled: bool, version: Optional[str]) -> dict[str, Any]:
        doc, _ = self._read()
        w = (doc.get("watches") or {}).get(name)
        if w is None:
            raise SettingsError(f"veille inconnue : {name}")
        if enabled:
            w.pop("enabled", None)
        else:
            w["enabled"] = False
        return self._write(doc, version, f"{'activation' if enabled else 'désactivation'} {name}")

    def delete_watch(self, name: str, version: Optional[str]) -> dict[str, Any]:
        doc, _ = self._read()
        watches = doc.get("watches") or {}
        if name not in watches:
            raise SettingsError(f"veille inconnue : {name}")
        del watches[name]
        return self._write(doc, version, f"suppression {name}")

    def save_global(self, payload: dict[str, Any]) -> dict[str, Any]:
        doc, _ = self._read()
        form = payload.get("form") or {}
        for sec, fields in GLOBAL_FIELDS.items():
            vals = _section(fields, form.get(sec), sec)
            cur = doc.get(sec)
            keep = {k: v for k, v in (plain(cur) or {}).items() if k not in fields} if isinstance(cur, dict) else {}
            assign(doc, sec, {**keep, **vals} or None)
        assign(doc, "thresholds", _thresholds(form.get("thresholds"), "thresholds") or None)
        return self._write(doc, payload.get("version"), "réglages généraux")

    def restore(self, file: Optional[str], version: Optional[str]) -> dict[str, Any]:
        """Go back to a history version, or to the image's config.yaml if `file` is empty."""
        if file:
            if os.path.basename(file) != file or not file.endswith(".yaml"):
                raise SettingsError("fichier d'historique invalide")
            src = os.path.join(self.history_dir, file)
            reason = f"restauration {file[:19]}"
        else:
            src = self.image_path or ""
            reason = "réinitialisation depuis l'image"
        if not src or not os.path.exists(src):
            raise SettingsError("version introuvable")
        with open(src, encoding="utf-8") as f:
            raw = f.read()
        snap = self._write(self._load_yaml(raw, "restauration"), version, reason, raw_text=raw)
        if not file and self.image_path:
            with open(self.seed_file, "w") as f:
                f.write(_file_sha(self.image_path))
            snap["image_changed"] = False
        return snap

    def preview_watch(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Normalized watch (root defaults applied) from a draft, without writing anything."""
        doc, _ = self._read()
        if payload.get("yaml") is not None:
            w = self._load_yaml(str(payload["yaml"]), f"veille {name}", indent=4)
        else:
            w = self.build_watch(doc, name, payload.get("form") or {})
        doc = copy.deepcopy(doc)
        w = copy.deepcopy(w)
        if isinstance(w, dict):
            w.pop("enabled", None)                       # a disabled draft still gets tested
        doc.setdefault("watches", CommentedMap())
        doc["watches"] = CommentedMap([(name, w)])
        return self.validate(doc)["watches"][name]


def live_config_path(db_path: Optional[str] = None) -> str:
    """Path of the live copy: $LISTINGWATCHER_LIVE_CONFIG, otherwise config.yaml next to the database."""
    explicit = env("LISTINGWATCHER_LIVE_CONFIG")
    if explicit:
        return explicit
    db_path = db_path or env("LISTINGWATCHER_DB", "data/listingwatcher.sqlite")
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "config.yaml")
