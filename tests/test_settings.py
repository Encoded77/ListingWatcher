"""Editable settings: working copy, lossless YAML round trip, validation, history,
web API and hot reload."""
import json
import re
import shutil
import urllib.error
import urllib.request

import pytest

from listingwatcher.config import load_config
from listingwatcher.pipeline import build_app
from listingwatcher.scheduler import ScanRunner
from listingwatcher.settings import ConflictError, SettingsError, SettingsManager, live_config_path
from listingwatcher.store import Store
from listingwatcher.web import WebApp, WebServer

from tests.conftest import ROOT

FORM_KEYS = ("title", "enabled", "notify", "market", "scam", "thresholds", "leboncoin", "ebay", "profile_type", "profile_yaml")


@pytest.fixture
def mgr(tmp_path):
    live = tmp_path / "config.yaml"
    assert SettingsManager.ensure_live(str(live), str(ROOT / "config.yaml"))
    assert not SettingsManager.ensure_live(str(live), str(ROOT / "config.yaml"))   # never overwritten afterwards
    return SettingsManager(str(live), str(ROOT / "config.yaml"))


def _form(snap, name):
    w = next(w for w in snap["watches"] if w["name"] == name)
    return {k: w[k] for k in FORM_KEYS}


def _text(m):
    with open(m.path, encoding="utf-8") as f:
        return f.read()


def test_live_path_follows_db(monkeypatch, tmp_path):
    monkeypatch.delenv("LISTINGWATCHER_LIVE_CONFIG", raising=False)
    assert live_config_path(str(tmp_path / "x.sqlite")) == str(tmp_path / "config.yaml")
    monkeypatch.setenv("LISTINGWATCHER_LIVE_CONFIG", "/data/cfg.yaml")
    assert live_config_path(str(tmp_path / "x.sqlite")) == "/data/cfg.yaml"


def test_unchanged_form_roundtrip_keeps_data_and_comments(mgr):
    before = load_config(mgr.path)
    snap = mgr.snapshot()
    for name in ("hdd", "minipc"):
        snap = mgr.save_watch(name, {"form": _form(snap, name), "version": snap["version"]})
    form = dict(snap["global"], thresholds=snap["thresholds"])
    snap = mgr.save_global({"form": form, "version": snap["version"]})
    after = load_config(mgr.path)
    before.pop("_path"), after.pop("_path")
    assert after == before
    text = _text(mgr)
    for comment in ("# 600 € delivered cap", "# Internal Hard Disk Drives", "# Tiers on the DELIVERED PRICE",
                    "# gen: Intel generation of the chassis", "# rejected form factors"):
        assert comment in text
    assert mgr.history() == []                          # nothing changed: no write, no history


def test_edit_inherit_keeps_neighbouring_comments(mgr):
    snap = mgr.snapshot()
    form = _form(snap, "minipc")
    form["notify"]["max_age_days"] = "60"
    form["profile_yaml"] = form["profile_yaml"].replace("min_ram_gb: 16 ", "min_ram_gb: 32 ")
    form["leboncoin"]["searches"].append({"category": "ordinateurs", "slug": "m720q-i5"})
    snap = mgr.save_watch("minipc", {"form": form, "version": snap["version"]})
    w = load_config(mgr.path)["watches"]["minipc"]
    assert w["notify"]["max_age_days"] == 60 and w["profile"]["pc"]["min_ram_gb"] == 32
    assert {"category": "ordinateurs", "slug": "m720q-i5"} in w["sources"]["leboncoin"]["searches"]
    text = _text(mgr)
    assert "min_ram_gb: 32              # unknown RAM" in text           # comment column kept
    assert text.index("max_age_days: 60") < text.index("# 600 € delivered cap") < text.index("thresholds:\n      - {max_delivered: 300")

    form = _form(snap, "minipc")
    form["notify"]["max_age_days"] = ""                 # empty = inherited: the key disappears
    snap = mgr.save_watch("minipc", {"form": form})
    w = load_config(mgr.path)["watches"]["minipc"]
    assert w["notify"]["max_age_days"] == 120           # root value
    assert "max_age_days: 60" not in _text(mgr) and "# 600 € delivered cap" in _text(mgr)
    assert [h["reason"] for h in mgr.history()] == ["veille minipc", "veille minipc"]


@pytest.mark.parametrize("patch, message", [
    ({"profile_yaml": "min_cpu: [ouvert"}, "YAML invalide"),
    ({"profile_type": "keywords", "profile_yaml": 'model_regex: "(("'}, "missing \\)"),
    ({"profile_type": "velo"}, "profil inconnu"),
    ({"thresholds": [{"max_delivered": "abc"}]}, "valeur invalide"),
    ({"thresholds": [{"max_delivered": 100, "priority": "max"}]}, "priorité"),
    ({"leboncoin": {"searches": [{"category": "Ordinateurs", "slug": "m720q"}]}}, "recherche"),
    ({"ebay": {"queries": "x", "category_ids": "abc"}}, "category_ids"),
    ({"leboncoin": None, "ebay": None}, "aucune source"),
    ({"title": ""}, "titre obligatoire"),
])
def test_invalid_watch_is_refused_and_file_untouched(mgr, patch, message):
    before = _text(mgr)
    form = {**_form(mgr.snapshot(), "minipc"), **patch}
    with pytest.raises(SettingsError, match=message):
        mgr.save_watch("minipc", {"form": form})
    assert _text(mgr) == before


def test_global_validation_and_conflict(mgr):
    snap = mgr.snapshot()
    form = dict(snap["global"], thresholds=[])
    form["schedule"] = dict(form["schedule"], scan_times="7h20, 25:00")
    with pytest.raises(SettingsError, match="HH:MM"):
        mgr.save_global({"form": form})
    form["schedule"]["scan_times"] = "7h20 13:05"
    snap2 = mgr.save_global({"form": form, "version": snap["version"]})
    assert load_config(mgr.path)["schedule"]["scan_times"] == ["07:20", "13:05"]
    with pytest.raises(ConflictError):
        mgr.save_global({"form": form, "version": snap["version"]})       # stale version
    assert snap2["version"] != snap["version"]


def test_create_duplicate_toggle_delete_restore(mgr):
    snap = mgr.snapshot()
    new = {"title": "Vélo route", "enabled": True, "notify": {"enabled": "false"},
           "thresholds": [{"max_delivered": "900", "priority": "default", "tags": "fire"}],
           "leboncoin": {"enabled": True, "searches": [{"category": "velos", "slug": "velo-route-carbone"}]},
           "ebay": None, "profile_type": "keywords",
           "profile_yaml": '# mots-clés\nrequire_any: ["carbone"]\nfamilies:\n  - {name: Canyon, any: [canyon]}\n'}
    with pytest.raises(SettingsError, match="nom de veille invalide"):
        mgr.save_watch("Vélo", {"form": new}, create=True)
    with pytest.raises(SettingsError, match="existe déjà"):
        mgr.save_watch("hdd", {"form": new}, create=True)
    snap = mgr.save_watch("velo", {"form": new, "version": snap["version"]}, create=True)
    cfg = load_config(mgr.path)
    assert cfg["watches"]["velo"]["thresholds"] == [{"max_delivered": 900, "priority": "default", "tags": ["fire"]}]
    assert "# mots-clés" in _text(mgr)
    snap = mgr.duplicate_watch("velo", "velo-2", snap["version"])
    assert "velo-2" not in load_config(mgr.path)["watches"]                # the copy is disabled
    assert next(w for w in snap["watches"] if w["name"] == "velo-2")["enabled"] is False
    snap = mgr.set_enabled("velo-2", True, snap["version"])
    assert "velo-2" in load_config(mgr.path)["watches"]
    snap = mgr.delete_watch("velo-2", snap["version"])
    assert [w["name"] for w in snap["watches"]] == ["hdd", "minipc", "velo"]
    # history: most recent first, ASCII file name
    reasons = [h["reason"] for h in mgr.history()]
    assert reasons[0] == "suppression velo 2" and reasons[-1] == "creation velo"
    snap = mgr.restore(mgr.history()[-1]["file"], snap["version"])        # before the creation
    assert "velo" not in load_config(mgr.path)["watches"]
    snap = mgr.restore(None, snap["version"])                             # the image's config.yaml
    assert _text(mgr) == (ROOT / "config.yaml").read_text(encoding="utf-8") and not snap["image_changed"]


def test_disabled_watch_is_still_validated(mgr):
    mgr.set_enabled("minipc", False, None)
    form = {**_form(mgr.snapshot(), "minipc"), "profile_type": "keywords", "profile_yaml": 'model_regex: "(("'}
    with pytest.raises(SettingsError):
        mgr.save_watch("minipc", {"form": form})
    with pytest.raises(SettingsError, match="aucune veille active"):
        mgr.set_enabled("hdd", False, None)


def test_yaml_mode_keeps_typed_comments(mgr):
    snap = mgr.snapshot()
    y = next(w for w in snap["watches"] if w["name"] == "minipc")["yaml"]
    assert y.startswith("title: Mini PC")
    y = y.replace("title: Mini PC", "title: Mini PC 1L    # renommé") + "# note en fin de veille\n"
    mgr.save_watch("minipc", {"yaml": y, "version": snap["version"]})
    text = _text(mgr)
    assert re.search(r"title: Mini PC 1L +# renommé", text) and "    # note en fin de veille" in text
    assert load_config(mgr.path)["watches"]["minipc"]["title"] == "Mini PC 1L"


def test_image_changed_banner(mgr, tmp_path):
    image = tmp_path / "image.yaml"
    shutil.copyfile(ROOT / "config.yaml", image)
    m = SettingsManager(mgr.path, str(image))
    assert not m.image_changed()
    image.write_text(image.read_text(encoding="utf-8") + "\n# something new\n", encoding="utf-8")
    assert m.image_changed()
    m.restore(None, None)
    assert not m.image_changed()


# ----------------------------------------------------------------------------- hot reload

def test_runner_reload_applies_now_or_after_scan(mgr, tmp_path, monkeypatch):
    cfg = load_config(mgr.path)
    app = build_app(cfg, db_path=str(tmp_path / "r.sqlite"), dry_run=True, fetchers=[])
    built = []

    def rebuild(new_cfg, old):
        built.append(new_cfg)
        return build_app(new_cfg, db_path=str(tmp_path / "r.sqlite"), dry_run=True, fetchers=[], transports=old.transports)

    runner = ScanRunner(app, rebuild)
    mgr.on_change = runner.reload
    snap = mgr.snapshot()
    form = _form(snap, "minipc")
    form["title"] = "Mini PC 1L"
    snap = mgr.save_watch("minipc", {"form": form})
    assert snap["reload"] == "applied" and runner.generation == 1
    assert runner.app.context("minipc").title == "Mini PC 1L" and runner.app.transports is app.transports

    monkeypatch.setattr("listingwatcher.scheduler.run_all", lambda a: {})
    runner.lock.acquire()                                  # a scan is running
    form["title"] = "Mini PC 2"
    snap = mgr.save_watch("minipc", {"form": form})
    assert snap["reload"] == "pending" and runner.app.context("minipc").title == "Mini PC 1L"
    runner.lock.release()
    runner.run_blocking()                                  # end of scan: the pending config applies
    assert runner.generation == 2 and runner.app.context("minipc").title == "Mini PC 2"


# ----------------------------------------------------------------------------- web API

@pytest.fixture
def server(mgr, tmp_path):
    db = str(tmp_path / "w.sqlite")
    Store(db).close()
    reloads = []

    def on_change(cfg):
        reloads.append(cfg)
        return "applied"

    mgr.on_change = on_change
    app = WebApp(lambda: Store(db), title="Annonces", watches={"hdd": {"title": "Disques"}}, settings=mgr)
    srv = WebServer(app, "127.0.0.1", 0)
    srv.start()
    yield srv, reloads
    srv.stop()


def _call(srv, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode("utf-8")
            return r.status, (json.loads(body) if "json" in r.headers.get("Content-Type", "") else body)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_settings_api(server):
    srv, reloads = server
    code, snap = _call(srv, "/api/settings")
    assert code == 200 and snap["editable"] and [w["name"] for w in snap["watches"]] == ["hdd", "minipc"]
    assert snap["profile_types"] == ["hdd", "keywords", "pc"] and "urgent" in snap["priorities"]
    form = _form(snap, "minipc")
    code, out = _call(srv, "/api/settings/classify", {"name": "minipc", "form": form, "title": "Lenovo M90q Gen 2 i5-11500T 16 Go 512 Go SSD",
                                                      "price": "360", "shipping": "8"})
    assert code == 200 and out["info"]["verdict"] == "accept" and out["decision"]["tier"] == "default"
    form["profile_yaml"] = form["profile_yaml"].replace("min_cpu: i5-10500T", "min_cpu: i7-12700T")   # unsaved draft
    code, out = _call(srv, "/api/settings/classify", {"name": "minipc", "form": form, "title": "Lenovo M90q Gen 2 i5-11500T 16 Go 512 Go SSD"})
    assert code == 200 and out["info"]["verdict"] == "reject" and not reloads
    form["title"] = "Mini PC 1L"
    code, out = _call(srv, "/api/settings/watches/minipc", {"form": form, "version": snap["version"]})
    assert code == 200 and out["reload"] == "applied" and len(reloads) == 1
    code, out = _call(srv, "/api/settings/watches/minipc", {"form": form, "version": snap["version"]})
    assert code == 409
    code, out = _call(srv, "/api/settings/watches/minipc", {"yaml": "title: [cassé"})
    assert code == 400 and "YAML invalide" in out["error"]
    code, out = _call(srv, "/api/settings/watches/velo/enabled", {"enabled": False})
    assert code == 400 and "inconnue" in out["error"]
    code, out = _call(srv, "/api/settings/nope", {})
    assert code == 404
    hist = _call(srv, "/api/settings")[1]["history"]
    code, text = _call(srv, f"/api/settings/history/{hist[0]['file']}")
    assert code == 200 and "title: Mini PC\n" in text
    code, out = _call(srv, "/api/settings/history/..%2Fconfig.yaml")
    assert code == 400


def test_settings_read_only_without_manager(tmp_path):
    db = str(tmp_path / "ro.sqlite")
    Store(db).close()
    srv = WebServer(WebApp(lambda: Store(db)), "127.0.0.1", 0)
    srv.start()
    try:
        code, snap = _call(srv, "/api/settings")
        assert code == 200 and snap["editable"] is False
        code, out = _call(srv, "/api/settings/global", {"form": {}})
        assert code == 400
    finally:
        srv.stop()


def test_page_has_settings_tab(server):
    srv, _ = server
    with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/", timeout=5) as r:
        page = r.read().decode("utf-8")
    assert "data-settings" in page and 'id="settings"' in page and "__SETTINGS_JS__" not in page
