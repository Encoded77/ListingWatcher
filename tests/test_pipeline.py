"""End to end with a fake source: notifies once, never twice for the same thing, drop ≥ 5 %, sold."""
from listingwatcher.fetchers.base import BaseFetcher
from listingwatcher.fetchers.http import BlockedError
from listingwatcher.models import FetchResult, Listing
from listingwatcher.pipeline import build_context, run_digest, run_scan
from listingwatcher.scheduler import next_digest, next_scan
from tests.conftest import with_watch
from datetime import datetime
from zoneinfo import ZoneInfo


class FakeSource(BaseFetcher):
    name = "lbc"
    supports_enrich = True

    def __init__(self):
        self.listings = []
        self.enriched = []
        self.blocked = False

    def fetch(self):
        if self.blocked:
            raise BlockedError("HTTP 403")
        return FetchResult("lbc", list(self.listings), True, [])

    def enrich(self, listing):
        self.enriched.append(listing.listing_id)
        listing.description = "SMART 4500 h, aucun secteur défectueux"
        return True


def L(lid, price, title="Seagate IronWolf ST8000VN004 8To", status="active", reviews=30):
    return Listing("lbc", lid, f"https://www.leboncoin.fr/ad/x/{lid}", title, price, shipping=6.9, fees=1.99,
                   status=status, seller_reviews=reviews, seller_rating=100, delivery=True, secure_payment=True,
                   seller_name="bob", location="Lyon 69000", condition="Très bon état")


def make_ctx(cfg):
    src = FakeSource()
    ctx = build_context(cfg, db_path=":memory:", dry_run=True, fetchers=[src])
    return ctx, src


def test_notify_once_then_price_drop_only(cfg):
    ctx, src = make_ctx(cfg)
    src.listings = [L("1", 230.0), L("2", 350.0), L("3", 200.0, title="Disque dur externe 8To")]
    s = run_scan(ctx)["lbc"]
    assert s["fetched"] == 3 and s["new"] == 3 and s["kept"] == 1 and s["notified"] == 1
    assert src.enriched == ["1"]            # only the kept listing is enriched
    sent = ctx.notifier.sent
    assert len(sent) == 1 and sent[0]["title"].startswith("[LBC] IronWolf ST8000VN004 — 238,89 €")
    assert "SMART (h) : 4500" in sent[0]["message"] and sent[0]["click"].endswith("/1")

    # same scan → nothing
    assert run_scan(ctx)["lbc"]["notified"] == 0
    # 3 % drop → nothing; 6 % drop → notification
    src.listings[0] = L("1", 224.0)
    assert run_scan(ctx)["lbc"]["notified"] == 0
    src.listings[0] = L("1", 215.0)
    assert run_scan(ctx)["lbc"]["notified"] == 1
    assert ctx.notifier.sent[-1]["title"].startswith("↓ [LBC]")
    # price back up, then same price → nothing
    src.listings[0] = L("1", 230.0)
    assert run_scan(ctx)["lbc"]["notified"] == 0


def test_ignored_then_price_drop_into_range_notifies_as_new(cfg):
    ctx, src = make_ctx(cfg)
    src.listings = [L("9", 350.0)]
    assert run_scan(ctx)["lbc"]["notified"] == 0
    src.listings = [L("9", 250.0)]
    assert run_scan(ctx)["lbc"]["notified"] == 1
    assert not ctx.notifier.sent[-1]["title"].startswith("↓")


def test_sold_not_notified_and_gone(cfg):
    ctx, src = make_ctx(cfg)
    src.listings = [L("1", 230.0, status="sold")]
    assert run_scan(ctx)["lbc"]["notified"] == 0
    src.listings = [L("2", 230.0)]
    run_scan(ctx)
    assert run_scan(ctx)["lbc"]["gone"] == 0        # "1" is sold, not "gone"; "2" is seen
    src.listings = []
    run_scan(ctx)
    assert run_scan(ctx)["lbc"]["gone"] == 1        # "2" missing two scans in a row
    assert ctx.store.get("lbc", "2")["status"] == "gone"


def test_suspicious_separate_and_never(cfg):
    ctx, src = make_ctx(cfg)
    src.listings = [L("1", 80.0, reviews=0)]
    run_scan(ctx)
    assert ctx.notifier.sent and ctx.notifier.sent[-1]["title"].startswith("⚠️")
    assert ctx.notifier.sent[-1]["priority"] == 2
    cfg2 = with_watch(cfg, notify={"suspicious": "never"})
    ctx, src = make_ctx(cfg2)
    src.listings = [L("1", 80.0, reviews=0)]
    run_scan(ctx)
    assert not ctx.notifier.sent


def test_blocked_source_paused_and_degraded_notice_once(cfg):
    ctx, src = make_ctx(cfg)
    src.blocked = True
    assert run_scan(ctx) == {}
    assert ctx.store.source_blocked_until("lbc") is not None
    sent = ctx.system_notifier.sent               # technical alert: system notifier, even for a silent watch
    assert len(sent) == 1 and "bloqué" in sent[0]["title"] and ctx.notifier.sent == []
    run_scan(ctx)
    assert len(sent) == 1          # paused: no new call, no new alert


def test_digest_once_a_day(cfg):
    ctx, src = make_ctx(cfg)
    src.listings = [L("1", 230.0), L("2", 200.0, status="pending"), L("3", 250.0)]
    run_scan(ctx)
    assert run_digest(ctx)
    msg = ctx.notifier.sent[-1]
    assert msg["title"].startswith("Digest Disques durs 8 To — 2 annonces")
    assert "/ad/x/1" in msg["message"] and "/ad/x/3" in msg["message"] and "/ad/x/2" not in msg["message"]
    assert msg["message"].index("/ad/x/1") < msg["message"].index("/ad/x/3")     # sorted by price
    assert not run_digest(ctx)
    assert run_digest(ctx, force=True)


def test_scheduler_slots():
    tz = ZoneInfo("Europe/Paris")
    now = datetime(2026, 9, 9, 13, 0, tzinfo=tz)
    nxt = next_scan(now, ["07:20", "12:30", "18:10", "22:30"], 12)
    assert abs((nxt - datetime(2026, 9, 9, 18, 10, tzinfo=tz)).total_seconds()) <= 12 * 60
    late = datetime(2026, 9, 9, 23, 30, tzinfo=tz)
    assert next_scan(late, ["07:20", "22:30"], 12).date().isoformat() == "2026-09-10"
    assert next_digest(now, "08:00", None) == datetime(2026, 9, 10, 8, 0, tzinfo=tz)
    early = datetime(2026, 9, 9, 6, 0, tzinfo=tz)
    assert next_digest(early, "08:00", None) == datetime(2026, 9, 9, 8, 0, tzinfo=tz)
    assert next_digest(early, "08:00", "2026-09-09") == datetime(2026, 9, 10, 8, 0, tzinfo=tz)
