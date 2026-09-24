"""Extraction d'un modèle canonique à partir d'un titre (et d'une description) bruités.

C'est ici que se joue la qualité de l'outil : chaque règle correspond à un piège
rencontré sur de vraies annonces. Ordre d'évaluation :
  1. rejets durs sur le texte (disque mort, SSD, externe, 2,5", NAS vendu avec disques, accessoire)
  2. références extraites et classées (catalogue de config puis règles structurelles)
  3. mots-clés SAS / SMR
  4. cohérence des capacités (≠ 8 To → rejet ; 3 capacités et plus → générique)
  5. famille déduite d'un mot-clé si aucune référence (IronWolf, Exos…)
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional

from .models import ModelInfo

# --------------------------------------------------------------------------- texte


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm(s: str) -> str:
    """Minuscules, sans accents, espaces compactés, apostrophes/guillemets unifiés."""
    s = strip_accents(s or "")
    for a, b in (("’", "'"), ("“", '"'), ("”", '"'), ("″", '"'), ("''", '"'),
                 ("—", "-"), ("–", "-")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip().lower()


def _kw_regex(words: list[str]) -> re.Pattern:
    parts = []
    for w in words:
        w = norm(w)
        # frontière de mot uniquement là où le mot commence/finit par un alphanumérique
        left = r"(?<![a-z0-9])" if re.match(r"[a-z0-9]", w) else ""
        right = r"(?![a-z0-9])" if re.search(r"[a-z0-9]$", w) else ""
        parts.append(left + re.escape(w) + right)
    return re.compile("|".join(parts)) if parts else re.compile(r"(?!x)x")


_NEGATION = re.compile(r"(aucun|aucune|sans|pas de|zero|no|ohne|keine|non|0)\s*$")

# --------------------------------------------------------------------------- références

# Chaque motif tolère un espace ou un tiret aux points de coupure habituels.
_SEAGATE = re.compile(r"(?<![A-Z0-9])ST[ \-]?(\d{4,5})[ \-]?([A-Z]{2})[ \-]?(\d{3,4}[A-Z]?)(?![A-Z0-9])")
_WD = re.compile(r"(?<![A-Z0-9])WD[ \-]?(\d{2,4})[ \-]?([A-Z]{4})(?![A-Z0-9])")
_HGST = re.compile(
    r"(?<![A-Z0-9])(HU[HS][ \-]?\d{6}[ \-]?AL[ENS45]\d{3}|HUS[ \-]?\d{3}T\d{1,2}T[ \-]?AL[ENS45]\d[A-Z0-9]\d)(?![A-Z0-9])"
)
_TOSHIBA_MG = re.compile(
    r"(?<![A-Z0-9])(M[GN])(\d{2})[ \-]?([ASN])([CD])A[ \-]?(\d{1,2}T|\d{3})([A-Z0-9]{0,2})(?![A-Z0-9])"
)
_TOSHIBA_HDW = re.compile(r"(?<![A-Z0-9])(HDW[A-Z])(\d)([0-9A-Z])(\d)([A-Z0-9]{0,5})(?![A-Z0-9])")

_SEAGATE_CLASS = {
    "VN": ("IronWolf", "accept"),
    "NE": ("IronWolf Pro", "accept"),
    "NT": ("IronWolf Pro", "accept"),
    "NM": ("Exos", "accept"),
    "DM": ("Barracuda", "smr"),
    "AS": ("Archive", "smr"),
    "VX": ("SkyHawk", "low_tier"),
    "VE": ("SkyHawk AI", "low_tier"),
}
_WD_CLASS = {
    "EFZZ": ("WD Red Plus", "accept"), "EFPX": ("WD Red Plus", "accept"),
    "EFAX": ("WD Red Plus", "accept"), "EFBX": ("WD Red Plus", "accept"),
    "FFBX": ("WD Red Pro", "accept"), "FFWX": ("WD Red Pro", "accept"),
    "FRYZ": ("WD Gold", "accept"), "FRYX": ("WD Gold", "accept"), "FRXZ": ("WD Gold", "accept"),
    "PURZ": ("WD Purple", "low_tier"), "PUZX": ("WD Purple", "low_tier"),
    "PURX": ("WD Purple", "low_tier"), "PURU": ("WD Purple", "low_tier"),
    "EDAZ": ("WD Blue/Elements", "smr"), "EAZZ": ("WD Blue", "smr"), "EZAZ": ("WD Blue", "smr"),
    "EMAZ": ("WD white label", "unverified"), "EMZZ": ("WD white label", "unverified"),
}
_HGST_IFACE = {"E": "accept", "N": "4kn", "5": "sas", "4": "sas_4kn"}


def _wd_capacity(digits: str) -> Optional[float]:
    if len(digits) == 2:
        return float(digits[0])            # WD80 → 8, WD82 → 8, WD40 → 4
    if len(digits) == 3:
        two = int(digits[:2])
        return float(two) if two >= 10 else float(digits[0])   # WD101 → 10, WD141 → 14
    if len(digits) == 4:
        return float(digits[0])            # WD8003 → 8, WD4003 → 4
    return None


def _hgst_capacity(ref: str) -> Optional[float]:
    m = re.search(r"T(\d{1,2})T", ref)
    if m:
        return float(m.group(1))           # HUS728T8TALE6L4 → 8
    m = re.search(r"HU[HS](\d{6})", ref)
    if m:
        drive = int(m.group(1)[4:6])
        return drive / 10 if drive >= 40 else float(drive)     # 728080 → 8.0 ; 721008 → 8 ; 721010 → 10
    return None


class RefHit:
    __slots__ = ("ref", "family", "brand", "verdict", "reason", "capacity")

    def __init__(self, ref, family, brand, verdict, reason=None, capacity=None):
        self.ref, self.family, self.brand = ref, family, brand
        self.verdict, self.reason, self.capacity = verdict, reason, capacity

    def __repr__(self):
        return f"RefHit({self.ref} {self.family} {self.verdict} {self.reason} {self.capacity})"


class Catalog:
    """Catalogue issu de config.yaml (accept / low_tier / reject) + règles structurelles."""

    def __init__(self, models_cfg: dict[str, Any] | None = None, target_tb: float = 8):
        models_cfg = models_cfg or {}
        self.target_tb = float(target_tb)
        self.accept: dict[str, tuple[str, str]] = {}
        self.low_tier: dict[str, tuple[str, str]] = {}
        self.reject: dict[str, str] = {}
        for fam in models_cfg.get("accept", []) or []:
            for r in fam.get("refs", []):
                self.accept[r.upper()] = (fam["family"], fam.get("brand", ""))
        for fam in models_cfg.get("low_tier", []) or []:
            for r in fam.get("refs", []):
                self.low_tier[r.upper()] = (fam["family"], fam.get("brand", ""))
        for reason, refs in (models_cfg.get("reject", {}) or {}).items():
            for r in refs or []:
                self.reject[r.upper()] = str(reason)

    # ---- extraction
    def extract(self, text: str) -> list[RefHit]:
        up = strip_accents(text or "").upper()
        hits: list[RefHit] = []
        for m in _SEAGATE.finditer(up):
            hits.append(self._seagate(m))
        for m in _WD.finditer(up):
            hits.append(self._wd(m))
        for m in _HGST.finditer(up):
            hits.append(self._hgst(m))
        for m in _TOSHIBA_MG.finditer(up):
            hits.append(self._toshiba_mg(m))
        for m in _TOSHIBA_HDW.finditer(up):
            hits.append(self._toshiba_hdw(m))
        seen, out = set(), []
        for h in hits:
            if h.ref not in seen:
                seen.add(h.ref)
                out.append(h)
        return out

    def _from_catalog(self, ref: str, brand: str, capacity, fallback_family: str | None):
        if ref in self.accept:
            fam, b = self.accept[ref]
            return RefHit(ref, fam, b or brand, "accept", None, capacity)
        if ref in self.low_tier:
            fam, b = self.low_tier[ref]
            return RefHit(ref, fam, b or brand, "accept", "low_tier", capacity)
        if ref in self.reject:
            return RefHit(ref, fallback_family, brand, "reject", self.reject[ref], capacity)
        return None

    def _seagate(self, m: re.Match) -> RefHit:
        digits, cls, tail = m.group(1), m.group(2), m.group(3)
        ref = f"ST{digits}{cls}{tail}"
        cap = int(digits) / 1000
        fam, kind = _SEAGATE_CLASS.get(cls, (None, "unknown"))
        hit = self._from_catalog(ref, "Seagate", cap, fam)
        if hit:
            return hit
        if kind == "accept":
            # famille connue mais référence hors catalogue : vérifier l'interface avant d'acheter
            return RefHit(ref, fam, "Seagate", "accept", "ref_unverified", cap)
        if kind == "low_tier":
            return RefHit(ref, fam, "Seagate", "accept", "low_tier", cap)
        if kind == "smr":
            return RefHit(ref, fam, "Seagate", "reject", "smr", cap)
        return RefHit(ref, None, "Seagate", "accept", "ref_unverified", cap)

    def _wd(self, m: re.Match) -> RefHit:
        digits, suffix = m.group(1), m.group(2)
        ref = f"WD{digits}{suffix}"
        cap = _wd_capacity(digits)
        fam, kind = _WD_CLASS.get(suffix, (None, "unknown"))
        hit = self._from_catalog(ref, "WD", cap, fam)
        if hit:
            return hit
        if kind == "accept":
            return RefHit(ref, fam, "WD", "accept", "ref_unverified", cap)
        if kind == "low_tier":
            return RefHit(ref, fam, "WD", "accept", "low_tier", cap)
        if kind == "smr":
            return RefHit(ref, fam, "WD", "reject", "smr", cap)
        return RefHit(ref, fam, "WD", "accept", "ref_unverified", cap)

    def _hgst(self, m: re.Match) -> RefHit:
        ref = re.sub(r"[ \-]", "", m.group(1))
        cap = _hgst_capacity(ref)
        hit = self._from_catalog(ref, "WD/HGST", cap, "Ultrastar")
        if hit:
            return hit
        iface = re.search(r"AL([ENS45])", ref).group(1)
        kind = _HGST_IFACE.get(iface, "unverified")
        if kind in ("4kn", "sas", "sas_4kn"):
            return RefHit(ref, "Ultrastar", "WD/HGST", "reject", kind, cap)
        return RefHit(ref, "Ultrastar", "WD/HGST", "accept", "ref_unverified", cap)

    def _toshiba_mg(self, m: re.Match) -> RefHit:
        series, gen, iface, dd, capcode, suffix = m.groups()
        ref = f"{series}{gen}{iface}{dd}A{capcode}{suffix}"
        cap = float(capcode[:-1]) if capcode.endswith("T") else int(capcode) / 100
        fam = "Toshiba MG" if series == "MG" else "Toshiba N300"
        hit = self._from_catalog(ref, "Toshiba", cap, fam)
        if hit:
            return hit
        if iface == "S":
            return RefHit(ref, fam, "Toshiba", "reject", "sas", cap)
        if suffix.startswith("A"):
            return RefHit(ref, fam, "Toshiba", "reject", "4kn", cap)
        return RefHit(ref, fam, "Toshiba", "accept", "ref_unverified", cap)

    def _toshiba_hdw(self, m: re.Match) -> RefHit:
        prefix, a, mid, c, suffix = m.groups()
        cap = float(mid) if mid.isdigit() else None
        fam = {"HDWG": "Toshiba N300", "HDWN": "Toshiba N300", "HDWT": "Toshiba S300",
               "HDWF": "Toshiba X300", "HDWE": "Toshiba X300", "HDWR": "Toshiba X300"}.get(prefix)
        short = f"{prefix}{a}{mid}{c}"
        hit = self._from_catalog(short, "Toshiba", cap, fam) or self._from_catalog(short + suffix, "Toshiba", cap, fam)
        if hit:
            return hit
        if fam == "Toshiba S300":
            return RefHit(short, fam, "Toshiba", "accept", "low_tier", cap)
        return RefHit(short, fam, "Toshiba", "accept", "ref_unverified", cap)


# --------------------------------------------------------------------------- mots-clés

DEFAULT_KEYWORDS: dict[str, list[str]] = {
    "dead": ["pour pieces", "pour piece", "piece detachee", "pieces detachees", "hs", "h.s", "ne fonctionne pas",
             "ne fonctionne plus", "defectueux", "defectueuse", "en panne", "for parts", "defekt", "not working",
             "ne demarre pas", "ne demarre plus", "non fonctionnel", "a reparer", "secteurs defectueux",
             "secteur defectueux", "bad sectors", "faulty", "kaputt", "clique"],
    "smr": ["barracuda", "archive", "blue", "compute", "smr", "shingled"],
    "sas": ["sas", "scsi", "12gb/s", "12 gb/s", "12gbps"],
    "external": ["externe", "external", "usb", "usb2", "usb3", "usb-c", "usbc", "thunderbolt", "my book", "mybook", "wd book",
                 "book", "elements", "expansion",
                 "backup plus", "easystore", "desktop drive", "game drive", "xbox", "playstation", "ps4", "ps5",
                 "portable", "g-raid", "g-tech", "g-technology", "g-drive", "gdrive", "quattro", "freecom", "fantec", "mercury elite", "lacie", "extern"],
    "bundle": ["serveur nas", "nas synology", "nas qnap", "nas ugreen", "nas asustor", "nas terramaster", "nas buffalo",
               "nas netgear", "nas wd", "synology ds", "qnap ts", "rackstation", "diskstation", "terastation", "readynas",
               "linkstation", "my cloud", "mycloud", "drobo", "avec disques", "avec disque", "fourni avec", "nas complet",
               "2 baies", "4 baies", "5 baies", "6 baies", "8 baies", "nvr", "enregistreur", "dvr", "baie de brassage"],
    "other_device": ["adaptateur", "caddy", "dock", "docking", "hub", "switch", "onduleur", "cable", "carte raid",
                     "carte controleur", "controleur", "cle usb", "tiroir", "rack seul", "boitier seul",
                     "boitier vide", "nvme", "m.2"],
}

# Mots-clés de famille quand la référence manque : (regex, famille, marque, low_tier)
_FAMILY_HINTS: list[tuple[re.Pattern, str, str, bool]] = [
    (re.compile(r"ironwolf ?pro"), "IronWolf Pro", "Seagate", False),
    (re.compile(r"iron ?wolf"), "IronWolf", "Seagate", False),
    (re.compile(r"\bexos\b|enterprise capacity|constellation es"), "Exos", "Seagate", False),
    (re.compile(r"\bred ?pro\b"), "WD Red Pro", "WD", False),
    (re.compile(r"\bred ?plus\b"), "WD Red Plus", "WD", False),
    (re.compile(r"\bwd ?red\b|western ?digital[^.]{0,20}\bred\b|\bred\b[^.]{0,20}\b(wd|western)"), "WD Red", "WD", False),
    (re.compile(r"\bwd ?gold\b|western ?digital[^.]{0,20}\bgold\b|\bgold\b[^.]{0,20}\b(wd|western)"), "WD Gold", "WD", False),
    (re.compile(r"ultrastar|\bhgst\b|\bhitachi\b"), "Ultrastar", "WD/HGST", False),
    (re.compile(r"\bn300\b"), "Toshiba N300", "Toshiba", False),
    (re.compile(r"\bmg0?\d\b|toshiba[^.]{0,20}(enterprise|entreprise)"), "Toshiba MG", "Toshiba", False),
    (re.compile(r"sky ?hawk"), "SkyHawk", "Seagate", True),
    (re.compile(r"\bpurple\b"), "WD Purple", "WD", True),
]
_BRAND_HINTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bseagate\b|\bsegate\b"), "Seagate"),
    (re.compile(r"\bwestern ?digital\b|\bwd\b"), "WD"),
    (re.compile(r"\btoshiba\b"), "Toshiba"),
    (re.compile(r"\bhgst\b|\bhitachi\b"), "WD/HGST"),
]

_CAP_TB = re.compile(r"(?<![\d,.])(\d{1,2})\s?(?:to|tb|t)(?![a-z0-9])")
_CAP_GB = re.compile(r"(?<![\d,.])(\d{3,5})\s?(?:go|gb|gigas?)(?![a-z])")
_COMMA_LIST = re.compile(r"\d\s?,\s?\d\s?,\s?\d")
_SMALL_FF = re.compile(r'2[.,]5\s?(?:"|pouces?|inch|zoll|po\b)|2"\s?1/2|2\s?1/2\s?(?:"|pouces?)')
_SSD = re.compile(r"\bssd\b|\bnvme\b")
_HDD = re.compile(r"\bhdd\b|disque dur|hard ?drive|festplatte|\bsata\b")
_HOURS = re.compile(
    r"(?<![\d,.])(\d{1,3}(?:[ .]\d{3})+|\d{3,6})\s?(?:h\b|heures?\b|hours?\b|hrs\b|std\b|stunden|betriebsstunden)"
)
_POWER_ON = re.compile(r"power[- ]?on[- ]?hours?\D{0,12}(\d[\d ]{2,7})")
_QTY = [
    re.compile(r"\blot de (\d{1,2})\b"),
    re.compile(r"(?<![\d,.])(\d{1,2})\s?x\s?(?=\d{1,2}\s?(?:to|tb|t)\b)"),
    re.compile(r"(?:to|tb)\s?x\s?(\d{1,2})\b"),
    re.compile(r"(?<![\d,.])(\d{1,2})\s?(?:disques?|pieces?|pcs|unites?|stuck|drives?|hdd)\b"),
    re.compile(r"(?<![\d,.\"'])(\d{1,2})\s?x?\s+(?:seagate|segate|wd|western|toshiba|hgst|hitachi|exos|ironwolf|ultrastar)\b"),
]
_NAS_MODEL = re.compile(r"\b(?:ds|rs|dxp)\d{3,4}[a-z+]*\b|\bts-?\d{3,4}[a-z]*\b")
# nom de gamme Seagate Exos qui encode la capacité : 7E8 = 8 To, 7E2000 = 2 To, 7E10 = 10 To, X16 = 16 To
_EXOS_NAME = re.compile(r"\b7e\s?(\d{1,2})(000)?\b|\bexos\s?x(\d{2})\b")
# « soit 8 To », « total 8 To », « (8 To au total) », « = 8 To » : capacité totale d'un lot, pas par disque
_TOTAL_CTX = re.compile(r"(?:\btotal\b|\bsoit\b|\bcumul[ée]*\b|=)\s*[:(]?\s*(\d{1,2})\s?(?:to|tb)\b|(\d{1,2})\s?(?:to|tb)\s+(?:au total|en tout|cumul[ée]s?)")
_GENERIC = re.compile(
    r"plusieurs (modeles|capacites|tailles|disques|references)|multiples? capacites|differentes capacites"
    r"|au choix|capacites? disponibles"
)


def _find_kw(rx: re.Pattern, text: str, negation_aware: bool = False) -> Optional[str]:
    for m in rx.finditer(text):
        if negation_aware:
            before = text[max(0, m.start() - 25):m.start()]
            after = text[m.end():m.end() + 12]
            if _NEGATION.search(before) or re.match(r"\s*[:=]?\s*0(?![\d,.])", after):
                continue
        return m.group(0)
    return None


class Classifier:
    def __init__(self, models_cfg: dict | None = None, keywords: dict | None = None, target_tb: float = 8):
        self.catalog = Catalog(models_cfg, target_tb)
        self.target = float(target_tb)
        kws = {**DEFAULT_KEYWORDS, **(keywords or {})}
        self.rx = {k: _kw_regex(v) for k, v in kws.items()}

    # ------------------------------------------------------------------ API
    def classify(self, title: str, description: str = "", condition_code: str = "") -> ModelInfo:
        t = norm(title)
        d = norm(description)
        full = f"{t} {d}".strip()
        info = ModelInfo(verdict="accept")

        # 1. rejets durs
        if condition_code == "parts" or _find_kw(self.rx["dead"], full, negation_aware=True):
            return self._reject(info, "dead")
        if _SSD.search(t) and not re.search(r"\bhdd\b", t):
            return self._reject(info, "ssd")
        if _find_kw(self.rx["external"], t):
            return self._reject(info, "external")
        if _SMALL_FF.search(t):
            return self._reject(info, "2.5in")
        if _find_kw(self.rx["bundle"], t) or _NAS_MODEL.search(t):
            return self._reject(info, "nas_bundle")
        if _find_kw(self.rx["other_device"], t):
            return self._reject(info, "other_device")

        # 2. références
        hits = self.catalog.extract(f"{title}\n{description}")
        target_hits = [h for h in hits if h.capacity is None or abs(h.capacity - self.target) < 0.01]
        good = [h for h in target_hits if h.verdict == "accept"]
        bad = [h for h in target_hits if h.verdict == "reject"]
        chosen: Optional[RefHit] = None
        if good:
            # préférer une réf. du catalogue (sans flag) à une réf. seulement structurelle
            good.sort(key=lambda h: (h.reason is not None, h.reason == "low_tier"))
            chosen = good[0]
        elif bad:
            info.model, info.family, info.brand = bad[0].ref, bad[0].family, bad[0].brand
            return self._reject(info, bad[0].reason or "rejected_ref")
        elif hits:
            # référence(s) trouvée(s) mais d'une autre capacité
            info.model, info.family, info.brand = hits[0].ref, hits[0].family, hits[0].brand
            info.attrs["capacities_tb"] = sorted({h.capacity for h in hits if h.capacity})
            rejected = [h for h in hits if h.verdict == "reject"]
            if rejected:
                # « 8To » dans le titre mais la seule référence citée est un ST6000AS0002 (6 To, SMR) :
                # la référence l'emporte sur le texte
                info.model, info.family, info.brand = rejected[0].ref, rejected[0].family, rejected[0].brand
                return self._reject(info, rejected[0].reason or "rejected_ref")
            if not self._text_has_target(t):
                return self._reject(info, "capacity_mismatch")

        # 3. SAS / SMR par mots-clés (titre = rejet ; description = rejet seulement sans réf. SATA acceptée)
        sas_t = _find_kw(self.rx["sas"], t)
        sas_d = _find_kw(self.rx["sas"], d)
        if sas_t or (sas_d and not (chosen and chosen.reason is None)):
            return self._reject(info, "sas")
        smr_t = _find_kw(self.rx["smr"], t)
        if smr_t and not (smr_t == "smr" and re.search(r"\bcmr\b", t)):
            return self._reject(info, "smr")

        # 4. capacités
        caps = self._capacities(full)
        qty = self._quantity(t)
        if qty > 1 and _TOTAL_CTX.search(t) and not any(abs(c - self.target) < 0.01 for c in caps):
            # « 8x disque dur (total 8 To) » : le 8 To n'est que la somme du lot
            return self._reject(info, "capacity_mismatch")
        if chosen and chosen.capacity:
            caps.add(chosen.capacity)
        if _COMMA_LIST.search(t) or _GENERIC.search(t):
            return self._reject(info, "generic")
        if not caps and not chosen:
            # ni capacité ni référence : on ne sait même pas si c'est un 8 To
            return self._reject(info, "no_capacity")
        if caps:
            if self.target not in caps and not chosen:
                info.attrs["capacities_tb"] = sorted(caps)
                return self._reject(info, "capacity_mismatch")
            others = {c for c in caps if abs(c - self.target) >= 0.01}
            # « 16 disques de 500 Go » : 8 To n'est que le total du lot
            if qty > 1 and any(abs(c * qty - self.target) < 0.01 for c in others):
                info.attrs["capacities_tb"] = sorted(caps)
                return self._reject(info, "capacity_mismatch")
            # "2x 8To = 16To" : la capacité totale d'un lot n'est pas une capacité concurrente
            others = {c for c in others if not (qty > 1 and abs(c - self.target * qty) < 0.01)}
            if len(others) >= 2:
                info.attrs["capacities_tb"] = sorted(caps)
                return self._reject(info, "generic")
            if others:
                info.flags.append("multi_capacity")
        info.attrs["capacities_tb"] = sorted(caps)

        # 5. modèle / famille
        if chosen:
            info.model, info.family, info.brand = chosen.ref, chosen.family, chosen.brand
            if chosen.reason:
                info.flags.append(chosen.reason)          # low_tier | ref_unverified
        else:
            fam = self._family_hint(full)
            if fam:
                info.family, info.brand, low = fam
                info.flags.append("ref_missing")
                if low:
                    info.flags.append("low_tier")
            else:
                info.flags.append("model_unknown")
                for rx, brand in _BRAND_HINTS:
                    if rx.search(full):
                        info.brand = brand
                        break

        # 6. lot, heures
        if qty > 1:
            info.quantity = qty
            info.flags.append("lot")
        elif re.search(r"\blot\b", t):
            info.flags.append("lot")
        info.attrs["smart_hours"] = self._hours(full)
        return info

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _reject(info: ModelInfo, reason: str) -> ModelInfo:
        info.verdict = "reject"
        info.reasons.append(reason)
        return info

    def _text_has_target(self, t: str) -> bool:
        return any(abs(c - self.target) < 0.01 for c in self._capacities(t))

    @staticmethod
    def _capacities(text: str) -> set[float]:
        """Capacités par disque citées dans le texte. Une capacité annoncée comme total d'un lot
        (« soit 8 To », « total 8 To ») ne compte pas ; le nom de gamme Exos (7E8) n'est qu'un
        indice, utilisé seulement quand rien d'autre n'est écrit."""
        caps = {float(m.group(1)) for m in _CAP_TB.finditer(text)}
        caps |= {int(m.group(1)) / 1000 for m in _CAP_GB.finditer(text) if int(m.group(1)) >= 100}
        totals = {float(m.group(1) or m.group(2)) for m in _TOTAL_CTX.finditer(text)}
        for tcap in totals:
            if sum(1 for m in _CAP_TB.finditer(text) if float(m.group(1)) == tcap) <= 1:
                caps.discard(tcap)
        if not caps:
            for m in _EXOS_NAME.finditer(text):
                caps.add(float(m.group(3) or m.group(1)))
        return {c for c in caps if 0.1 <= c <= 30}

    @staticmethod
    def _quantity(t: str) -> int:
        for rx in _QTY:
            m = rx.search(t)
            if m:
                q = int(m.group(1))
                if 2 <= q <= 20:
                    return q
        return 1

    @staticmethod
    def _hours(text: str) -> Optional[int]:
        best = None
        for m in list(_HOURS.finditer(text)) + list(_POWER_ON.finditer(text)):
            try:
                v = int(re.sub(r"[ .]", "", m.group(1)))
            except ValueError:
                continue
            if 100 <= v <= 120000:
                best = v if best is None else max(best, v)
        return best

    @staticmethod
    def _family_hint(text: str):
        for rx, fam, brand, low in _FAMILY_HINTS:
            if rx.search(text):
                return fam, brand, low
        return None
