"""HFR (forum.hardware.fr), "Achats & Ventes" sub-forums: topic list pages, server-side rendered.

robots.txt forbids the forum search (/forum1.php, /search.php) but allows the topic lists
(/hfr/AchatsVentes/<sub-forum>/liste_sujet-<N>.htm, most recently active first) and the topics
themselves. So the fetcher reads the first ``list_pages`` pages of each sub-forum (shared by every
watch during a scan), keeps the sale topics whose title contains one of the watch's queries, then
reads the topic's first page to find the price of the matching item in the opening post.

One listing = one topic. A seller's topic often lists several items: the "block" of the opening post
that mentions the query (from the matching line to the next separator line) becomes the description,
and its first price is the listing price. A topic without a readable price is skipped.

Topic pages are cached across scans as long as their last-post date does not change, so a topic that
nobody touched is not downloaded again.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from ..models import FetchResult, Listing
from ..normalize import norm
from .base import BaseFetcher
from .http import BlockedError, CachedTransport, FetchError, PoliteClient

log = logging.getLogger("listingwatcher.hfr")

SOURCE = "hfr"
BASE = "https://forum.hardware.fr"
PARIS = ZoneInfo("Europe/Paris")

_TOPIC_ID = re.compile(r"sujet_(\d+)_")
_DATE = re.compile(r"(\d{2})-(\d{2})-(\d{4})\D+(\d{2}):(\d{2})")
_SALE = re.compile(r"\bvds\b|\bvend[sre]?\b|\bvente\b")
_AGGREGATE = re.compile(r"\btopic\b|\btopik\b")
_SEPARATOR = re.compile(r"^\s*[-=_*~#]{5,}")
_PRICE = re.compile(
    r"(?<![\d,.])(\d{1,3}(?:[   .]\d{3})+|\d{1,6})(?:[,.]\d{1,2})?\s*(?:€|euros?\b|eur\b)", re.I)
_PRICE_LABEL = re.compile(r"\bprix\s*(?:demand[ée]\s*)?:?\s*(\d{1,3}(?:[   .]\d{3})+|\d{1,6})\b", re.I)
_TRANSACTIONS = re.compile(r"Transactions\s*\((\d+)\)")
_SHIP_IN = re.compile(r"fdp\s*-?\s*in\b|fdpin\b|€\s*in\b|port\s+(?:inclus|compris|offert)|fdp\s+(?:inclus|compris|offert)|livraison\s+(?:incluse|offerte)")
_SHIP_YES = re.compile(r"\benvoi\b|\bexp[ée]di|colissimo|mondial relay|relais|\bfdp\b|\blivraison\b")
_HAND_ONLY = re.compile(r"(?:main propre|\bmp\b|remise en main propre)\s+(?:uniquement|seulement)|pas d.envoi|aucun envoi")
_SOLD = re.compile(r"[\[(]\s*vendue?s?\s*[\])]|\bvendue?s?\s*(?:!|$)|^\s*vendue?s?\b")
_STRUCK = re.compile(r"~~.*?~~")
_PRICE_IN = re.compile(r"(\d{1,6})\s*(?:€|e|euros?)?\s*(?:fdp\s*)?-?\s*in\b")
_PRICE_WORD = re.compile(r"\bprix\b|\bpx\b|€\s*(?:out|in)\b|\bfdp\b")
_PENDING = re.compile(r"[\[(]\s*(?:r[ée]serv[ée]e?s?|resa)\s*[\])]|\br[ée]serv[ée]e?s?\s*(?:!|$)")


# ---------------------------------------------------------------------- parsing (pure, testable)

def parse_price(text: str) -> Optional[float]:
    """First price of a text ('Prix: 1 050€out', '650 euros', 'Prix : 700'), None if absent."""
    m = _PRICE.search(text or "") or _PRICE_LABEL.search(text or "")
    if not m:
        return None
    try:
        value = float(re.sub(r"[   .]", "", m.group(1)))
    except ValueError:
        return None
    return value if value > 0 else None


def parse_date(text: str) -> str:
    """'28-09-2026 à 16:42' (Paris time) → ISO 8601 with offset; '' if unreadable."""
    m = _DATE.search(text or "")
    if not m:
        return ""
    d, mo, y, h, mi = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi, tzinfo=PARIS).isoformat()
    except ValueError:
        return ""


def parse_topic_list(html: str) -> list[dict[str, Any]]:
    """Rows of a topic list page: id, title, url (first page), author, last post date, replies."""
    soup = BeautifulSoup(html or "", "lxml")
    out: list[dict[str, Any]] = []
    for row in soup.select("tr.sujet"):
        a = row.select_one("td.sujetCase3 a.cCatTopic")
        if not a or not a.get("href"):
            continue
        m = _TOPIC_ID.search(a["href"])
        if not m:
            continue
        author = row.select_one("td.sujetCase6")
        last = row.select_one("td.sujetCase9")
        replies = row.select_one("td.sujetCase7")
        href = a["href"]
        out.append({
            "id": m.group(1),
            "title": a.get_text(" ", strip=True),
            "url": href if href.startswith("http") else f"{BASE}{href}",
            "author": author.get_text(" ", strip=True).replace("​", "") if author else "",
            "last_post": parse_date(last.get_text(" ", strip=True)) if last else "",
            "sticky": "ligne_sticky" in (row.get("class") or []),
            "replies": int(replies.get_text(strip=True)) if replies and replies.get_text(strip=True).isdigit() else None,
        })
    return out


def parse_opening_post(html: str) -> Optional[dict[str, Any]]:
    """Text (one line per paragraph / line break) and seller transaction count of a topic's first post."""
    soup = BeautifulSoup(html or "", "lxml")
    table = soup.select_one("table.messagetable")
    if table is None:
        return None
    para = table.select_one("div[id^=para]")
    if para is None:
        return None
    for q in para.select("table.quote"):          # quoted forum rules, photo captions…
        q.decompose()
    for s in para.find_all(["s", "strike", "del"]):
        s.replace_with(f"~~{s.get_text(' ', strip=True)}~~")
    for br in para.find_all("br"):
        br.replace_with("\n")
    for p in para.find_all(["p", "div", "li"]):
        p.insert_after("\n")
    lines = [re.sub(r"[ \t ]+", " ", l).strip() for l in para.get_text().splitlines()]
    lines = [l for l in lines if l]
    side = table.select_one("td.messCase1")
    tm = _TRANSACTIONS.search(side.get_text(" ", strip=True)) if side else None
    return {"lines": lines, "transactions": int(tm.group(1)) if tm else None}


def query_words(query: str) -> list[str]:
    return [w for w in norm(query).split() if w]


def matches(text: str, words: list[str]) -> bool:
    t = f" {norm(text)} "
    return bool(words) and all(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", t) for w in words)


def item_block(lines: list[str], words: list[str], max_lines: int = 25) -> tuple[list[str], bool]:
    """Lines describing the item: from the first line mentioning every query word to the next separator.
    Returns (block, found); without a matching line, the post's first lines."""
    for i, line in enumerate(lines):
        if matches(line, words):
            block = [line]
            for nxt in lines[i + 1:i + max_lines]:
                if _SEPARATOR.match(nxt):
                    break
                block.append(nxt)
            return block, True
    return lines[:max_lines], False


def topic_to_listing(row: dict[str, Any], post: dict[str, Any], words: list[str]) -> Optional[Listing]:
    block, found = item_block(post["lines"], words)
    price, price_line = None, ""
    # a "Prix :" line wins over any other amount of the block (purchase price, new price…)
    labelled = [l for l in block if _PRICE_WORD.search(norm(l))]
    for line in labelled + block:
        price = parse_price(_STRUCK.sub(" ", line))   # struck-through parts: old price or sold item
        if price is not None:
            price_line = norm(_STRUCK.sub(" ", line))
            break
    if price is None and not found:
        # the title names the item but the post never does: accept a single-price post only
        prices = {p for p in (parse_price(l) for l in post["lines"]) if p is not None}
        price = prices.pop() if len(prices) == 1 else None
    if price is None:
        return None
    text = "\n".join(block)
    ntext, ntitle, nall = norm(text), norm(row["title"]), norm("\n".join(post["lines"]))
    status = "active"
    head = [norm(l) for l in block[:2]] if found else []
    struck = found and not _STRUCK.sub("", block[0]).strip(" ~-:")
    if re.search(r"\bvendue?s?\b", ntitle) or struck or any(_SOLD.search(l) for l in head):
        status = "sold"
    elif _PENDING.search(ntitle) or any(_PENDING.search(l) for l in head):
        status = "pending"
    shipping: Optional[float] = None
    both = _PRICE_IN.search(price_line)
    if both and float(both.group(1)) > price and "out" in price_line:
        shipping = float(both.group(1)) - price          # "55€ out, 60€ in": 5 € of shipping
    elif _SHIP_IN.search(ntext) or _SHIP_IN.search(nall):
        shipping = 0.0
    if _HAND_ONLY.search(nall):
        delivery: Optional[bool] = False
    elif _SHIP_YES.search(nall):
        delivery = True
    else:
        delivery = None
    return Listing(
        source=SOURCE,
        listing_id=row["id"],
        url=row["url"],
        title=row["title"],
        price=price,
        shipping=shipping,
        description=text[:2000],
        condition="Occasion",
        condition_code="used",
        seller_name=row.get("author", ""),
        seller_type="private",
        seller_reviews=post.get("transactions"),
        delivery=delivery,
        secure_payment=None,
        status=status,
        posted_at=row.get("last_post", ""),
        raw={"replies": row.get("replies")},
    )


# ---------------------------------------------------------------------- fetcher

class HfrTransport(CachedTransport):
    """Per-scan page cache + topic pages kept across scans while their last post date is unchanged."""

    def __init__(self, client: PoliteClient):
        super().__init__(client)
        self.topics: dict[str, tuple[str, str]] = {}      # url → (last_post, html)

    def get_topic(self, url: str, last_post: str) -> str:
        cached = self.topics.get(url)
        if cached and last_post and cached[0] == last_post:
            return cached[1]
        text = self.get_text(url)
        self.topics[url] = (last_post, text)
        if len(self.topics) > 2000:
            self.topics.pop(next(iter(self.topics)))
        return text


class HfrFetcher(BaseFetcher):
    name = SOURCE
    supports_enrich = False

    def __init__(self, scfg: dict[str, Any], cfg: dict[str, Any], client: PoliteClient | None = None,
                 transport: HfrTransport | None = None):
        super().__init__(scfg, cfg)
        self.queries = [str(q).strip() for q in (scfg.get("queries") or []) if str(q).strip()]
        subs = scfg.get("subcats") or ["Hardware"]
        self.subcats = [str(s).strip() for s in (subs if isinstance(subs, list) else [subs]) if str(s).strip()]
        self.list_pages = int(scfg.get("list_pages", 3))
        self.topic_max = int(scfg.get("topic_max_per_scan", 15))
        self.transport = transport or HfrTransport(client or self.make_client(scfg))

    @staticmethod
    def make_client(scfg: dict[str, Any]) -> PoliteClient:
        return PoliteClient(
            min_delay=float(scfg.get("min_delay_s", 8)), max_delay=float(scfg.get("max_delay_s", 15)),
            respect_robots=bool(scfg.get("respect_robots", True)),
        )

    @staticmethod
    def make_transport(scfg: dict[str, Any]) -> HfrTransport:
        return HfrTransport(HfrFetcher.make_client(scfg))

    @staticmethod
    def list_url(subcat: str, page: int) -> str:
        return f"{BASE}/hfr/AchatsVentes/{subcat}/liste_sujet-{page}.htm"

    def candidates(self, rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], list[str]]]:
        """Sale topics whose title contains one of the queries, with the matching query's words."""
        out = []
        for row in rows:
            title = norm(row["title"])
            if row.get("sticky") or not _SALE.search(title) or _AGGREGATE.search(title):
                continue
            for q in self.queries:
                words = query_words(q)
                if matches(row["title"], words):
                    out.append((row, words))
                    break
        return out

    def fetch(self) -> FetchResult:
        errors: list[str] = []
        complete = True
        rows: dict[str, dict[str, Any]] = {}
        for sub in self.subcats:
            for page in range(1, self.list_pages + 1):
                url = self.list_url(sub, page)
                try:
                    found = parse_topic_list(self.transport.get_text(url))
                except BlockedError:
                    raise
                except FetchError as e:
                    log.warning("hfr %s: %s", url, e)
                    errors.append(f"{url}: {e}")
                    complete = False
                    break
                if not found:
                    errors.append(f"{url}: aucun sujet lisible")
                    complete = False
                    break
                for r in found:
                    rows.setdefault(r["id"], r)
        listings: list[Listing] = []
        cands = self.candidates(list(rows.values()))
        if len(cands) > self.topic_max:
            complete = False                     # unread topics must not be marked gone
        for row, words in cands[:self.topic_max]:
            try:
                post = parse_opening_post(self.transport.get_topic(row["url"], row.get("last_post", "")))
            except BlockedError:
                raise
            except FetchError as e:
                log.warning("hfr %s: %s", row["url"], e)
                errors.append(f"{row['url']}: {e}")
                complete = False
                continue
            if not post:
                continue
            listing = topic_to_listing(row, post, words)
            if listing is None:
                log.info("hfr topic without a readable price, skipped: %s", row["title"])
                continue
            listings.append(listing)
        log.info("hfr %s: %s topics read, %s candidates, %s listings",
                 "+".join(self.subcats), len(rows), len(cands), len(listings))
        return FetchResult(SOURCE, listings, complete, errors)
