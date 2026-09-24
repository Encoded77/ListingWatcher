from datetime import datetime, timedelta, timezone

from listingwatcher.models import Decision, Listing, ModelInfo
from listingwatcher.store import Store


def L(price=200.0, status="active", lid="1"):
    return Listing("lbc", lid, "u", "IronWolf ST8000VN004", price, shipping=6.9, fees=1.99, status=status)


def I():
    return ModelInfo("accept", "IronWolf", "ST8000VN004", "Seagate")


def D(unit):
    return Decision(True, "default", "default", unit, round(unit / 8, 2))


def test_new_then_unchanged_then_drop():
    s = Store().for_watch("hdd")
    assert s.upsert(L(), I(), D(208.89))["event"] == "new"
    assert s.upsert(L(), I(), D(208.89))["event"] == "unchanged"
    ev = s.upsert(L(180), I(), D(188.89))
    assert ev["event"] == "price_drop" and ev["prev"]["unit_price"] == 208.89
    assert s.upsert(L(190), I(), D(198.89))["event"] == "price_up"
    row = s.get("lbc", "1")
    assert row["first_seen"] and row["last_seen"] and row["status"] == "active"
    assert s.stats()["price_points"] == 3


def test_worst_status_wins_and_sold_sticks():
    s = Store().for_watch("hdd")
    s.upsert(L(), I(), D(208.89))
    s.upsert(L(status="sold"), I(), D(208.89))
    assert s.get("lbc", "1")["status"] == "sold"
    # a cached page shows it "active" again: the sold status sticks
    s.upsert(L(status="active"), I(), D(208.89))
    assert s.get("lbc", "1")["status"] == "sold"


def test_pending_then_active_keeps_pending():
    s = Store().for_watch("hdd")
    s.upsert(L(status="pending"), I(), D(208.89))
    s.upsert(L(status="active"), I(), D(208.89))
    assert s.get("lbc", "1")["status"] == "pending"


def test_gone_after_missing_scans_and_reappear():
    s = Store().for_watch("hdd")
    s.upsert(L(), I(), D(208.89))
    assert s.mark_missing("lbc", set(), gone_after=2) == 0
    assert s.get("lbc", "1")["status"] == "active"
    assert s.mark_missing("lbc", set(), gone_after=2) == 1
    assert s.get("lbc", "1")["status"] == "gone"
    s.upsert(L(), I(), D(208.89))
    assert s.get("lbc", "1")["status"] == "active" and s.get("lbc", "1")["missed_scans"] == 0


def test_digest_rows_exclude_sold_rejected_suspicious():
    s = Store().for_watch("hdd")
    s.upsert(L(lid="a"), I(), D(208.89))
    s.upsert(L(lid="b", status="sold"), I(), D(208.89))
    s.upsert(L(lid="c"), ModelInfo("reject", reasons=["smr"]), Decision(False, "reject", "none", 208.89, 26.1))
    s.upsert(L(lid="d"), I(), Decision(True, "urgent", "low", 90.0, 11.25, suspicious=["prix trop bas"]))
    s.upsert(L(lid="e", price=150), I(), D(158.89))
    rows = s.digest_rows()
    assert [r["listing_id"] for r in rows] == ["e", "a"]


def test_median_unit_price_one_point_per_listing():
    s = Store().for_watch("hdd")
    s.upsert(L(lid="a", price=200), I(), D(200))
    s.upsert(L(lid="a", price=100), I(), D(100))    # last point of "a" = 100
    s.upsert(L(lid="b", price=300), I(), D(300))
    s.upsert(L(lid="c", price=250), I(), D(250))
    med, n = s.median_unit_price("ST8000VN004", "IronWolf")
    assert n == 3 and med == 250
    med, n = s.median_unit_price("INCONNU", "IronWolf")
    assert n == 3
    assert s.median_unit_price("X", "Y") == (None, 0)


def test_notified_and_meta_and_source_state():
    s = Store().for_watch("hdd")
    s.upsert(L(), I(), D(208.89))
    s.mark_notified("lbc", "1", 208.89)
    row = s.get("lbc", "1")
    assert row["notified_price"] == 208.89 and row["notify_count"] == 1
    s.set_meta("digest_date", "2026-09-09")
    assert s.get_meta("digest_date") == "2026-09-09"
    assert s.source_blocked_until("lbc") is None
    s.block_source("lbc", 12, "HTTP 403")
    until = s.source_blocked_until("lbc")
    assert until and until > datetime.now(timezone.utc) + timedelta(hours=11)
    s.source_ok("lbc")
    assert s.source_blocked_until("lbc") is None


def test_median_ignores_lots_rejects_and_suspicious():
    s = Store().for_watch("hdd")
    s.upsert(L(lid="a", price=200), I(), D(200))
    s.upsert(L(lid="b", price=300), I(), D(300))
    s.upsert(L(lid="c", price=250), I(), D(250))
    s.upsert(L(lid="d", price=250), I(), D(250))
    lot = ModelInfo("accept", "IronWolf", "ST8000VN004", "Seagate", quantity=4)
    s.upsert(L(lid="lot", price=250), lot, D(62.5))
    s.upsert(L(lid="scam", price=90), I(), Decision(True, "urgent", "low", 90.0, 11.25, suspicious=["prix"]))
    s.upsert(L(lid="rej", price=40), ModelInfo("reject", reasons=["smr"]), Decision(False, "reject", "none", 40.0, 5.0))
    med, n = s.median_unit_price("ST8000VN004", "IronWolf")
    assert n == 4 and med == 250
