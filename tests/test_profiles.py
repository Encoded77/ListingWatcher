"""The core is generic: a purely configured `keywords` profile must be enough to watch something else."""
import pytest

from listingwatcher.filtering import Filter
from listingwatcher.models import Listing
from listingwatcher.notify import Notifier
from listingwatcher.profiles import load_profile
from listingwatcher.profiles.keywords import KeywordsProfile

GPU = {
    "unit_label": "", "unit_divisor": None,
    "require_any": ["rtx 3080", "3080"],
    "reject": ["pour pieces", "hs", "ne fonctionne pas"],
    "reject_title": ["pc complet", "boitier"],
    "families": [{"name": "RTX 3080 Ti", "any": ["3080 ti", "3080ti"]}, {"name": "RTX 3080", "any": ["3080"]}],
    "model_regex": r"\b(?:TUF|ROG STRIX|GAMING X TRIO|VENTUS)\b",
}


def test_keywords_profile_classification():
    p = KeywordsProfile(GPU)
    info = p.classify("Carte graphique MSI RTX 3080 Gaming X Trio 10 Go")
    assert info.accepted and info.family == "RTX 3080" and info.model == "Gaming X Trio" and info.quantity == 1
    assert not p.classify("RTX 3080 Ti TUF, HS pour pièces").accepted
    assert p.classify("PC complet RTX 3080").reasons[0].startswith("reject:")
    assert p.classify("GTX 1080 Ti").reasons == ["no_match"]
    lot = p.classify("Lot de 3 RTX 3080 mining")
    assert lot.quantity == 3 and "lot" in lot.flags
    unk = p.classify("Vends 3080, état neuf")
    assert unk.accepted and unk.family == "RTX 3080"
    assert p.per_unit(450.0) is None and p.format_per_unit(None) == ""


def test_keywords_profile_end_to_end(cfg):
    kcfg = {**cfg, "profile": {"type": "keywords", "title": "Cartes graphiques", "keywords": GPU},
            "thresholds": [{"max_delivered": 400, "priority": "urgent", "tags": ["fire"]},
                           {"max_delivered": 600, "priority": "default"}],
            "market": {**cfg["market"], "reference_unit_price": 500}}
    profile = load_profile(kcfg)
    assert profile.name == "keywords" and profile.unit_divisor is None
    flt = Filter(kcfg, profile=profile)
    l = Listing("lbc", "1", "u", "MSI RTX 3080 Ventus", 380.0, shipping=9.9, seller_reviews=12, delivery=True,
                posted_at="2026-09-08 10:00:00")
    info = profile.classify(l.title)
    d = flt.decide(l, info)
    assert d.keep and d.tier == "urgent" and d.per_unit is None and d.unit_price == 389.9
    n = Notifier("", "", dry_run=True, profile=profile, digest_title="Digest Cartes graphiques")
    n.listing_alert(l, info, d, "new")
    assert n.sent[-1]["title"] == "[LBC] RTX 3080 Ventus — 389,90 €"
    assert "SMART" not in n.sent[-1]["message"]


def test_hdd_profile_keeps_its_unit(cfg):
    profile = load_profile(cfg)
    assert profile.name == "hdd" and profile.unit_divisor == 8 and profile.unit_label == "€/To"
    assert profile.format_per_unit(28.75) == "28,75 €/To"
    info = profile.classify("Seagate IronWolf ST8000VN004 8Tb (9000h environ)")
    assert profile.attr_lines(info) == ["SMART (h) : 9000", "Capacités citées (To) : 8.0"]


def test_unknown_profile_rejected(cfg):
    with pytest.raises(ValueError):
        load_profile({**cfg, "profile": {"type": "velo"}})
