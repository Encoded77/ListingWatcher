"""Watch normalization: legacy format wrapped, inherited defaults, shared transport + per-watch searches."""
import pytest

from listingwatcher.config import LEGACY_WATCH, normalize_watches, watch_source_cfg


def test_legacy_config_wrapped_in_hdd_watch():
    cfg = {
        "profile": {"type": "keywords", "title": "Cartes", "keywords": {"require_any": ["3080"]}},
        "thresholds": [{"max_delivered": 400, "priority": "urgent"}],
        "notify": {"price_drop_pct": 7, "source_labels": {"lbc": "LBC"}},
        "market": {"history_days": 30, "reference_unit_price": 500}, "scam": {"max_shipping_eur": 40},
        "sources": {
            "leboncoin": {"enabled": True, "min_delay_s": 6, "searches": [{"category": "ordinateurs", "slug": "rtx-3080"}]},
            "ebay": {"enabled": True, "marketplaces": ["EBAY_FR"], "queries": ["RTX 3080"], "price_min": 100},
        },
    }
    out = normalize_watches(cfg)
    assert list(out["watches"]) == [LEGACY_WATCH]
    w = out["watches"][LEGACY_WATCH]
    assert w["name"] == "hdd" and w["title"] == "Cartes" and w["profile"]["type"] == "keywords"
    assert w["thresholds"][0]["max_delivered"] == 400
    assert w["notify"]["price_drop_pct"] == 7 and w["notify"]["enabled"] is True
    assert w["market"]["reference_unit_price"] == 500 and w["scam"]["max_shipping_eur"] == 40
    assert w["sources"]["leboncoin"] == {"searches": [{"category": "ordinateurs", "slug": "rtx-3080"}]}
    lbc = watch_source_cfg(out, w, "leboncoin")
    assert lbc["min_delay_s"] == 6 and lbc["searches"][0]["slug"] == "rtx-3080"
    ebay = watch_source_cfg(out, w, "ebay")
    assert ebay["marketplaces"] == ["EBAY_FR"] and ebay["price_min"] == 100


def test_watches_inherit_defaults_and_override():
    cfg = {
        "notify": {"price_drop_pct": 5, "suspicious": "separate"}, "market": {"history_days": 30}, "scam": {"max_shipping_eur": 40},
        "sources": {"leboncoin": {"min_delay_s": 6}, "ebay": {"enabled": False, "marketplaces": ["EBAY_FR"]}},
        "watches": {
            "a": {"title": "A", "profile": {"type": "keywords"}, "thresholds": [{"max_delivered": 100}],
                  "notify": {"enabled": False, "topic": "t-a"}, "scam": {"max_shipping_eur": 10},
                  "sources": {"leboncoin": {"searches": [{"category": "x", "slug": "a"}]}, "ebay": {"queries": ["a"]}}},
            "b": {"profile": {"type": "keywords"}, "sources": {"leboncoin": {"enabled": False}}},
            "off": {"enabled": False, "profile": {"type": "keywords"}},
        },
    }
    out = normalize_watches(cfg)
    assert list(out["watches"]) == ["a", "b"]
    a, b = out["watches"]["a"], out["watches"]["b"]
    assert a["notify"] == {"price_drop_pct": 5, "suspicious": "separate", "enabled": False, "topic": "t-a"}
    assert a["scam"]["max_shipping_eur"] == 10 and b["scam"]["max_shipping_eur"] == 40
    assert b["title"] == "b" and b["notify"]["enabled"] is True and b["thresholds"] == []
    assert watch_source_cfg(out, a, "leboncoin")["searches"][0]["slug"] == "a"
    assert watch_source_cfg(out, a, "ebay") is None            # source disabled globally
    assert watch_source_cfg(out, b, "leboncoin") is None       # disabled for this watch
    assert watch_source_cfg(out, b, "ebay") is None            # the watch does not declare it


def test_no_active_watch_rejected():
    with pytest.raises(ValueError):
        normalize_watches({"watches": {"x": {"enabled": False}}})


def test_shipped_config_has_both_watches(cfg):
    assert list(cfg["watches"]) == ["hdd", "minipc"]
    hdd, pc = cfg["watches"]["hdd"], cfg["watches"]["minipc"]
    assert hdd["notify"]["enabled"] is True and pc["notify"]["enabled"] is False
    assert pc["thresholds"][-1]["max_delivered"] == 600
    assert pc["profile"]["pc"]["min_cpu"] == "i5-10500T" and pc["profile"]["pc"]["min_ram_gb"] == 16
    assert hdd["profile"]["type"] == "hdd" and pc["profile"]["type"] == "pc"
    assert watch_source_cfg(cfg, pc, "leboncoin")["min_delay_s"] == 6
    assert watch_source_cfg(cfg, pc, "ebay")["category_ids"] == "171957"
    assert watch_source_cfg(cfg, hdd, "ebay")["category_ids"] == "56083"
