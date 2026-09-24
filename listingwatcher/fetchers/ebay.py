"""eBay through the official Browse API (OAuth client credentials), marketplaces EBAY_FR and EBAY_DE.

Search: /buy/browse/v1/item_summary/search with the conditionIds, price, deliveryCountry=FR filters,
and the X-EBAY-C-ENDUSERCTX header (contextualLocation) so that shipping is computed to France.
Quantity, return policy, MPN and description come from /buy/browse/v1/item/{id} (enrich),
called only for kept listings.
"""
from __future__ import annotations

import logging
import re
import time
from html import unescape
from typing import Any, Optional
from urllib.parse import quote

import httpx

from ..config import env
from ..models import FetchResult, Listing
from .base import BaseFetcher, MissingCredentials

log = logging.getLogger("listingwatcher.ebay")

SOURCE = "ebay"
_CONDITION_CODE = {
    "1000": "new", "1500": "new", "1750": "new",
    "2000": "refurb", "2010": "refurb", "2020": "refurb", "2030": "refurb", "2500": "refurb", "2750": "refurb",
    "3000": "used", "4000": "used", "5000": "used", "6000": "used", "7000": "parts",
}


def _html_to_text(html: str) -> str:
    text = re.sub(r"<(br|/p|/div|/li|/tr)[^>]*>", "\n", html or "", flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", unescape(text)).strip()


def summary_to_listing(item: dict[str, Any], marketplace: str) -> Optional[Listing]:
    price = item.get("price") or {}
    if (price.get("currency") or "EUR") != "EUR":
        return None
    shipping: Optional[float] = None
    for opt in item.get("shippingOptions") or []:
        cost = opt.get("shippingCost")
        if cost and cost.get("value") is not None and (cost.get("currency") or "EUR") == "EUR":
            shipping = float(cost["value"])
            break
    seller = item.get("seller") or {}
    loc = item.get("itemLocation") or {}
    cond_id = str(item.get("conditionId") or "")
    pct = seller.get("feedbackPercentage")
    return Listing(
        source=SOURCE,
        listing_id=str(item.get("itemId")),
        url=item.get("itemWebUrl") or "",
        title=item.get("title") or "",
        price=float(price.get("value") or 0),
        shipping=shipping,
        condition=item.get("condition") or "",
        condition_code=_CONDITION_CODE.get(cond_id, "unknown"),
        seller_name=seller.get("username") or "",
        seller_type="pro" if seller.get("sellerAccountType") == "BUSINESS" else "",
        seller_rating=float(pct) if pct not in (None, "") else None,
        seller_reviews=int(seller["feedbackScore"]) if seller.get("feedbackScore") is not None else None,
        location=", ".join(x for x in (loc.get("city"), loc.get("country")) if x),
        country=loc.get("country") or "",
        delivery=True,
        secure_payment=True,
        status="active",
        posted_at=item.get("itemCreationDate") or "",
        raw={
            "marketplace": marketplace, "conditionId": cond_id, "buyingOptions": item.get("buyingOptions"),
            "legacyItemId": item.get("legacyItemId"), "topRated": item.get("topRatedBuyingExperience"),
            "shippingOptions": item.get("shippingOptions"),
        },
    )


class EbayTransport:
    """HTTP client and OAuth token shared across watches."""

    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(timeout=30.0)
        self._token: Optional[str] = None
        self._token_exp = 0.0

    def token(self, base: str, client_id: str, client_secret: str) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self.http.post(
            f"{base}/identity/v1/oauth2/token",
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        data = r.json()
        self._token = data["access_token"]
        self._token_exp = time.time() + float(data.get("expires_in", 7200))
        return self._token

    def forget_token(self) -> None:
        self._token = None


class EbayFetcher(BaseFetcher):
    name = SOURCE
    supports_enrich = True

    def __init__(self, scfg: dict[str, Any], cfg: dict[str, Any], client_id: str | None = None,
                 client_secret: str | None = None, http: httpx.Client | None = None,
                 transport: EbayTransport | None = None):
        super().__init__(scfg, cfg)
        self.client_id = client_id or env("EBAY_CLIENT_ID")
        self.client_secret = client_secret or env("EBAY_CLIENT_SECRET")
        if not (self.client_id and self.client_secret):
            raise MissingCredentials("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET missing")
        sandbox = env("EBAY_ENV", "production").lower() == "sandbox"
        self.base = "https://api.sandbox.ebay.com" if sandbox else "https://api.ebay.com"
        self.transport = transport or EbayTransport(http)
        self.http = self.transport.http
        self.marketplaces = list(scfg.get("marketplaces") or ["EBAY_FR"])
        self.queries = list(scfg.get("queries") or [])
        self.category_ids = str(scfg.get("category_ids") or "")
        self.condition_ids = [str(c) for c in (scfg.get("condition_ids") or [])]
        self.price_min = scfg.get("price_min")
        self.price_max = scfg.get("price_max")
        self.zip = str(scfg.get("delivery_zip") or "75001")
        self.limit = int(scfg.get("limit", 200))
        self.max_pages = int(scfg.get("max_pages", 2))
        self.detail = bool(scfg.get("detail_for_kept", True))
        self.pause = float(scfg.get("pause_s", 0.5))

    @staticmethod
    def make_transport(scfg: dict[str, Any]) -> EbayTransport:
        return EbayTransport()

    # ------------------------------------------------------------------ auth
    def token(self) -> str:
        return self.transport.token(self.base, self.client_id, self.client_secret)

    def _headers(self, marketplace: str) -> dict[str, str]:
        ctx = quote(f"country=FR,zip={self.zip}", safe="")
        return {
            "Authorization": f"Bearer {self.token()}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace,
            "X-EBAY-C-ENDUSERCTX": f"contextualLocation={ctx}",
            "Accept": "application/json",
        }

    def _filter(self) -> str:
        parts = ["deliveryCountry:FR", "buyingOptions:{FIXED_PRICE}"]
        if self.condition_ids:
            parts.append("conditionIds:{" + "|".join(self.condition_ids) + "}")
        if self.price_min is not None or self.price_max is not None:
            lo = "" if self.price_min is None else str(self.price_min)
            hi = "" if self.price_max is None else str(self.price_max)
            parts.append(f"price:[{lo}..{hi}]")
            parts.append("priceCurrency:EUR")
        return ",".join(parts)

    # ------------------------------------------------------------------ API
    def search(self, marketplace: str, q: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        offset = 0
        for _ in range(self.max_pages):
            params: dict[str, Any] = {"q": q, "limit": self.limit, "offset": offset, "filter": self._filter()}
            if self.category_ids:
                params["category_ids"] = self.category_ids
            r = self.http.get(f"{self.base}/buy/browse/v1/item_summary/search", params=params,
                              headers=self._headers(marketplace))
            if r.status_code == 401:
                self.transport.forget_token()
                r = self.http.get(f"{self.base}/buy/browse/v1/item_summary/search", params=params,
                                  headers=self._headers(marketplace))
            r.raise_for_status()
            data = r.json()
            batch = data.get("itemSummaries") or []
            items.extend(batch)
            total = int(data.get("total") or 0)
            offset += self.limit
            if not batch or offset >= total:
                break
            time.sleep(self.pause)
        return items

    def fetch(self) -> FetchResult:
        found: dict[str, Listing] = {}
        errors: list[str] = []
        complete = True
        for mp in self.marketplaces:
            for q in self.queries:
                try:
                    items = self.search(mp, q)
                except httpx.HTTPStatusError as e:
                    msg = f"{mp} '{q}': HTTP {e.response.status_code} {e.response.text[:200]}"
                    log.warning("ebay %s", msg)
                    errors.append(msg)
                    complete = False
                    if e.response.status_code == 429:
                        return FetchResult(SOURCE, list(found.values()), False, errors)
                    continue
                except httpx.HTTPError as e:
                    errors.append(f"{mp} '{q}': {e}")
                    complete = False
                    continue
                n = 0
                for it in items:
                    l = summary_to_listing(it, mp)
                    if l is None:
                        continue
                    cur = found.get(l.listing_id)
                    if cur is None or l.delivered_price < cur.delivered_price:
                        found[l.listing_id] = l
                    n += 1
                log.info("ebay %s '%s': %s listings (%s total so far)", mp, q, n, len(found))
                time.sleep(self.pause)
        return FetchResult(SOURCE, list(found.values()), complete, errors)

    def enrich(self, listing: Listing) -> bool:
        if not self.detail:
            return False
        mp = (listing.raw or {}).get("marketplace") or self.marketplaces[0]
        r = self.http.get(f"{self.base}/buy/browse/v1/item/{quote(listing.listing_id, safe='')}",
                          headers=self._headers(mp))
        if r.status_code >= 400:
            log.info("ebay getItem %s → HTTP %s", listing.listing_id, r.status_code)
            return False
        item = r.json()
        parts = []
        if item.get("mpn"):
            parts.append(f"MPN: {item['mpn']}")
        for a in item.get("localizedAspects") or []:
            if a.get("name") and a.get("value"):
                parts.append(f"{a['name']}: {a['value']}")
        if item.get("description"):
            parts.append(_html_to_text(item["description"])[:4000])
        listing.description = "\n".join(parts)
        avail = item.get("estimatedAvailabilities") or []
        if avail and avail[0].get("estimatedAvailableQuantity") is not None:
            listing.quantity = int(avail[0]["estimatedAvailableQuantity"])
        rt = item.get("returnTerms") or {}
        if rt:
            if rt.get("returnsAccepted"):
                period = rt.get("returnPeriod") or {}
                listing.returns = f"acceptés {period.get('value', '')} {str(period.get('unit', '')).lower()}".strip()
            else:
                listing.returns = "non acceptés"
        if listing.shipping is None:
            for opt in item.get("shippingOptions") or []:
                cost = opt.get("shippingCost")
                if cost and cost.get("value") is not None:
                    listing.shipping = float(cost["value"])
                    break
        seller = item.get("seller") or {}
        if seller.get("sellerAccountType"):
            listing.seller_type = "pro" if seller["sellerAccountType"] == "BUSINESS" else "private"
        if item.get("itemEndDate"):
            listing.raw["itemEndDate"] = item["itemEndDate"]
        return True
