import json
from pathlib import Path

import httpx
import pytest

from listingwatcher.fetchers.base import MissingCredentials
from listingwatcher.fetchers.ebay import EbayFetcher, summary_to_listing

FIX = json.loads((Path(__file__).parent / "fixtures" / "ebay_search.json").read_text(encoding="utf-8"))


def test_summary_to_listing_fixed_shipping():
    l = summary_to_listing(FIX["itemSummaries"][0], "EBAY_DE")
    assert l.source == "ebay" and l.listing_id == "v1|123456789012|0"
    assert l.price == 189.9 and l.shipping == 12.99 and l.delivered_price == 202.89
    assert l.condition_code == "used" and l.seller_reviews == 4821 and l.seller_rating == 99.6
    assert l.location == "City1, DE" and l.delivery is True and l.secure_payment is True
    assert l.raw["marketplace"] == "EBAY_DE"


def test_summary_calculated_shipping_unknown():
    l = summary_to_listing(FIX["itemSummaries"][1], "EBAY_FR")
    assert l.shipping is None and l.seller_reviews == 0 and l.condition_code == "new"


def test_for_parts_condition_code(classifier):
    l = summary_to_listing(FIX["itemSummaries"][2], "EBAY_DE")
    assert l.condition_code == "parts"
    assert not classifier.classify(l.title, "", l.condition_code).accepted


def test_non_eur_skipped():
    item = dict(FIX["itemSummaries"][0], price={"value": "150", "currency": "USD"})
    assert summary_to_listing(item, "EBAY_DE") is None


def test_missing_credentials_raises(cfg):
    with pytest.raises(MissingCredentials):
        EbayFetcher(cfg["sources"]["ebay"], cfg)


def test_fetch_with_mocked_api(cfg):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/oauth2/token"):
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
        if "/item_summary/search" in request.url.path:
            assert request.headers["authorization"] == "Bearer tok"
            assert request.headers["x-ebay-c-marketplace-id"] in ("EBAY_FR", "EBAY_DE")
            assert "contextualLocation=country%3DFR%2Czip%3D75001" == request.headers["x-ebay-c-enduserctx"]
            flt = request.url.params["filter"]
            assert "deliveryCountry:FR" in flt and "conditionIds:{" in flt and "price:[60..400]" in flt
            return httpx.Response(200, json=FIX)
        if "/buy/browse/v1/item/" in request.url.path:
            return httpx.Response(200, json={
                "itemId": "v1|123456789012|0", "mpn": "ST8000VN004",
                "description": "<p>Power-On Hours: 12000<br>SMART ok</p>",
                "estimatedAvailabilities": [{"estimatedAvailableQuantity": 3}],
                "returnTerms": {"returnsAccepted": True, "returnPeriod": {"value": 30, "unit": "DAY"}},
                "seller": {"sellerAccountType": "BUSINESS"},
            })
        return httpx.Response(404)

    from listingwatcher.config import watch_source_cfg
    scfg = dict(watch_source_cfg(cfg, cfg["watches"]["hdd"], "ebay"), marketplaces=["EBAY_DE"], queries=["8TB NAS"], pause_s=0)
    f = EbayFetcher(scfg, cfg, client_id="id", client_secret="secret",
                    http=httpx.Client(transport=httpx.MockTransport(handler)))
    res = f.fetch()
    assert res.complete and len(res.listings) == 3
    assert sum(1 for c in calls if c.url.path.endswith("/oauth2/token")) == 1   # token is cached

    l = next(x for x in res.listings if x.listing_id == "v1|123456789012|0")
    assert f.enrich(l)
    assert l.description.startswith("MPN: ST8000VN004") and "Power-On Hours: 12000" in l.description
    assert l.quantity == 3 and l.returns == "acceptés 30 day" and l.seller_type == "pro"
