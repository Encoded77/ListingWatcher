"""Plusieurs veilles dans un seul processus : base isolée par veille et migrée, transport partagé,
veille silencieuse, interface à onglets, profil pc."""
import json
import sqlite3
import urllib.error
import urllib.request

import pytest

from listingwatcher.filtering import Filter
from listingwatcher.models import Decision, Listing, ModelInfo
from listingwatcher.normalize import norm
from listingwatcher.profiles import load_profile
from listingwatcher.profiles.pc import best_cpu, parse_cpus, parse_memory
from listingwatcher.store import Store
from listingwatcher.web import WebApp, WebServer
from tests.test_pipeline import FakeSource, L
from tests.test_store import D, I
from tests.test_store import L as HL
from tests.test_web import _get, _post


# ----------------------------------------------------------------------------- base

def test_watches_are_isolated():
    base = Store()
    a, b = base.for_watch("a"), base.for_watch("b")
    a.upsert(HL(price=200), I(), D(208.89))
    assert b.get("lbc", "1") is None
    b.upsert(HL(price=100), I(), D(108.89))
    assert a.get("lbc", "1")["unit_price"] == 208.89 and b.get("lbc", "1")["unit_price"] == 108.89
    assert base.watches() == ["a", "b"]
    assert a.stats()["listings"] == 1 and base.stats()["listings"] == 2
    # même annonce, deux veilles : la médiane et le digest restent par veille
    assert a.median_unit_price("ST8000VN004", "IronWolf") == (208.89, 1)
    assert b.median_unit_price("ST8000VN004", "IronWolf") == (108.89, 1)
    assert [r["watch"] for r in a.digest_rows()] == ["a"]
    assert b.mark_missing("lbc", set(), gone_after=1) == 1
    assert a.get("lbc", "1")["status"] == "active" and b.get("lbc", "1")["status"] == "gone"
    assert base.list_listings(watch="a", status="") and not base.list_listings(watch="b", status="active")


def test_migration_from_single_watch_schema(tmp_path):
    db = str(tmp_path / "old.sqlite")
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE listings (source TEXT NOT NULL, listing_id TEXT NOT NULL, url TEXT, title TEXT, model TEXT, family TEXT,
            verdict TEXT, reasons TEXT, flags TEXT, price REAL, shipping REAL, fees REAL, delivered REAL, unit_price REAL,
            quantity INTEGER, tier TEXT, keep INTEGER, suspicious TEXT, condition TEXT, seller TEXT, seller_rating REAL,
            seller_reviews INTEGER, location TEXT, delivery INTEGER, smart_hours INTEGER, status TEXT, first_seen TEXT,
            last_seen TEXT, missed_scans INTEGER DEFAULT 0, notified_price REAL, notified_at TEXT, notify_count INTEGER DEFAULT 0,
            raw TEXT, review TEXT DEFAULT '', note TEXT DEFAULT '', PRIMARY KEY (source, listing_id));
        CREATE TABLE price_history (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, listing_id TEXT NOT NULL,
            seen_at TEXT NOT NULL, price REAL, delivered REAL, unit_price REAL, model TEXT, family TEXT);
        CREATE INDEX ix_price_history_model ON price_history (model, seen_at);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO listings (source, listing_id, title, model, family, unit_price, keep, status, review, note, notify_count, suspicious, quantity)
            VALUES ('lbc', '42', 'IronWolf', 'ST8000VN004', 'IronWolf', 210.0, 1, 'active', 'starred', 'a rappeler', 2, '[]', 1);
        INSERT INTO price_history (source, listing_id, seen_at, unit_price, model, family)
            VALUES ('lbc', '42', '2026-09-01T00:00:00+00:00', 210.0, 'ST8000VN004', 'IronWolf');
        INSERT INTO meta VALUES ('digest_date', '2026-09-09');
    """)
    con.commit()
    con.close()
    s = Store(db)
    row = s.get("hdd", "lbc", "42")
    assert row and row["review"] == "starred" and row["note"] == "a rappeler" and row["notify_count"] == 2
    assert row["posted_at"] is None and row["attrs"] is None          # colonnes ajoutées au passage
    assert s.get("minipc", "lbc", "42") is None
    assert s.price_history("hdd", "lbc", "42")[0]["unit_price"] == 210.0
    assert s.get_meta("digest_date") == "2026-09-09"
    # la clé primaire inclut la veille : la même annonce peut exister dans une autre veille
    s.for_watch("minipc").upsert(HL(price=100, lid="42"), I(), D(100))
    assert s.get("hdd", "lbc", "42")["unit_price"] == 210.0
    s.close()
    s2 = Store(db)                                                       # deuxième ouverture : migration idempotente
    assert s2.stats()["listings"] == 2 and s2.watches() == ["hdd", "minipc"]
    s2.close()


# ----------------------------------------------------------------------------- pipeline

def test_watches_do_not_mix_and_silent_watch_never_notifies(pc8_cfg):
    cfg = pc8_cfg
    """La même annonce vue par deux veilles : chacune la classe, la stocke et la notifie (ou non) de son côté."""
    from listingwatcher.pipeline import build_app, run_all, run_digests
    lbc = FakeSource()
    lbc.supports_enrich = False        # la description factice « SMART… » n'a pas de sens pour un PC
    lbc.listings = [L("1", 230.0), L("2", 150.0, title="Lenovo M720q i5-8500T 16Go 256Go SSD")]
    app = build_app(cfg, db_path=":memory:", dry_run=True, fetchers=[lbc])
    assert [c.watch for c in app.contexts] == ["hdd", "minipc"]
    hdd, pc = app.contexts
    assert hdd.notifier.enabled and not pc.notifier.enabled
    out = run_all(app)
    assert out["hdd"]["lbc"]["kept"] == 1 and out["hdd"]["lbc"]["notified"] == 1
    assert out["minipc"]["lbc"]["kept"] == 1 and out["minipc"]["lbc"]["notified"] == 0
    assert len(hdd.notifier.sent) == 1 and pc.notifier.sent == []
    assert hdd.store.get("lbc", "2")["keep"] == 0 and pc.store.get("lbc", "1")["keep"] == 0
    row = pc.store.get("lbc", "2")
    assert row["keep"] == 1 and row["family"] == "Lenovo M720q" and row["model"] == "i5-8500T"
    assert app.store.stats()["listings"] == 4 and hdd.store.stats()["listings"] == 2
    # une baisse de prix sur la veille silencieuse ne notifie toujours pas
    lbc.listings[1] = L("2", 100.0, title="Lenovo M720q i5-8500T 16Go 256Go SSD")
    assert run_all(app)["minipc"]["lbc"]["notified"] == 0 and pc.notifier.sent == []
    # digest : seule la veille qui notifie envoie ; la silencieuse est marquée faite sans rien envoyer
    assert run_digests(app)
    assert hdd.notifier.sent[-1]["title"].startswith("Digest Disques durs 8 To") and pc.notifier.sent == []
    assert not run_digests(app)
    assert app.store.get_meta("digest_date:minipc") and app.store.get_meta("digest_date")


def test_only_watch_and_unknown_watch(cfg):
    from listingwatcher.pipeline import build_app, build_context
    app = build_app(cfg, db_path=":memory:", dry_run=True, only_watch="minipc", fetchers=[])
    assert [c.watch for c in app.contexts] == ["minipc"] and app.contexts[0].profile.name == "pc"
    assert build_context(cfg, db_path=":memory:", dry_run=True, fetchers=[]).watch == "hdd"
    with pytest.raises(ValueError):
        build_app(cfg, db_path=":memory:", dry_run=True, only_watch="velo", fetchers=[])


def test_shared_transport_and_page_cache(cfg, lbc_page1):
    """Deux veilles avec la même recherche leboncoin : un seul client poli, la page n'est lue qu'une fois par scan."""
    from listingwatcher.fetchers import build_fetchers, reset_transports
    from listingwatcher.fetchers.leboncoin import LeboncoinFetcher
    from tests.conftest import fake_html
    from tests.test_leboncoin import FakeClient
    base = "https://www.leboncoin.fr/ck/ordinateurs/mini-pc"
    client = FakeClient({base: fake_html(dict(lbc_page1, max_pages=1))})
    transports = {"leboncoin": LeboncoinFetcher.make_transport({})}
    transports["leboncoin"].client = client
    search = {"searches": [{"category": "ordinateurs", "slug": "mini-pc"}]}
    c = {**cfg, "sources": {"leboncoin": {"enabled": True, "max_pages": 1}},
         "watches": {"a": {"name": "a", "sources": {"leboncoin": search}}, "b": {"name": "b", "sources": {"leboncoin": search}}}}
    fa = build_fetchers(c, c["watches"]["a"], transports=transports)
    fb = build_fetchers(c, c["watches"]["b"], transports=transports)
    assert fa[0].client is fb[0].client is client and fa[0].transport is fb[0].transport
    assert len(fa[0].fetch().listings) == len(fb[0].fetch().listings) > 0
    assert client.calls == [base]                       # page servie depuis le cache pour la 2e veille
    reset_transports(transports)
    fb[0].fetch()
    assert client.calls == [base, base]                 # nouveau scan : relue


# ----------------------------------------------------------------------------- web

@pytest.fixture
def multi(tmp_path):
    db = str(tmp_path / "m.sqlite")
    s = Store(db)
    for watch, lid, price in (("hdd", "1", 230.0), ("hdd", "2", 240.0), ("minipc", "1", 120.0)):
        l = Listing("lbc", lid, f"https://x/{watch}/{lid}", f"{watch} {lid}", price, shipping=6.9)
        s.for_watch(watch).upsert(l, ModelInfo("accept", "F", "M"), Decision(True, "default", "default", price + 6.9))
    s.set_meta("last_scan:minipc", "2026-09-10T10:00:00+00:00")
    s.close()
    app = WebApp(lambda: Store(db), title="Annonces", watches={
        "hdd": {"title": "Disques", "unit_divisor": 8, "unit_label": "€/To", "notify_enabled": True},
        "minipc": {"title": "Mini PC", "attr_labels": {"cpu": "CPU"}, "notify_enabled": False},
    })
    srv = WebServer(app, "127.0.0.1", 0)
    srv.start()
    yield srv
    srv.stop()


def test_watch_tabs_and_scoping(multi):
    srv = multi
    ws = json.loads(_get(srv, "/api/watches")[1])
    assert [w["name"] for w in ws] == ["hdd", "minipc"]
    assert ws[0]["kept_active"] == 2 and ws[0]["unit_label"] == "€/To" and ws[0]["notify_enabled"]
    assert ws[1]["kept_active"] == 1 and not ws[1]["notify_enabled"] and ws[1]["last_scan"].startswith("2026-09-10")
    rows = json.loads(_get(srv, "/api/listings")[1])                     # première veille par défaut
    assert [r["listing_id"] for r in rows] == ["1", "2"] and all(r["watch"] == "hdd" for r in rows)
    rows = json.loads(_get(srv, "/api/listings?watch=minipc")[1])
    assert [r["listing_id"] for r in rows] == ["1"] and rows[0]["unit_price"] == 126.9
    st = json.loads(_get(srv, "/api/stats?watch=minipc")[1])
    assert st["kept_active"] == 1 and st["title"] == "Mini PC" and st["attr_labels"] == {"cpu": "CPU"} and st["notify_enabled"] is False
    hist = json.loads(_get(srv, "/api/listings/lbc/1/history?watch=minipc")[1])
    assert len(hist) == 1 and hist[0]["unit_price"] == 126.9
    code, row = _post(srv, "/api/listings/lbc/1/review?watch=minipc", {"review": "starred"})
    assert code == 200 and row["watch"] == "minipc" and row["review"] == "starred"
    assert json.loads(_get(srv, "/api/listings?watch=hdd&review=starred")[1]) == []
    code, body = _post(srv, "/api/listings/lbc/1/review", {"review": "seen", "watch": "minipc"})
    assert code == 200 and body["review"] == "seen"
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/api/listings?watch=velo", timeout=5)
    assert e.value.code == 400


# ----------------------------------------------------------------------------- profil pc

def _score(s):
    c = best_cpu(parse_cpus(norm(s)))
    return (c.label, c.score) if c else None


def test_pc_cpu_scores():
    assert _score("i5-8400") == ("i5-8400", 10)
    assert _score("Core i5 8500T") == ("i5-8500T", 10)
    assert _score("i7-7700") == ("i7-7700", 10)
    assert _score("i3-10100") == ("i3-10100", 10)
    assert _score("i5-6500") == ("i5-6500", 8)
    assert _score("i5-12500T") == ("i5-12500T", 14)
    assert _score("Intel Core i5 de 8ème génération") == ("i5 8e gén.", 10)
    assert _score("Ryzen 5 PRO 2400GE") == ("Ryzen 5 PRO 2400GE", 10)
    assert _score("Ryzen 7 5700G") == ("Ryzen 7 5700G", 14)
    assert _score("Pentium G5400") == ("Pentium", None)
    assert _score("i5 ou i7-8700T au choix") == ("i7-8700T", 11)
    assert _score("Mini PC très rapide") is None
    assert _score("Dell OptiPlex 3060 Micro - I5 - 8 GO") == ("i5", None)       # gamme seule : génération inconnue
    assert _score("Core i5 vPro") == ("i5", None)


def test_pc_memory_parsing():
    assert parse_memory(norm("16Go RAM 256Go SSD")) == (16, 256, "SSD")
    assert parse_memory(norm("8 Go DDR4, SSD NVMe 512 Go")) == (8, 512, "SSD NVMe")
    assert parse_memory(norm("M720q 16go 1to")) == (16, 1000, "")
    assert parse_memory(norm("32 Go de mémoire, disque dur 500 Go")) == (32, 500, "HDD")
    assert parse_memory(norm("RAM 16Go SSD 256Go")) == (16, 256, "SSD")
    assert parse_memory(norm("SSD 256 Go, RAM 8 Go")) == (8, 256, "SSD")
    assert parse_memory(norm("i5 8500T")) == (None, None, "")
    assert parse_memory(norm("HP 800 G4 i5 8Go 500Go")) == (8, 500, "")
    assert parse_memory(norm("I5 - 8 GO - SSD 250 GO")) == (8, 250, "SSD")
    assert parse_memory(norm("i5-8500T / 8 Go RAM / SSD NVMe 256 Go")) == (8, 256, "SSD NVMe")
    assert parse_memory(norm("Core i5 | 16 Go RAM | 256 Go SSD")) == (16, 256, "SSD")


def test_pc_profile_classification(pc8_cfg):
    cfg = pc8_cfg
    p = load_profile(cfg["watches"]["minipc"])
    assert p.name == "pc" and p.min_cpu == "i5-8400" and p.min_score == 10 and p.unit_divisor is None

    info = p.classify("Lenovo ThinkCentre M720q Tiny i5-8500T 16Go 256Go SSD", "Windows 11, wifi")
    assert info.accepted and info.family == "Lenovo M720q" and info.model == "i5-8500T"
    assert info.label == "Lenovo M720q i5-8500T"
    assert info.attrs["ram_gb"] == 16 and info.attrs["storage_gb"] == 256 and info.attrs["storage"] == "SSD"
    assert info.attrs["cpu_score"] == 10 and info.flags == []
    assert p.attr_lines(info) == ["CPU : i5-8500T", "RAM (Go) : 16", "Stockage (Go) : 256", "Type : SSD", "Score CPU : 10"]

    assert p.classify("Dell OptiPlex 7060 Micro i7-8700T 32Go 512Go NVMe").family == "OptiPlex 7060 Micro"
    assert p.classify("HP ProDesk 400 G4 Mini i5-8500T 8Go 256Go").family == "HP ProDesk 400 G4 Mini"
    assert p.classify("HP EliteDesk 800 G4 mini i5 8500T 16go").family == "HP EliteDesk 800 G4 Mini"

    # plancher de performance, pas de CPU exact
    assert p.classify("OptiPlex 3060 Micro i3-10100T 8Go 256Go").accepted        # i3 10e gén. = i5 8e gén.
    assert p.classify("OptiPlex 3060 Micro i7-7700T 8Go 256Go").accepted
    assert p.classify("OptiPlex 3060 Micro i5-7500T 8Go 256Go").reasons == ["cpu_below:i5-7500T"]
    assert p.classify("OptiPlex 3060 Micro i3-8100T 8Go 256Go").reasons == ["cpu_below:i3-8100T"]
    assert p.classify("Mini PC Celeron J4125 8Go 128Go").reasons == ["cpu_below:Celeron"]
    assert p.classify("OptiPlex 3060 Micro i5-8500T 4Go 128Go").reasons == ["ram_below:4"]

    # informations manquantes : signalées, priorité basse via cpu_unknown, pas rejetées
    unk = p.classify("Lenovo M720q 16Go 256Go SSD")
    assert unk.accepted and "cpu_unknown" in unk.flags and unk.model is None and unk.label == "Lenovo M720q (réf. ?)"
    assert "cpu_unknown" in p.low_flags
    noram = p.classify("Lenovo M720q i5-8500T")
    assert noram.accepted and set(noram.flags) == {"ram_unknown", "storage_unknown"}
    bare = p.classify("Lenovo M720q i5-8500T barebone sans alim")
    assert bare.accepted and "barebone" in bare.flags and "no_psu" in bare.flags and "ram_unknown" not in bare.flags
    nosto = p.classify("M720q i5-8500T 8Go, sans disque")
    assert nosto.accepted and "no_storage" in nosto.flags and "storage_unknown" not in nosto.flags

    # châssis hors catalogue : accepté en priorité basse ; ni châssis ni CPU : pas un PC ciblé
    nuc = p.classify("Intel NUC i5-8259U 16Go 512Go")
    assert nuc.accepted and "family_unknown" in nuc.flags and nuc.family is None and nuc.model == "i5-8259U"
    assert p.classify("Seagate IronWolf ST8000VN004 8To").reasons == ["no_match"]
    assert p.classify("Mini PC 16Go 256Go très rapide").reasons == ["no_match"]

    # génération déduite du châssis quand le titre ne donne que la gamme
    g = p.classify("Dell OptiPlex 3060 Micro - I5 - 8 GO - SSD 250 GO")
    assert g.accepted and g.model == "i5 (8e gén.)" and g.attrs["cpu_score"] == 10 and "cpu_gen_assumed" in g.flags
    assert "cpu_unknown" not in g.flags and g.attrs["ram_gb"] == 8 and g.attrs["storage_gb"] == 250
    assert p.classify("Pack Mini PC Dell OptiPlex 3060 Micro – i3, 16 Go RAM, SSD 1 To").reasons == ["cpu_below:i3 (8e gén.)"]
    assert p.classify("Intel NUC i5 16Go").reasons == ["no_match"]                    # ni châssis ni génération
    assert p.classify("Mini pc - i5").reasons == ["no_match"]
    assert p.classify("Recherche dell 3060 en i3 4 ou 8Go de Ram").reasons == ["wanted"]
    m = p.classify("Lenovo M720q — Intel i5 | 16 Go RAM | 256 Go SSD")
    assert m.accepted and m.model == "i5 (8e gén.)" and m.attrs["ram_gb"] == 16 and m.attrs["storage_gb"] == 256
    assert p.classify("Ordinateur - PC HP ProDesk 400G4 - Intel i5").family == "HP ProDesk 400 G4 Mini"

    # lots avec écran / clavier : gardés mais signalés ; processeur seul : rejeté
    b = p.classify("PC HP mini Prodesk 400 G4 i5-8500T + ecran HP E223")
    assert b.accepted and "bundle" in b.flags and "bundle" in p.low_flags
    assert p.classify("Processeur Intel i5 9500T").reasons == ["cpu_only"]
    assert p.classify("Lenovo M720q i5-8500T parfait pour serveur Home Assistant").accepted

    # rejets : formats non 1 litre, portables, morts, pièces
    assert p.classify("Dell OptiPlex 3060 SFF i5-8500 8Go").reasons == ["form:sff"]
    assert p.classify("Dell OptiPlex 7060 tour i7-8700").reasons == ["form:tour"]
    assert p.classify("Mini tour HP 260 G4 Desktop i5 10Th Gen 8g nvme 256go").reasons == ["form:mini tour"]
    assert p.classify("Micro tour Windows 11 - Lenovo Thinkcentre M720q Tiny - Core i5").accepted   # « tiny » subsiste
    assert p.classify("Dell OptiPlex 3060 MT i5-8500, micro casque offert").accepted   # mot « micro » : pas de rejet
    assert p.classify("HP ProBook 400 G4 i5-8250U 8Go").reasons[0].startswith("reject:probook")
    assert p.classify("Lenovo M720q i5-8500T HS pour pièces").reasons[0].startswith("reject:")
    assert not p.classify("Lenovo M720q i5-8500T", condition_code="parts").accepted
    lot = p.classify("Lot de 3 Lenovo M720q i5-8500T 8Go 256Go")
    assert lot.quantity == 3 and "lot" in lot.flags
    assert p.classify("Lenovo M720q x2 i5-8500T 8Go 256Go").quantity == 2


def test_pc_profile_end_to_end(pc8_cfg):
    cfg = pc8_cfg
    w = cfg["watches"]["minipc"]
    p = load_profile(w)
    flt = Filter(w, profile=p)
    l = Listing("lbc", "1", "u", "Lenovo M720q i5-8500T 16Go 256Go SSD", 140.0, shipping=9.9, seller_reviews=12, delivery=True,
                posted_at="2026-09-08 10:00:00")
    info = p.classify(l.title)
    d = flt.decide(l, info)
    assert d.keep and d.tier == "urgent" and d.priority == "urgent" and d.unit_price == 149.9 and d.per_unit is None
    l2 = Listing("lbc", "2", "u", "Lenovo M720q 16Go 256Go SSD", 140.0, shipping=9.9, seller_reviews=12, delivery=True)
    d2 = flt.decide(l2, p.classify(l2.title))
    assert d2.keep and d2.priority == "low" and "demander le processeur exact" in d2.notes
    l3 = Listing("lbc", "3", "u", "Lenovo M720q i5-8500T 16Go 256Go SSD", 260.0, shipping=9.9, seller_reviews=12, delivery=True)
    assert not flt.decide(l3, p.classify(l3.title)).keep                # > 250 € rendu
