"""Vinted parser and fetcher on synthetic fixtures modelled on the 28 September 2026 markup."""
from pathlib import Path

import pytest

from listingwatcher.fetchers.http import BlockedError
from listingwatcher.fetchers.vinted import (
    VintedFetcher, card_to_listing, parse_card_title, parse_catalog_page, parse_item_page, parse_price,
)

FIX = Path(__file__).parent / "fixtures"


def page(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class FakeTransport:
    def __init__(self, pages: dict[str, str], default: str = ""):
        self.pages, self.default, self.urls = pages, default, []

    def get_text(self, url: str) -> str:
        self.urls.append(url)
        if url in self.pages:
            return self.pages[url]
        if isinstance(self.default, Exception):
            raise self.default
        return self.default


@pytest.mark.parametrize("text,value", [
    ("1300.00", 1300.0), ("1 099,00", 1099.0), ("1 099,00", 1099.0), ("1,300.50", 1300.5),
    ("1.300,50", 1300.5), ("", None), ("abc", None),
])
def test_parse_price(text, value):
    assert parse_price(text) == value


def test_card_title_with_commas_and_extra_attributes():
    c = parse_card_title("Mac Studio, M4 Max, Marque: Apple, Modèle: Mac Studio 2025, État: Bon état, 2800.00 €, 2940.70 €")
    assert c["title"] == "Mac Studio, M4 Max"
    assert c["price"] == 2800 and c["total"] == 2940.70
    assert c["attrs"] == {"État": "Bon état", "Modèle": "Mac Studio 2025", "Marque": "Apple"}


def test_card_title_without_price():
    assert parse_card_title("Annonce sans prix lisible") is None


def test_parse_catalog_page_dedups_and_skips_unpriced():
    cards = parse_catalog_page(page("vinted_catalog.html"))
    assert [c["id"] for c in cards] == ["10000000001", "10000000002", "10000000003"]
    assert cards[0]["url"] == "https://www.vinted.fr/items/10000000001-rtx-3090"
    assert cards[1]["title"] == "Rtx 3090 FE, 24Go, comme neuve" and cards[1]["price"] == 1099


def test_card_to_listing_fields():
    card = parse_catalog_page(page("vinted_catalog.html"))[0]
    l = card_to_listing(card, 8.9)
    assert l.source == "vinted" and l.listing_id == "10000000001"
    assert l.price == 1300 and l.fees == 65.70 and l.shipping == 8.9 and l.shipping_estimated
    assert l.delivered_price == round(1300 + 65.70 + 8.9, 2)
    assert l.condition == "Très bon état" and l.condition_code == "used"
    assert l.delivery is True and l.secure_payment is True and l.raw["brand"] == "Gigabyte"
    new = card_to_listing(parse_catalog_page(page("vinted_catalog.html"))[1], None)
    assert new.condition_code == "new" and new.shipping is None and not new.shipping_estimated


def test_parse_item_page():
    info = parse_item_page(page("vinted_item.html"))
    assert info["description"].startswith("Carte testée")
    assert info["availability"] == "InStock" and info["reserved"] is True
    assert info["reputation"] == 96.0 and info["feedback_count"] == 74


def test_search_url_with_price_bounds():
    f = VintedFetcher({"queries": ["rtx 3090"], "price_min": 300, "price_max": 700.0}, {}, transport=FakeTransport({}))
    assert f.search_url("rtx 3090", 1) == ("https://www.vinted.fr/catalog?search_text=rtx+3090&order=newest_first"
                                           "&price_from=300&price_to=700&currency=EUR")
    assert f.search_url("rtx 3090", 2).endswith("&page=2")
    bare = VintedFetcher({"queries": ["x"]}, {}, transport=FakeTransport({}))
    assert bare.search_url("x", 1) == "https://www.vinted.fr/catalog?search_text=x&order=newest_first"


def test_fetch_merges_queries_and_enrich_sets_status():
    t = FakeTransport({}, default=page("vinted_catalog.html"))
    f = VintedFetcher({"queries": ["rtx 3090", "3090 fe"]}, {}, transport=t)
    res = f.fetch()
    assert res.complete and res.source == "vinted" and len(res.listings) == 3 and len(t.urls) == 2
    l = res.listings[0]
    t.default = page("vinted_item.html")
    assert f.enrich(l)
    assert l.status == "pending" and l.seller_rating == 96.0 and l.seller_reviews == 74
    assert "jamais minée" in l.description


def test_fetch_empty_first_page_is_incomplete():
    f = VintedFetcher({"queries": ["x"]}, {}, transport=FakeTransport({}, default="<html>défi</html>"))
    res = f.fetch()
    assert not res.complete and res.listings == [] and res.errors


def test_fetch_propagates_block():
    f = VintedFetcher({"queries": ["x"]}, {}, transport=FakeTransport({}, default=BlockedError("HTTP 403")))
    with pytest.raises(BlockedError):
        f.fetch()
