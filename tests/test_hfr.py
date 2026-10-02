"""HFR parser and fetcher on synthetic fixtures modelled on the 28 September 2026 markup."""
from pathlib import Path

import pytest

from listingwatcher.fetchers.hfr import (
    HfrFetcher, HfrTransport, item_block, parse_date, parse_opening_post, parse_price, parse_topic_list,
    query_words, topic_to_listing,
)
from listingwatcher.fetchers.http import BlockedError

FIX = Path(__file__).parent / "fixtures"
LIST_URL = "https://forum.hardware.fr/hfr/AchatsVentes/Hardware/liste_sujet-1.htm"


def page(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class FakeClient:
    def __init__(self, pages: dict[str, str]):
        self.pages, self.urls = pages, []

    def get(self, url: str):
        self.urls.append(url)
        if url not in self.pages:
            raise BlockedError(f"HTTP 403 sur {url}")
        return type("R", (), {"text": self.pages[url]})()


@pytest.mark.parametrize("text,value", [
    ("Prix: 1 050€out", 1050.0), ("650 euros fdp in", 650.0), ("Prix : 700", 700.0), ("prix demandé 1.200 €", 1200.0),
    ("24 Go GDDR6X", None), ("Achetée 1 499,99 € en 2021", 1499.0), ("0 €", None),
])
def test_parse_price(text, value):
    assert parse_price(text) == value


def test_parse_date_is_paris_time():
    assert parse_date("28-09-2026 à 16:42") == "2026-09-28T16:42:00+02:00"
    assert parse_date("n'importe quoi") == ""


def test_parse_topic_list():
    rows = parse_topic_list(page("hfr_list.html"))
    assert [r["id"] for r in rows] == ["100", "653233", "700001", "252796", "700002"]
    r = rows[1]
    assert r["title"] == "[VDS] Boîtier Frame 4000D / RTX 3090 FE" and r["author"] == "vendeur1"
    assert r["url"] == "https://forum.hardware.fr/hfr/AchatsVentes/Hardware/frame-4000d-rtx-3090-sujet_653233_1.htm"
    assert r["last_post"] == "2026-09-28T16:42:00+02:00" and r["replies"] == 12 and not r["sticky"]
    assert rows[0]["sticky"]


def test_candidates_keep_sale_topics_only():
    f = HfrFetcher({"queries": ["3090", "mac studio"]}, {}, transport=HfrTransport(FakeClient({})))
    cands = f.candidates(parse_topic_list(page("hfr_list.html")))
    # sticky rules, the [ACH] topic and the regional aggregate topic are ignored
    assert [(r["id"], w) for r, w in cands] == [("653233", ["3090"]), ("700002", ["mac", "studio"])]


def test_opening_post_drops_quotes_and_reads_transactions():
    post = parse_opening_post(page("hfr_topic.html"))
    assert post["transactions"] == 54
    assert not any("999" in l for l in post["lines"])          # the quoted rules are gone
    assert "Toujours dispo" not in " ".join(post["lines"])       # replies are not part of the post


def test_item_block_stops_at_separator():
    post = parse_opening_post(page("hfr_topic.html"))
    block, found = item_block(post["lines"], query_words("rtx 3090"))
    assert found and block[0].startswith("Je vends ma NVIDIA GeForce RTX 3090")
    assert block[-1] == "Prix: 1 050€out"


def test_topic_to_listing_multi_item_post():
    rows = {r["id"]: r for r in parse_topic_list(page("hfr_list.html"))}
    post = parse_opening_post(page("hfr_topic.html"))
    gpu = topic_to_listing(rows["653233"], post, query_words("3090"))
    assert gpu.source == "hfr" and gpu.listing_id == "653233" and gpu.price == 1050
    assert gpu.shipping is None and gpu.delivery is True and gpu.status == "active"
    assert gpu.seller_reviews == 54 and gpu.seller_name == "vendeur1"
    assert gpu.posted_at == "2026-09-28T16:42:00+02:00" and gpu.condition_code == "used"
    case = topic_to_listing(rows["653233"], post, query_words("4000d"))
    assert case.price == 80          # the struck-through old price is ignored


def test_topic_to_listing_sold_and_unpriced():
    row = {"id": "1", "title": "[VDS] RTX 3090", "url": "u", "author": "a", "last_post": ""}
    sold = {"lines": ["RTX 3090 FE 700€ [VENDU]", "fdp in"], "transactions": 3}
    l = topic_to_listing(row, sold, query_words("3090"))
    assert l.status == "sold" and l.shipping == 0.0
    struck = {"lines": ["~~RTX 3090 FE 700€~~"], "transactions": 3}
    assert topic_to_listing(row, struck, query_words("3090")) is None     # only a struck price: not for sale
    bought = {"lines": ["RTX 3090 achetée 1 499 € en 2021, très bon état", "Prix : 700 € fdp in"], "transactions": 1}
    assert topic_to_listing(row, bought, query_words("3090")).price == 700
    assert topic_to_listing(row, {"lines": ["RTX 3090 FE, faire offre"], "transactions": 0}, query_words("3090")) is None
    inout = {"lines": ["- RTX 3090 FE : 655€ out, 665€ in par colissimo"], "transactions": 2}
    l = topic_to_listing(row, inout, query_words("3090"))
    assert l.price == 655 and l.shipping == 10 and l.delivered_price == 665
    hand = {"lines": ["RTX 3090 650€", "Main propre uniquement"], "transactions": None}
    assert topic_to_listing(row, hand, query_words("3090")).delivery is False


def test_fetch_reads_lists_then_topics_and_caches_unchanged_topics():
    topic_url = "https://forum.hardware.fr/hfr/AchatsVentes/Hardware/frame-4000d-rtx-3090-sujet_653233_1.htm"
    client = FakeClient({LIST_URL: page("hfr_list.html"), topic_url: page("hfr_topic.html")})
    transport = HfrTransport(client)
    f = HfrFetcher({"queries": ["3090"], "list_pages": 1}, {}, transport=transport)
    res = f.fetch()
    assert res.complete and [l.listing_id for l in res.listings] == ["653233"]
    assert client.urls == [LIST_URL, topic_url]
    transport.reset()                                   # next scan: the list is read again, the topic is not
    res = f.fetch()
    assert [l.price for l in res.listings] == [1050] and client.urls == [LIST_URL, topic_url, LIST_URL]


def test_fetch_topic_budget_makes_result_incomplete():
    client = FakeClient({LIST_URL: page("hfr_list.html")})
    f = HfrFetcher({"queries": ["3090", "mac studio"], "list_pages": 1, "topic_max_per_scan": 0}, {},
                   transport=HfrTransport(client))
    res = f.fetch()
    assert not res.complete and res.listings == []


def test_fetch_propagates_block():
    f = HfrFetcher({"queries": ["3090"], "list_pages": 1}, {}, transport=HfrTransport(FakeClient({})))
    with pytest.raises(BlockedError):
        f.fetch()
