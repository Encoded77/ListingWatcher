"""leboncoin parser on fixtures captured on 9 September 2026 (pages 1 and 2 of ck/accessoires_informatique/disque-dur-8to)."""
import pytest

from listingwatcher.fetchers.http import FetchError
from listingwatcher.fetchers.leboncoin import (
    LeboncoinFetcher, ad_to_listing, estimate_shipping, merge_listings, parse_search_page,
)
from listingwatcher.models import FetchResult
from tests.conftest import fake_html

SHIP = {"default_eur": 6.9, "by_weight_g": [[1000, 5.5], [2000, 6.9], [5000, 9.9], [10000, 13.9]]}


def test_parse_search_page(lbc_page1):
    ads, max_pages = parse_search_page(fake_html(lbc_page1))
    assert len(ads) == 35 and max_pages == 3


def test_parse_without_next_data_raises():
    with pytest.raises(FetchError):
        parse_search_page("<html><p>Please enable JS and disable any ad blocker</p></html>")


def test_ad_to_listing_fields(lbc_page1):
    ad = next(a for a in lbc_page1["ads"] if a["list_id"] == 3262452896)   # WD Gold WD8004FRYZ, pro, 325 €
    l = ad_to_listing(ad, SHIP)
    assert l.source == "lbc" and l.listing_id == "3262452896"
    assert l.title.startswith("Disque dur 8to 3''5 WD Gold WD8004FRYZ")
    assert l.price == 325.0
    assert l.delivery is True and l.shipping_estimated and l.shipping == 5.5     # 250 g → 1 kg tier
    assert l.fees == 16.95
    assert l.delivered_price == 325 + 5.5 + 16.95
    assert l.condition == "État neuf" and l.condition_code == "new"
    assert l.seller_name == "boutique3" and l.seller_type == "pro"
    assert l.seller_rating == 100 and l.seller_reviews == 45
    assert l.location == "Ville3 75000" and l.quantity == 4
    assert l.status == "active" and l.url.endswith("/3262452896")


def test_statuses_sold_pending_parts(lbc_page1):
    by_id = {a["list_id"]: a for a in lbc_page1["ads"]}
    assert ad_to_listing(by_id[3253146301], SHIP).status == "sold"       # "Vendu" (sold)
    assert ad_to_listing(by_id[3261865283], SHIP).status == "pending"    # "Achat en cours" (purchase in progress)
    dead = ad_to_listing(by_id[3261273582], SHIP)                        # Barracuda [HS], condition "Pour pièces" (for parts)
    assert dead.condition_code == "parts" and dead.status == "pending"


def test_no_delivery_listing(lbc_page1):
    ad = next(a for a in lbc_page1["ads"] if a["list_id"] == 3241928424)    # face_to_face
    l = ad_to_listing(ad, SHIP)
    assert l.delivery is False and l.shipping == 0.0 and l.fees == 0.0
    assert l.seller_reviews is None


def test_estimate_shipping():
    assert estimate_shipping(900, SHIP) == 5.5
    assert estimate_shipping(1500, SHIP) == 6.9
    assert estimate_shipping(0, SHIP) == 6.9
    assert estimate_shipping(50000, SHIP) == 6.9


def test_merge_keeps_worst_status(lbc_page1, lbc_page2):
    found = {}
    for a in lbc_page1["ads"] + lbc_page2["ads"]:
        merge_listings(found, ad_to_listing(a, SHIP))
    ids1 = {a["list_id"] for a in lbc_page1["ads"]}
    ids2 = {a["list_id"] for a in lbc_page2["ads"]}
    assert ids1 & ids2, "the fixtures must overlap"
    assert len(found) == len(ids1 | ids2)
    # 3249690419 is "Vendu" (sold) on both pages; force an artificial contradiction
    a = next(x for x in lbc_page1["ads"] if x["list_id"] == 3249690419)
    fresh = ad_to_listing(a, SHIP)
    fresh.status = "active"
    merge_listings(found, fresh)
    assert found["3249690419"].status == "sold"


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, headers=None):
        self.calls.append(url)
        class R:
            text = self.pages[url]
        return R()


def test_fetcher_paginates_and_dedupes(lbc_page1, lbc_page2, cfg):
    p1 = dict(lbc_page1, max_pages=2)
    p2 = dict(lbc_page2, max_pages=2)
    base = "https://www.leboncoin.fr/ck/accessoires_informatique/disque-dur-8to"
    client = FakeClient({base: fake_html(p1), base + "/p-2": fake_html(p2)})
    scfg = {"searches": [{"category": "accessoires_informatique", "slug": "disque-dur-8to"}], "max_pages": 5,
            "shipping_estimate": SHIP}
    f = LeboncoinFetcher(scfg, cfg, client=client)
    res = f.fetch()
    assert isinstance(res, FetchResult) and res.complete and not res.errors
    assert client.calls == [base, base + "/p-2"]          # stops at the page's max_pages, no p-3
    assert len(res.listings) == len({a["list_id"] for a in lbc_page1["ads"] + lbc_page2["ads"]})


def test_full_pipeline_classification_on_fixture(lbc_page1, lbc_page2, classifier, flt):
    """On the 2 real pages, the filter must keep only plausible 8 TB internal drives."""
    found = {}
    for a in lbc_page1["ads"] + lbc_page2["ads"]:
        merge_listings(found, ad_to_listing(a, SHIP))
    kept = {}
    for l in found.values():
        info = classifier.classify(l.title, l.description, l.condition_code)
        d = flt.decide(l, info)
        if d.keep:
            kept[l.listing_id] = (info, d)
    titles = {found[i].title for i in kept}
    for t in titles:
        low = t.lower()
        assert "externe" not in low and "synology" not in low and "sas" not in low and "barracuda" not in low, t
    # listings expected among the kept ones
    assert "3263490710" in kept        # "Disque dur interne 8To, Seagate IronWolf", 280 €
    assert "3249690419" in kept        # Exos 7E8 250 € (sold, but kept in the database; the pipeline does not notify sold listings)
    assert "3260587296" in kept and "low_tier" in kept["3260587296"][0].flags   # WD Purple
    assert "3265405667" in kept and kept["3265405667"][0].quantity == 2        # lot of 2 Exos, 400 €
    # listings expected among the rejected ones
    for lid in ("3264439495", "3261273582", "3254708298", "3265747496", "2160681526", "2996560441", "3170029618"):
        assert lid not in kept, found[lid].title
