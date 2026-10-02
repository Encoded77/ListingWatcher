"""Vinted: catalog search pages /catalog?search_text=..., server-side rendered.

Each result card carries an overlay link whose ``title`` attribute holds everything the search needs:
``"<title>, Marque: <brand>, État: <condition>, <price> €, <price with buyer protection> €"`` (extra
attributes such as ``Taille`` or ``Modèle`` may sit between the title and the condition). The second
price includes Vinted's buyer-protection fee; shipping is never shown on search pages, so it is
estimated from ``shipping_eur``.

The item page (/items/<id>-<slug>) exposes a JSON-LD ``Product`` block (description, availability) and,
in the React payload, ``is_reserved`` and the seller's ``feedback_reputation`` (0..1): read only for
kept listings.

Verified traps: the old JSON API (/api/v2/catalog/items) answers 404 since 2026, the search is fuzzy (an
"rtx 3090" query also returns other cards: the profile must filter), and results mix countries.
"""
from __future__ import annotations

import html as htmllib
import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urlencode

from ..models import FetchResult, Listing, worst_status
from .base import BaseFetcher
from .http import BlockedError, CachedTransport, FetchError, PoliteClient

log = logging.getLogger("listingwatcher.vinted")

SOURCE = "vinted"
BASE = "https://www.vinted.fr"

_CARD = re.compile(r'<a\s[^>]*data-testid="product-item-id-(\d+)--overlay-link"[^>]*>', re.S)
_ATTR = re.compile(r'([a-zA-Z-]+)="([^"]*)"')
_PRICES = re.compile(r",\s*([\d\s  .,]+?)\s*€(?:,\s*([\d\s  .,]+?)\s*€)?\s*$")
_PAIR = re.compile(r",\s*([A-ZÉÈ][\wéèêàç' ]{1,20}):\s*([^,]*)$")
_JSONLD = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_RESERVED = re.compile(r'\\?"is_reserved\\?":\s*true')
_REPUTATION = re.compile(r'\\?"feedback_reputation\\?":\s*([0-9.]+)')
_FEEDBACK_COUNT = re.compile(r'\\?"feedback_count\\?":\s*(\d+)')


# ---------------------------------------------------------------------- parsing (pure, testable)

def parse_price(text: str) -> Optional[float]:
    s = re.sub(r"[\s  ]", "", text or "")
    if not s:
        return None
    if "," in s and "." in s:      # the first separator groups thousands
        s = s.replace(",", "") if s.index(",") < s.index(".") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_card_title(text: str) -> Optional[dict[str, Any]]:
    """'RTX 3090, Marque: NVIDIA, État: Très bon état, 1085.00 €, 1139.95 €' → fields; None if no price."""
    text = htmllib.unescape(text or "").strip()
    m = _PRICES.search(text)
    if not m:
        return None
    price = parse_price(m.group(1))
    if price is None:
        return None
    total = parse_price(m.group(2)) if m.group(2) else None
    rest = text[:m.start()]
    attrs: dict[str, str] = {}
    while True:
        pm = _PAIR.search(rest)
        if not pm:
            break
        attrs[pm.group(1).strip()] = pm.group(2).strip()
        rest = rest[:pm.start()]
    return {"title": rest.strip(), "price": price, "total": total, "attrs": attrs}


def parse_catalog_page(html: str) -> list[dict[str, Any]]:
    """Result cards of a catalog page: id, url, title, price, total (with protection), attributes."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in _CARD.finditer(html or ""):
        item_id = m.group(1)
        if item_id in seen:
            continue
        tag = dict(_ATTR.findall(m.group(0)))
        card = parse_card_title(tag.get("title", ""))
        if not card:
            continue
        href = htmllib.unescape(tag.get("href", "")).split("?")[0]
        card["id"] = item_id
        card["url"] = href if href.startswith("http") else f"{BASE}{href or '/items/' + item_id}"
        seen.add(item_id)
        out.append(card)
    return out


def _condition_code(label: str) -> str:
    l = (label or "").lower()
    if "neuf" in l:
        return "new"
    return "used" if l else "unknown"


def card_to_listing(card: dict[str, Any], shipping_eur: Optional[float]) -> Listing:
    attrs = card.get("attrs") or {}
    total = card.get("total")
    fees = round(total - card["price"], 2) if total and total > card["price"] else 0.0
    cond = attrs.get("État", "")
    return Listing(
        source=SOURCE,
        listing_id=str(card["id"]),
        url=card["url"],
        title=card["title"],
        price=float(card["price"]),
        shipping=shipping_eur,
        shipping_estimated=shipping_eur is not None,
        fees=fees,
        condition=cond,
        condition_code=_condition_code(cond),
        seller_type="private",
        delivery=True,
        secure_payment=True,
        raw={"brand": attrs.get("Marque", ""), "attrs": attrs},
    )


def parse_item_page(html: str) -> dict[str, Any]:
    """Description, availability, reservation and seller reputation from an item page."""
    out: dict[str, Any] = {}
    for m in _JSONLD.finditer(html or ""):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            out["description"] = data.get("description") or ""
            offers = data.get("offers") or {}
            if isinstance(offers, dict):
                out["availability"] = str(offers.get("availability") or "")
            break
    out["reserved"] = bool(_RESERVED.search(html or ""))
    rm = _REPUTATION.search(html or "")
    if rm:
        out["reputation"] = round(float(rm.group(1)) * 100, 1)
    fm = _FEEDBACK_COUNT.search(html or "")
    if fm:
        out["feedback_count"] = int(fm.group(1))
    return out


# ---------------------------------------------------------------------- fetcher

class VintedFetcher(BaseFetcher):
    name = SOURCE
    supports_enrich = True

    def __init__(self, scfg: dict[str, Any], cfg: dict[str, Any], client: PoliteClient | None = None,
                 transport: CachedTransport | None = None):
        super().__init__(scfg, cfg)
        self.queries = [str(q).strip() for q in (scfg.get("queries") or []) if str(q).strip()]
        self.max_pages = int(scfg.get("max_pages", 1))
        self.price_min = scfg.get("price_min")
        self.price_max = scfg.get("price_max")
        ship = scfg.get("shipping_eur", 8.9)
        self.shipping_eur = None if ship in (None, "") else float(ship)
        self.detail_pages = bool(scfg.get("detail_pages", True))
        self.transport = transport or CachedTransport(client or self.make_client(scfg))

    @staticmethod
    def make_client(scfg: dict[str, Any]) -> PoliteClient:
        return PoliteClient(
            min_delay=float(scfg.get("min_delay_s", 5)), max_delay=float(scfg.get("max_delay_s", 10)),
            respect_robots=bool(scfg.get("respect_robots", True)),
        )

    @staticmethod
    def make_transport(scfg: dict[str, Any]) -> CachedTransport:
        return CachedTransport(VintedFetcher.make_client(scfg))

    def search_url(self, query: str, page: int) -> str:
        params: dict[str, Any] = {"search_text": query, "order": "newest_first"}
        if self.price_min not in (None, ""):
            params["price_from"] = int(float(self.price_min))
        if self.price_max not in (None, ""):
            params["price_to"] = int(float(self.price_max))
        if "price_from" in params or "price_to" in params:
            params["currency"] = "EUR"
        if page > 1:
            params["page"] = page
        return f"{BASE}/catalog?{urlencode(params)}"

    def fetch(self) -> FetchResult:
        found: dict[str, Listing] = {}
        errors: list[str] = []
        complete = True
        for q in self.queries:
            for page in range(1, self.max_pages + 1):
                url = self.search_url(q, page)
                try:
                    cards = parse_catalog_page(self.transport.get_text(url))
                except BlockedError:
                    raise
                except FetchError as e:
                    log.warning("vinted %s: %s", url, e)
                    errors.append(f"{url}: {e}")
                    complete = False
                    break
                if not cards and page == 1:
                    # a real search always has cards: an empty first page is a challenge or a new layout
                    errors.append(f"{url}: aucune annonce lisible")
                    complete = False
                for card in cards:
                    if card["id"] not in found:
                        found[card["id"]] = card_to_listing(card, self.shipping_eur)
                log.info("vinted '%s' p%s: %s listings (%s total so far)", q, page, len(cards), len(found))
                if not cards:
                    break
        return FetchResult(SOURCE, list(found.values()), complete, errors)

    def enrich(self, listing: Listing) -> bool:
        if not self.detail_pages:
            return False
        info = parse_item_page(self.transport.get_text(listing.url))
        if not info:
            return False
        listing.description = info.get("description") or listing.description
        availability = str(info.get("availability") or "").lower()
        if "soldout" in availability or "outofstock" in availability:
            listing.status = worst_status(listing.status, "sold")
        elif info.get("reserved"):
            listing.status = worst_status(listing.status, "pending")
        if info.get("reputation") is not None:
            listing.seller_rating = info["reputation"]
        if info.get("feedback_count") is not None:
            listing.seller_reviews = info["feedback_count"]
        return True
