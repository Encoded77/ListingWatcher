"""leboncoin: result pages /ck/<category>/<slug>[/p-N], server-side rendered.

The data lives in the ``__NEXT_DATA__`` JSON (props.pageProps.searchData.ads). It holds title, price,
condition, seller, rating, delivery, city, the "Vendu" / "Achat en cours" badges (transaction_status
attribute) and the id. The description (body) is empty there: it is only read on the /ad/... detail page
of kept listings, which exposes the same JSON (props.pageProps.ad).

Verified traps: pages are partially cached and a listing can appear active on one page and sold on
another (worst status wins); pagination overlaps (deduplication by list_id); a bare HTTP client gets
a DataDome challenge (403), hence the browser fingerprint.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from ..models import FetchResult, Listing, worst_status
from .base import BaseFetcher
from .http import BlockedError, FetchError, PoliteClient

log = logging.getLogger("listingwatcher.lbc")

SOURCE = "lbc"
BASE = "https://www.leboncoin.fr"
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


# ---------------------------------------------------------------------- parsing (pure, testable)

def extract_next_data(html: str) -> dict[str, Any]:
    m = _NEXT_DATA.search(html or "")
    if not m:
        raise FetchError("page sans __NEXT_DATA__ (défi anti-bot ou changement de structure)")
    return json.loads(m.group(1))


def parse_search_page(html: str) -> tuple[list[dict[str, Any]], int]:
    data = extract_next_data(html)
    sd = (data.get("props", {}).get("pageProps", {}) or {}).get("searchData") or {}
    return list(sd.get("ads") or []), int(sd.get("max_pages") or 1)


def parse_ad_page(html: str) -> Optional[dict[str, Any]]:
    data = extract_next_data(html)
    return (data.get("props", {}).get("pageProps", {}) or {}).get("ad")


def _attrs(ad: dict[str, Any]) -> dict[str, tuple[str, str]]:
    return {a.get("key"): (str(a.get("value") or ""), str(a.get("value_label") or a.get("value") or ""))
            for a in ad.get("attributes") or [] if a.get("key")}


def _condition_code(label: str) -> str:
    l = label.lower()
    if "pièce" in l or "piece" in l:
        return "parts"
    if "neuf" in l:
        return "new"
    return "used" if l else "unknown"


def estimate_shipping(weight_g: int, ship_cfg: dict[str, Any]) -> float:
    for limit, price in sorted(ship_cfg.get("by_weight_g") or [], key=lambda x: x[0]):
        if weight_g and weight_g <= int(limit):
            return float(price)
    return float(ship_cfg.get("default_eur", 6.9))


def ad_to_listing(ad: dict[str, Any], ship_cfg: dict[str, Any] | None = None) -> Listing:
    ship_cfg = ship_cfg or {}
    at = _attrs(ad)
    price_list = ad.get("price")
    price = float(price_list[0]) if isinstance(price_list, list) and price_list else float(ad.get("price_cents") or 0) / 100

    shippable = at.get("shippable", ("", ""))[0] == "true"
    shipping: Optional[float]
    estimated = False
    if shippable:
        cost = at.get("shipping_cost", ("", ""))[0]
        if cost and cost.isdigit() and int(cost) > 0:
            shipping = int(cost) / 100
        else:
            weight = int(at.get("estimated_parcel_weight", ("0", ""))[0] or 0)
            shipping = estimate_shipping(weight, ship_cfg)
            estimated = True
        fees = float((ad.get("buyer_fee") or {}).get("amount") or 0) / 100
    else:
        shipping, fees = 0.0, 0.0

    tx = at.get("transaction_status", ("", ""))[1].lower()
    if "vendu" in tx:
        status = "sold"
    elif "cours" in tx:
        status = "pending"
    elif (ad.get("status") or "active") != "active":
        status = "gone"
    else:
        status = "active"

    rating = at.get("rating_score", ("", ""))[0]
    reviews = at.get("rating_count", ("", ""))[0]
    owner = ad.get("owner") or {}
    loc = ad.get("location") or {}
    cond_label = at.get("condition", ("", ""))[1]
    stock = at.get("stock_quantity", ("", ""))[0]

    return Listing(
        source=SOURCE,
        listing_id=str(ad.get("list_id")),
        url=ad.get("url") or f"{BASE}/ad/{ad.get('category_id')}/{ad.get('list_id')}",
        title=ad.get("subject") or "",
        price=price,
        shipping=shipping,
        shipping_estimated=estimated,
        fees=fees,
        description=ad.get("body") or "",
        condition=cond_label,
        condition_code=_condition_code(cond_label),
        seller_name=owner.get("name") or "",
        seller_type="pro" if owner.get("type") == "pro" else "private",
        seller_rating=round(float(rating) * 100) if rating else None,
        seller_reviews=int(reviews) if reviews.isdigit() else None,
        location=loc.get("city_label") or loc.get("city") or "",
        country=loc.get("country_id") or "FR",
        delivery=shippable,
        secure_payment=at.get("purchase_cta_visible", ("", ""))[0] == "true",
        quantity=int(stock) if stock.isdigit() else None,
        status=status,
        posted_at=ad.get("first_publication_date") or "",
        raw={
            "category": ad.get("category_name"), "shipping_type": at.get("shipping_type", ("", ""))[0],
            "old_price": at.get("old_price", ("", ""))[0], "urgent": (ad.get("options") or {}).get("urgent"),
            "brand_attr": at.get("computer_accessories_brand", ("", ""))[1], "owner_type": owner.get("type"),
            "transaction_status": tx, "ad_status": ad.get("status"),
        },
    )


def merge_listings(into: dict[str, Listing], new: Listing) -> None:
    """Same listing seen twice (overlapping pagination, different slugs): worst status wins."""
    cur = into.get(new.listing_id)
    if cur is None:
        into[new.listing_id] = new
        return
    cur.status = worst_status(cur.status, new.status)
    if not cur.description and new.description:
        cur.description = new.description


# ---------------------------------------------------------------------- fetcher

class LbcTransport:
    """Polite client shared across watches + cache of the pages read during the current scan
    (two watches sharing a search or a detail page download it only once)."""

    def __init__(self, client: PoliteClient):
        self.client = client
        self.pages: dict[str, str] = {}

    def get_text(self, url: str) -> str:
        text = self.pages.get(url)
        if text is None:
            text = self.client.get(url).text
            self.pages[url] = text
        return text

    def reset(self) -> None:
        self.pages.clear()


class LeboncoinFetcher(BaseFetcher):
    name = SOURCE
    supports_enrich = True

    def __init__(self, scfg: dict[str, Any], cfg: dict[str, Any], client: PoliteClient | None = None,
                 transport: LbcTransport | None = None):
        super().__init__(scfg, cfg)
        self.searches = [s for s in (scfg.get("searches") or []) if s.get("category") and s.get("slug")]
        self.max_pages = int(scfg.get("max_pages", 3))
        self.ship_cfg = scfg.get("shipping_estimate") or {}
        self.detail_pages = bool(scfg.get("detail_pages", True))
        self.transport = transport or LbcTransport(client or self.make_client(scfg))

    @property
    def client(self) -> PoliteClient:
        return self.transport.client

    @staticmethod
    def make_client(scfg: dict[str, Any]) -> PoliteClient:
        return PoliteClient(
            min_delay=float(scfg.get("min_delay_s", 6)), max_delay=float(scfg.get("max_delay_s", 11)),
            respect_robots=bool(scfg.get("respect_robots", True)),
        )

    @staticmethod
    def make_transport(scfg: dict[str, Any]) -> LbcTransport:
        return LbcTransport(LeboncoinFetcher.make_client(scfg))

    @staticmethod
    def search_url(category: str, slug: str, page: int) -> str:
        url = f"{BASE}/ck/{category}/{slug}"
        return url if page <= 1 else f"{url}/p-{page}"

    def fetch(self) -> FetchResult:
        found: dict[str, Listing] = {}
        errors: list[str] = []
        complete = True
        for s in self.searches:
            for page in range(1, self.max_pages + 1):
                url = self.search_url(s["category"], s["slug"], page)
                try:
                    ads, max_pages = parse_search_page(self.transport.get_text(url))
                except BlockedError:
                    raise
                except FetchError as e:
                    log.warning("lbc %s: %s", url, e)
                    errors.append(f"{url}: {e}")
                    complete = False
                    break
                for ad in ads:
                    try:
                        merge_listings(found, ad_to_listing(ad, self.ship_cfg))
                    except Exception as e:  # noqa: BLE001 - a malformed listing must not break the scan
                        log.debug("listing skipped (%s): %s", e, str(ad)[:200])
                log.info("lbc %s/%s p%s: %s listings (%s total so far)", s["category"], s["slug"], page, len(ads), len(found))
                if page >= max_pages or not ads:
                    break
        return FetchResult(SOURCE, list(found.values()), complete, errors)

    def enrich(self, listing: Listing) -> bool:
        if not self.detail_pages:
            return False
        ad = parse_ad_page(self.transport.get_text(listing.url))
        if not ad:
            return False
        listing.description = ad.get("body") or listing.description
        at = _attrs(ad)
        tx = at.get("transaction_status", ("", ""))[1].lower()
        if "vendu" in tx:
            listing.status = worst_status(listing.status, "sold")
        elif "cours" in tx:
            listing.status = worst_status(listing.status, "pending")
        return True
