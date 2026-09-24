"""PC profile (mini PC, tower, laptop…): a **performance** floor rather than an exact reference.

The CPU is extracted from the title, then the description (Intel Core iN-XXXX, Pentium/Celeron, AMD Ryzen)
and reduced to a score: generation + tier bonus (i3 0, i5 +2, i7 +3, i9 +4). An i5-8400 is worth 10,
like an i7-7700 or an i3-10100. `min_cpu` (e.g. « i5-8400 ») sets the floor. RAM and storage are
read in GB, compared to `min_ram_gb` / `min_storage_gb`, and flagged when missing.

The family (`family`) is the chassis, recognized by keywords (M720q, OptiPlex 3060 Micro…); the
"reference" (`model`) is the CPU, which gives a label « Lenovo M720q i5-8500T » and a market
median per CPU, then per chassis.

Configuration (watches.<watch>.profile.pc):
  min_cpu: i5-8400                 # floor (empty = none); an unknown CPU is not rejected but flagged
  min_ram_gb: 8                    # 0 = no minimum
  min_storage_gb: 0
  require_family: false            # true = reject chassis outside the catalog
  families: [{name: Lenovo M720q, any: [m720q], gen: 8}, …]   # gen: CPU generation of the chassis, for « M720q i5 » without a number
  reject: [...]                    # title + description (default: the hdd profile's "dead" keywords)
  reject_title: [portable, laptop…]
  reject_form: [sff, tour, tower, mt]   # rejected form factors (title), unless a mini_any word is present
  mini_any: [micro, mini, tiny, usff]
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from ..models import ModelInfo
from ..normalize import DEFAULT_KEYWORDS, _find_kw, _kw_regex, norm
from .base import Profile

# ----------------------------------------------------------------------------- CPU

_TIER_POINTS = {"celeron": -3, "pentium": -2, "athlon": -2, "3": 0, "5": 2, "7": 3, "9": 4}
_INTEL = re.compile(r"(?<![a-z0-9])(?:core\s*)?i([3579])[\s\-]?(\d{4,5})([a-z]{0,2})(?![a-z0-9])")
_INTEL_GEN = re.compile(
    r"(?<![a-z0-9])(?:core\s*)?i([3579])(?![a-z0-9\-])[^.,;()]{0,25}?(\d{1,2})\s?(?:e|eme|th|st|nd|rd)?\s?(?:gen\b|generation)"
)
_LOWEND = re.compile(r"\b(celeron|pentium|athlon)\b")
_INTEL_TIER = re.compile(r"(?<![a-z0-9])(?:core\s*)?i([3579])(?![a-z0-9\-])")     # bare « i5 », no number, no generation
_SEP = re.compile(r"[-|,/;•+()]")
_RYZEN = re.compile(r"\bryzen\s*([3579])\s*(pro\s*)?(\d{4})([a-z]{0,2})(?![a-z0-9])")
_RYZEN_GEN = {"1": 7, "2": 8, "3": 9, "4": 10, "5": 11, "6": 12, "7": 13, "8": 14}


@dataclass
class Cpu:
    label: str
    tier: str                      # 3 | 5 | 7 | 9 | celeron | pentium | athlon
    gen: Optional[int]

    @property
    def score(self) -> Optional[int]:
        if self.gen is None:
            return None
        return self.gen + _TIER_POINTS.get(self.tier, 0)


def parse_cpus(text: str) -> list[Cpu]:
    """Every CPU quoted in a normalized text, in order."""
    out: list[Cpu] = []
    seen: set[str] = set()
    for m in _INTEL.finditer(text):
        tier, num, suffix = m.group(1), m.group(2), m.group(3)
        gen = int(num[:2]) if len(num) == 5 else int(num[0])
        label = f"i{tier}-{num}{suffix.upper()}"
        if label not in seen:
            seen.add(label); out.append(Cpu(label, tier, gen))
    for m in _INTEL_GEN.finditer(text):
        tier, gen = m.group(1), int(m.group(2))
        if not any(c.tier == tier and c.gen == gen for c in out) and 2 <= gen <= 20:
            label = f"i{tier} {gen}e gén."
            if label not in seen:
                seen.add(label); out.append(Cpu(label, tier, gen))
    for m in _RYZEN.finditer(text):
        tier, pro, num, suffix = m.group(1), m.group(2), m.group(3), m.group(4)
        label = f"Ryzen {tier}{' PRO' if pro else ''} {num}{suffix.upper()}"
        if label not in seen:
            seen.add(label); out.append(Cpu(label, tier, _RYZEN_GEN.get(num[0])))
    for m in _LOWEND.finditer(text):
        name = m.group(1)
        if name not in seen:
            seen.add(name); out.append(Cpu(name.capitalize(), name, None))
    for m in _INTEL_TIER.finditer(text):
        tier = m.group(1)
        if not any(c.tier == tier for c in out):
            out.append(Cpu(f"i{tier}", tier, None))
    return out


def best_cpu(cpus: list[Cpu]) -> Optional[Cpu]:
    """The most powerful CPU quoted (a listing « i5 ou i7 au choix »); otherwise the first one."""
    if not cpus:
        return None
    scored = [c for c in cpus if c.score is not None]
    return max(scored, key=lambda c: c.score) if scored else cpus[0]


# ----------------------------------------------------------------------------- RAM / storage

_GB = re.compile(r"(?<![\d,.])(\d{1,4})\s?(?:go|gb)(?![a-z])")
_TB = re.compile(r"(?<![\d,.])(\d)\s?(?:to|tb)(?![a-z0-9])")
_RAM_CTX = re.compile(r"\bram\b|ddr[345]?|memoire|sodimm|so-dimm")
_STO_CTX = re.compile(r"\bssd\b|nvme|\bhdd\b|disque|stockage|m\.?2\b|sata|emmc")
_NVME = re.compile(r"\bnvme\b|m\.?2\b")
_SSD = re.compile(r"\bssd\b")
_HDD = re.compile(r"\bhdd\b|disque dur")
_NO_STORAGE = re.compile(r"\b(?:sans|no|ohne|pas de)\s+(?:ssd|disque|stockage|hdd|nvme)")
_NO_RAM = re.compile(r"\b(?:sans|no|ohne|pas de)\s+(?:ram|memoire|barrette)")
_NO_PSU = re.compile(r"\b(?:sans|no|ohne|pas de)\s+(?:alim|alimentation|chargeur|bloc|adaptateur secteur|cable)")
_BAREBONE = re.compile(r"\bbarebone\b")
_BUNDLE = re.compile(r"\becrans?\b|\bmoniteur\b|\bclavier\b")
_CPU_ONLY = re.compile(r"^\s*(?:processeur|cpu|proc)\b")
_WANTED = re.compile(r"^\s*(?:recherche|cherche|achete|achat|je cherche)\b")
_QTY = [
    re.compile(r"\blot de (\d{1,2})\b"),
    re.compile(r"(?<![\d,.])(\d{1,2})\s?(?:pcs|pieces|unites|ordinateurs|mini[ -]?pcs?|exemplaires)\b"),
    re.compile(r"(?:^|\s)x\s?(\d{1,2})\b"),
]


def _nearest(rx: re.Pattern, before: str, after: str) -> float:
    """Distance (in characters) of the keyword closest to the number, before or after; inf if absent."""
    d = float("inf")
    for m in rx.finditer(before):
        d = min(d, len(before) - m.end())
    m = rx.search(after)
    if m:
        d = min(d, m.start())
    return d


def parse_memory(text: str) -> tuple[Optional[int], Optional[int], str]:
    """(RAM in GB, storage in GB, storage type) from a normalized text.
    Each number is attached to the closest context (« 16 Go RAM », « SSD 256 Go »); on a tie
    or without context, the value decides: ≤ 64 GB = RAM, ≥ 120 GB = storage."""
    ram: list[int] = []
    sto: list[int] = []
    ambiguous: list[int] = []
    for m in _GB.finditer(text):
        v = int(m.group(1))
        # context window bounded by the neighbouring numbers: « 16 Go 256 Go SSD » does not make 16 an SSD
        before = text[max(0, m.start() - 22):m.start()]
        prev_units = list(_GB.finditer(before)) + list(_TB.finditer(before))
        if prev_units:
            before = before[max(u.end() for u in prev_units):]
        seps = list(_SEP.finditer(before))          # « i5 - 8 Go - SSD 250 Go »: the context stops at the separator
        if seps:
            before = before[seps[-1].end():]
        after = text[m.end():m.end() + 14]
        nxt = re.search(r"\d", after)
        if nxt:
            after = after[:nxt.start()]
        sep = _SEP.search(after)
        if sep:
            after = after[:sep.start()]
        d_ram, d_sto = _nearest(_RAM_CTX, before, after), _nearest(_STO_CTX, before, after)
        if d_ram < d_sto:
            ram.append(v)
        elif d_sto < d_ram:
            sto.append(v)
        elif d_ram != float("inf"):            # tie: the value decides
            (ram if v <= 64 else sto).append(v)
        else:
            ambiguous.append(v)
    for m in _TB.finditer(text):
        sto.append(int(m.group(1)) * 1000)
    for v in ambiguous:
        if v <= 64 and not ram:
            ram.append(v)
        elif v >= 120:
            sto.append(v)
    kind = "SSD NVMe" if _NVME.search(text) else "SSD" if _SSD.search(text) else "HDD" if _HDD.search(text) else ""
    return (max(ram) if ram else None), (max(sto) if sto else None), kind


# ----------------------------------------------------------------------------- profile

class PcProfile(Profile):
    name = "pc"
    attr_labels = {"cpu": "CPU", "ram_gb": "RAM (Go)", "storage_gb": "Stockage (Go)", "storage": "Type", "cpu_score": "Score CPU"}
    low_flags = frozenset({"cpu_unknown", "family_unknown", "barebone", "bundle"})
    flag_notes = {
        "cpu_unknown": "demander le processeur exact",
        "cpu_gen_assumed": "génération du CPU déduite du châssis, à confirmer",
        "bundle": "vendu avec écran ou clavier : prix non comparable",
        "ram_unknown": "demander la RAM",
        "storage_unknown": "demander le stockage",
        "family_unknown": "châssis hors catalogue",
        "barebone": "vendu sans RAM ni disque",
        "no_storage": "vendu sans disque",
        "no_ram": "vendu sans RAM",
        "no_psu": "vendu sans alimentation",
    }

    def __init__(self, pcfg: dict[str, Any] | None = None):
        super().__init__(pcfg)
        c = self.cfg
        floor = best_cpu(parse_cpus(norm(str(c.get("min_cpu") or ""))))
        self.min_cpu = floor.label if floor else ""
        self.min_score = floor.score if floor else None
        self.min_ram = int(c.get("min_ram_gb") or 0)
        self.min_storage = int(c.get("min_storage_gb") or 0)
        self.require_family = bool(c.get("require_family", False))
        self.families = [(str(f["name"]), _kw_regex(list(f.get("any") or [])), int(f["gen"]) if f.get("gen") else None)
                         for f in (c.get("families") or [])]
        self.reject = _kw_regex(list(c.get("reject") if c.get("reject") is not None else DEFAULT_KEYWORDS["dead"]))
        self.reject_title = _kw_regex(list(c.get("reject_title") or []))
        self.reject_form = _kw_regex(list(c.get("reject_form") or []))
        self.mini_any = _kw_regex(list(c.get("mini_any") or ["micro", "mini", "tiny", "usff", "1l"]))
        self.attr_labels = {**PcProfile.attr_labels, **dict(c.get("attr_labels") or {})}

    def classify(self, title: str, description: str = "", condition_code: str = "") -> ModelInfo:
        t, d = norm(title), norm(description)
        full = f"{t} {d}".strip()
        info = ModelInfo(verdict="accept")
        if condition_code == "parts":
            info.verdict = "reject"; info.reasons.append("dead"); return info
        kw = _find_kw(self.reject_title, t) or _find_kw(self.reject, full, negation_aware=True)
        if kw:
            info.verdict = "reject"; info.reasons.append(f"reject:{kw}"); return info
        if _WANTED.search(t):
            info.verdict = "reject"; info.reasons.append("wanted"); return info
        m = self.reject_form.search(t)
        # « micro tour » contains « micro »: strip the rejected form factors before looking for a « mini » word
        if m and not self.mini_any.search(self.reject_form.sub(" ", t)):
            info.verdict = "reject"; info.reasons.append(f"form:{m.group(0)}"); return info

        family_gen = None
        for name, rx, gen in self.families:
            if rx.search(t) or rx.search(d):
                info.family, family_gen = name, gen
                break
        if not info.family:
            if self.require_family:
                info.verdict = "reject"; info.reasons.append("family_unknown"); return info
            if _CPU_ONLY.search(t):
                info.verdict = "reject"; info.reasons.append("cpu_only"); return info
            info.flags.append("family_unknown")
        if _BUNDLE.search(t):
            info.flags.append("bundle")

        cpu = best_cpu(parse_cpus(t)) or best_cpu(parse_cpus(d))
        if cpu and cpu.gen is None and cpu.tier in ("3", "5", "7", "9"):
            if family_gen:
                # « OptiPlex 3060 i5 »: the chassis sets the generation (3060 = 8th), the tier comes from the title
                cpu = Cpu(f"{cpu.label} ({family_gen}e gén.)", cpu.tier, family_gen)
                info.flags.append("cpu_gen_assumed")
            elif not info.family:
                cpu = None          # « Mini pc - i5 »: neither chassis nor generation, nothing identifiable
        if cpu:
            info.model = cpu.label
            info.attrs["cpu"] = cpu.label
            if cpu.score is not None:
                info.attrs["cpu_score"] = cpu.score
                info.attrs["cpu_gen"] = cpu.gen
                if self.min_score is not None and cpu.score < self.min_score:
                    info.verdict = "reject"; info.reasons.append(f"cpu_below:{cpu.label}"); return info
            elif cpu.tier in ("celeron", "pentium", "athlon") and self.min_score is not None:
                info.verdict = "reject"; info.reasons.append(f"cpu_below:{cpu.label}"); return info
            else:
                info.flags.append("cpu_unknown")     # known tier, unknown generation
        else:
            info.flags.append("cpu_unknown")
        if not info.family and not info.model:
            # neither a known chassis nor a CPU: nothing indicates a targeted PC (another object surfaced by the search)
            info.verdict = "reject"; info.reasons.append("no_match"); return info

        ram, sto, kind = parse_memory(full)
        barebone = bool(_BAREBONE.search(full))
        no_ram = barebone or bool(_NO_RAM.search(full))
        no_sto = barebone or bool(_NO_STORAGE.search(full))
        if barebone:
            info.flags.append("barebone")
        else:
            if no_ram:
                info.flags.append("no_ram")
            if no_sto:
                info.flags.append("no_storage")
        if _NO_PSU.search(full):
            info.flags.append("no_psu")
        if ram is not None and not no_ram:
            info.attrs["ram_gb"] = ram
            if self.min_ram and ram < self.min_ram:
                info.verdict = "reject"; info.reasons.append(f"ram_below:{ram}"); return info
        elif not no_ram:
            info.flags.append("ram_unknown")
        if sto is not None and not no_sto:
            info.attrs["storage_gb"] = sto
            if kind:
                info.attrs["storage"] = kind
            if self.min_storage and sto < self.min_storage:
                info.verdict = "reject"; info.reasons.append(f"storage_below:{sto}"); return info
        elif not no_sto:
            info.flags.append("storage_unknown")

        for rx in _QTY:
            lm = rx.search(t)
            if lm:
                q = int(lm.group(1))
                if 2 <= q <= 50:
                    info.quantity = q
                    info.flags.append("lot")
                break
        return info
