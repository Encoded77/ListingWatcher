import json
import urllib.error
import urllib.request

import pytest

from listingwatcher.models import Decision, Listing, ModelInfo
from listingwatcher.store import Store
from listingwatcher.web import WebApp, WebServer


@pytest.fixture
def server(tmp_path):
    db = str(tmp_path / "t.sqlite")
    s = Store(db).for_watch("hdd")
    for lid, price, status, keep in (("1", 230.0, "active", True), ("2", 350.0, "active", False), ("3", 200.0, "sold", True)):
        l = Listing("lbc", lid, f"https://x/{lid}", f"IronWolf ST8000VN004 {lid}", price, shipping=6.9, status=status,
                    seller_name="bob", location="Lyon")
        info = ModelInfo("accept", "IronWolf", "ST8000VN004", "Seagate")
        s.upsert(l, info, Decision(keep, "default" if keep else "ignore", "default", price + 6.9, 30.0))
    s.set_meta("last_scan", "2026-09-09T10:00:00+00:00")
    s.close()
    triggered = []
    app = WebApp(lambda: Store(db), title="Test", scan_trigger=lambda: triggered.append(1) or True,
                 scan_running=lambda: False)
    srv = WebServer(app, "127.0.0.1", 0)
    srv.start()
    yield srv, triggered
    srv.stop()


def _get(srv, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}{path}", timeout=5) as r:
        return r.status, r.read().decode("utf-8"), r.headers.get("Content-Type", "")


def _post(srv, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_page_and_health(server):
    srv, _ = server
    status, body, ctype = _get(srv, "/")
    assert status == 200 and "text/html" in ctype and "<title>Test</title>" in body and "__TB__" not in body
    status, body, _ = _get(srv, "/api/health")
    assert status == 200 and json.loads(body)["ok"]


def test_listings_filters(server):
    srv, _ = server
    rows = json.loads(_get(srv, "/api/listings")[1])
    assert [r["listing_id"] for r in rows] == ["1"]           # active + kept by default
    rows = json.loads(_get(srv, "/api/listings?status=&keep=")[1])
    assert len(rows) == 3 and "raw" not in rows[0]
    rows = json.loads(_get(srv, "/api/listings?status=sold")[1])
    assert [r["listing_id"] for r in rows] == ["3"]
    rows = json.loads(_get(srv, "/api/listings?keep=0")[1])
    assert [r["listing_id"] for r in rows] == ["2"]
    rows = json.loads(_get(srv, "/api/listings?status=&keep=&q=VN004%203")[1])
    assert [r["listing_id"] for r in rows] == ["3"]


def test_review_and_stats(server):
    srv, _ = server
    code, row = _post(srv, "/api/listings/lbc/1/review", {"review": "starred", "note": "à appeler"})
    assert code == 200 and row["review"] == "starred" and row["note"] == "à appeler"
    stats = json.loads(_get(srv, "/api/stats")[1])
    assert stats["starred"] == 1 and stats["kept_active"] == 1 and stats["last_scan"].startswith("2026-09-09")
    code, _ = _post(srv, "/api/listings/lbc/1/review", {"review": "bogus"})
    assert code == 400
    rows = json.loads(_get(srv, "/api/listings?review=starred")[1])
    assert [r["listing_id"] for r in rows] == ["1"]
    code, row = _post(srv, "/api/listings/lbc/1/review", {"review": "ignored"})
    assert code == 200 and row["review"] == "ignored" and row["note"] == "à appeler"   # the note survives without a note field
    rows = json.loads(_get(srv, "/api/listings?review=none")[1])
    assert rows == []


def test_history_and_scan(server):
    srv, triggered = server
    hist = json.loads(_get(srv, "/api/listings/lbc/1/history")[1])
    assert len(hist) == 1 and hist[0]["unit_price"] == 236.9
    code, body = _post(srv, "/api/scan")
    assert code == 202 and body["started"] and triggered == [1]
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/api/nope", timeout=5)
        assert False, "404 expected"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_ignored_excluded_from_digest_and_notifications(cfg):
    from listingwatcher.pipeline import build_context, run_scan
    from tests.test_pipeline import FakeSource, L
    src = FakeSource()
    ctx = build_context(cfg, db_path=":memory:", dry_run=True, fetchers=[src])
    src.listings = [L("1", 230.0)]
    run_scan(ctx)
    assert len(ctx.notifier.sent) == 1
    ctx.store.set_review("lbc", "1", "ignored")
    src.listings = [L("1", 150.0)]                     # big drop, but the listing is ignored
    run_scan(ctx)
    assert len(ctx.notifier.sent) == 1
    assert ctx.store.digest_rows() == []
