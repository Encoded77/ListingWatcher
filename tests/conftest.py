import copy
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from listingwatcher.config import load_config  # noqa: E402
from listingwatcher.filtering import Filter  # noqa: E402
from listingwatcher.normalize import Classifier  # noqa: E402
from listingwatcher.profiles import load_profile  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def cfg():
    """Full config (all watches) as loaded from config.yaml."""
    return load_config(str(ROOT / "config.yaml"))


@pytest.fixture(scope="session")
def wcfg(cfg):
    """The hard-drive watch (tiers, notify, market, scam and profile)."""
    return cfg["watches"]["hdd"]


@pytest.fixture(scope="session")
def profile(wcfg):
    return load_profile(wcfg)


@pytest.fixture(scope="session")
def classifier(profile):
    return profile.classifier


@pytest.fixture
def flt(wcfg, profile):
    return Filter(wcfg, profile=profile)


def with_watch(cfg: dict, watch: str = "hdd", **overrides) -> dict:
    """Copy of the config with one section of a watch overridden: with_watch(cfg, notify={"suspicious": "never"})."""
    out = copy.deepcopy(cfg)
    w = out["watches"][watch]
    for key, val in overrides.items():
        w[key] = {**w.get(key, {}), **val} if isinstance(val, dict) else val
    return out


@pytest.fixture(scope="session")
def pc8_cfg(cfg):
    """Config where the minipc watch keeps its original settings (8th gen, i5-8400, 8 GB, 250 € max): the pc profile
    tests cover its logic, not the shipped settings, which evolve."""
    out = copy.deepcopy(cfg)
    w = out["watches"]["minipc"]
    w["profile"]["pc"].update(min_cpu="i5-8400", min_ram_gb=8, min_storage_gb=0)
    w["thresholds"] = [{"max_delivered": 150, "priority": "urgent", "tags": ["fire"]},
                       {"max_delivered": 200, "priority": "default", "tags": []},
                       {"max_delivered": 250, "priority": "low", "tags": []}]
    return out


@pytest.fixture(scope="session")
def lbc_page1():
    return json.loads((FIXTURES / "lbc_page1.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def lbc_page2():
    return json.loads((FIXTURES / "lbc_page2.json").read_text(encoding="utf-8"))


def fake_html(search_data: dict) -> str:
    payload = {"props": {"pageProps": {"searchData": search_data}}}
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></body></html>'


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    for k in ("NTFY_URL", "NTFY_TOPIC", "NTFY_TOKEN", "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
