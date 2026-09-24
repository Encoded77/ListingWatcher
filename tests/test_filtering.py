from listingwatcher.filtering import Filter
from listingwatcher.models import Listing, ModelInfo


def L(price, shipping=0.0, fees=0.0, **kw):
    base = dict(source="lbc", listing_id="1", url="u", title="t", price=price, shipping=shipping, fees=fees,
                seller_reviews=20, seller_rating=99.0, delivery=True, secure_payment=True)
    base.update(kw)
    return Listing(**base)


def ok(family="IronWolf", model="ST8000VN004", flags=None, quantity=1):
    return ModelInfo("accept", family, model, "Seagate", [], flags or [], {"capacities_tb": [8.0]}, quantity)


def test_tiers_on_delivered_price(flt):
    assert flt.decide(L(190, 6.9, 1.99), ok()).tier == "urgent"
    d = flt.decide(L(240, 6.9, 1.99), ok())
    assert d.tier == "default" and d.priority == "default"
    assert flt.decide(L(280, 6.9), ok()).tier == "low"
    d = flt.decide(L(295, 6.9), ok())
    assert d.tier == "ignore" and not d.keep


def test_compares_delivered_not_displayed(flt):
    # 199 € displayed but 10 € shipping: 209 € delivered → no longer "urgent"
    d = flt.decide(L(199, 10.0), ok())
    assert d.tier == "default" and d.unit_price == 209.0
    assert d.per_unit == round(209 / 8, 2)


def test_lot_uses_unit_price(flt):
    d = flt.decide(L(400, 6.9, 1.99), ok(quantity=2))
    assert d.keep and d.tier == "default"
    assert d.unit_price == round((400 + 6.9 + 1.99) / 2, 2)
    assert any("lot de 2" in n for n in d.notes)


def test_reject_is_never_kept(flt):
    info = ModelInfo("reject", reasons=["smr"])
    d = flt.decide(L(100), info)
    assert not d.keep and d.tier == "reject"


def test_low_priority_flags(flt):
    for flag in ("model_unknown", "low_tier", "ref_unverified"):
        d = flt.decide(L(150), ok(flags=[flag]))
        assert d.keep and d.tier == "urgent" and d.priority == "low", flag
    d = flt.decide(L(150), ok(flags=["model_unknown"]))
    assert any("demander la référence" in n for n in d.notes)


def test_suspicious_price_far_below_reference(flt):
    d = flt.decide(L(90), ok())        # 90 € < 55 % of 240 €
    assert d.keep and d.suspicious and "médiane" in d.suspicious[0]
    d = flt.decide(L(150), ok())
    assert not d.suspicious


def test_suspicious_zero_feedback_below_market(flt):
    d = flt.decide(L(180, seller_reviews=0), ok())
    assert any("sans évaluation" in s for s in d.suspicious)
    d = flt.decide(L(180, seller_reviews=None), ok())
    assert any("sans évaluation" in s for s in d.suspicious)


def test_suspicious_shipping(flt):
    d = flt.decide(L(159, 875.0), ok())     # real case: 159 € + 875 € shipping
    assert not d.keep or any("port" in s for s in d.suspicious)
    d = flt.decide(L(150, 90.0), ok())
    assert any("port" in s for s in d.suspicious)


def test_suspicious_no_delivery_no_history(flt):
    d = flt.decide(L(150, 0.0, delivery=False, secure_payment=False, seller_reviews=0), ok())
    assert any("ni livraison" in s for s in d.suspicious)


def test_require_delivery(wcfg, profile):
    f = Filter({**wcfg, "notify": {**wcfg["notify"], "require_delivery": True}}, profile=profile)
    d = f.decide(L(150, 0.0, delivery=False), ok())
    assert not d.keep and d.tier == "ignore"


def test_market_median_used_when_enough_samples(wcfg, profile):
    class M:
        def median_unit_price(self, model, family):
            return 300.0, 10
    f = Filter(wcfg, market=M(), profile=profile)
    d = f.decide(L(150), ok())
    assert any("médiane (300 €)" in s for s in d.suspicious)


def test_market_ignored_when_few_samples(wcfg, profile):
    class M:
        def median_unit_price(self, model, family):
            return 300.0, 2
    f = Filter(wcfg, market=M(), profile=profile)
    assert not f.decide(L(150), ok()).suspicious


def test_mixed_lot_price_not_divided(flt):
    info = ok(quantity=9, flags=["multi_capacity", "lot", "ref_missing"])
    d = flt.decide(L(540, 13.9, 1.99), info)
    assert d.unit_price == 555.89 and not d.keep      # > 300 €: ignored instead of a bogus 62 €/drive
    assert any("lot mixte" in n for n in d.notes)


def test_old_listings_ignored(wcfg, profile):
    from listingwatcher.filtering import listing_age_days
    assert listing_age_days("2026-09-06 14:59:56") is not None
    assert listing_age_days("2023-01-10 10:00:00") > 1000
    assert listing_age_days("2026-09-08T10:00:00.000Z") is not None and listing_age_days("n'importe quoi") is None
    f = Filter({**wcfg, "notify": {**wcfg["notify"], "max_age_days": 120}}, profile=profile)
    assert f.decide(L(150, posted_at="2023-01-10 10:00:00"), ok()).keep is False
    assert f.decide(L(150, posted_at="2023-01-10 10:00:00"), ok()).tier == "ignore"
    assert f.decide(L(150, posted_at="2026-09-06 14:59:56"), ok()).keep
    assert f.decide(L(150), ok()).keep                      # unknown date: no filter
    f0 = Filter({**wcfg, "notify": {**wcfg["notify"], "max_age_days": 0}}, profile=profile)
    assert f0.decide(L(150, posted_at="2023-01-10 10:00:00"), ok()).keep
