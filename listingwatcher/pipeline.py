"""One scan: fetch → normalisation → filtering → persistence → notification, watch by watch.
Plus the daily digest.

`App` = the process: config, database, shared transports, one `Context` per watch. A watch's
pipeline (`run_scan`, `run_digest`) only sees its own context; `run_all` / `run_digests`
chain the watches sequentially so that the leboncoin client stays single and polite."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config import env, watch_source_cfg
from .fetchers import REGISTRY, BaseFetcher, build_fetchers, reset_transports
from .fetchers.http import BlockedError
from .filtering import Filter
from .models import FetchResult, Listing
from .profiles import load_profile
from .profiles.base import Profile
from .notify import Notifier
from .store import Store, WatchStore, utcnow

log = logging.getLogger("listingwatcher.pipeline")


@dataclass
class Context:
    """A watch ready to scan: its profile, its filter, its fetchers, its view of the database."""
    cfg: dict[str, Any]
    wcfg: dict[str, Any]
    store: WatchStore
    notifier: Notifier
    profile: Profile
    filter: Filter
    fetchers: list[BaseFetcher] = field(default_factory=list)
    system_notifier: Notifier | None = None       # technical alerts (blocked source), always active

    @property
    def watch(self) -> str:
        return self.wcfg["name"]

    @property
    def title(self) -> str:
        return self.wcfg["title"]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.cfg.get("timezone") or "Europe/Paris")

    def source_cfg(self, source_name: str) -> dict[str, Any]:
        """Effective source section (shared transport + the watch's searches) for a short name."""
        for key, cls in REGISTRY.items():
            if cls.name == source_name or key == source_name:
                return watch_source_cfg(self.cfg, self.wcfg, key) or (self.cfg.get("sources") or {}).get(key) or {}
        return (self.cfg.get("sources") or {}).get(source_name) or {}


@dataclass
class App:
    cfg: dict[str, Any]
    store: Store
    contexts: list[Context]
    transports: dict[str, Any] = field(default_factory=dict)
    system_notifier: Notifier | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.cfg.get("timezone") or "Europe/Paris")

    def context(self, watch: str) -> Context:
        for c in self.contexts:
            if c.watch == watch:
                return c
        raise KeyError(f"veille inconnue : {watch} (disponibles : {', '.join(c.watch for c in self.contexts)})")

    def close(self) -> None:
        self.store.close()


def _notifier(cfg: dict[str, Any], wcfg: dict[str, Any], profile: Profile | None, dry_run: bool,
              enabled: bool = True, digest_title: str = "Digest annonces", label: str = "") -> Notifier:
    ncfg = wcfg.get("notify") or {}
    return Notifier(env("NTFY_URL"), str(ncfg.get("topic") or env("NTFY_TOPIC")), env("NTFY_TOKEN"), dry_run=dry_run,
                    source_labels=(cfg.get("notify") or {}).get("source_labels") or {},
                    profile=profile, digest_title=digest_title, enabled=enabled, label=label)


def build_app(cfg: dict[str, Any], db_path: str | None = None, dry_run: bool = False,
              only_source: str | None = None, only_watch: str | None = None,
              fetchers: list[BaseFetcher] | None = None, transports: dict[str, Any] | None = None) -> App:
    """`transports`: source clients of a previous App, reused as they are on a hot reload
    (same transport = same leboncoin politeness, same eBay token)."""
    db_path = db_path or env("LISTINGWATCHER_DB", "data/listingwatcher.sqlite")
    store = Store(db_path, history_days=int((cfg.get("market") or {}).get("history_days", 30)))
    watches = cfg["watches"]
    if only_watch:
        if only_watch not in watches:
            raise ValueError(f"veille inconnue : {only_watch} (disponibles : {', '.join(watches)})")
        watches = {only_watch: watches[only_watch]}
    notifying = [w for w in cfg["watches"].values() if (w.get("notify") or {}).get("enabled", True)]
    system = _notifier(cfg, {}, None, dry_run)
    app = App(cfg, store, [], transports=transports if transports is not None else {}, system_notifier=system)
    for name, wcfg in watches.items():
        profile = load_profile(wcfg)
        ncfg = wcfg.get("notify") or {}
        label = str(ncfg.get("label") or "") if len(notifying) > 1 else str(ncfg.get("label") or "")
        notifier = _notifier(cfg, wcfg, profile, dry_run, enabled=bool(ncfg.get("enabled", True)),
                             digest_title=f"Digest {wcfg['title']}", label=label)
        wstore = store.for_watch(name)
        flt = Filter(wcfg, market=wstore, profile=profile)
        ctx = Context(cfg, wcfg, wstore, notifier, profile, flt, system_notifier=system)
        ctx.fetchers = list(fetchers) if fetchers is not None else build_fetchers(cfg, wcfg, only=only_source,
                                                                                    transports=app.transports)
        app.contexts.append(ctx)
    return app


def build_context(cfg: dict[str, Any], db_path: str | None = None, dry_run: bool = False,
                  fetchers: list[BaseFetcher] | None = None, only_source: str | None = None,
                  watch: str | None = None) -> Context:
    """Context of a single watch (the first one by default): handy for tests and `classify`."""
    watch = watch or next(iter(cfg["watches"]))
    return build_app(cfg, db_path, dry_run, only_source, only_watch=watch, fetchers=fetchers).contexts[0]


def evaluate(ctx: Context, listing: Listing):
    info = ctx.profile.classify(listing.title, listing.description, listing.condition_code)
    decision = ctx.filter.decide(listing, info)
    return info, decision


def process_result(ctx: Context, result: FetchResult, fetcher: BaseFetcher | None = None) -> dict[str, int]:
    notify_cfg = ctx.wcfg.get("notify") or {}
    drop_pct = float(notify_cfg.get("price_drop_pct", 5)) / 100
    suspicious_mode = str(notify_cfg.get("suspicious", "separate"))
    scfg = ctx.source_cfg(result.source)
    detail_budget = int(scfg.get("detail_max_per_scan", 12) or 0)

    counts = {"fetched": len(result.listings), "new": 0, "kept": 0, "notified": 0, "rejected": 0,
              "enriched": 0, "gone": 0}
    seen: set[str] = set()
    for listing in result.listings:
        seen.add(listing.listing_id)
        info, decision = evaluate(ctx, listing)
        prev = ctx.store.get(listing.source, listing.listing_id)
        is_new = prev is None

        # second pass: detail page only for a new, kept listing
        if (is_new and decision.keep and fetcher is not None and fetcher.supports_enrich
                and counts["enriched"] < detail_budget):
            try:
                if fetcher.enrich(listing):
                    counts["enriched"] += 1
                    info, decision = evaluate(ctx, listing)
            except BlockedError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("enrichment failed for %s: %s", listing.url, e)

        ctx.store.upsert(listing, info, decision)
        if is_new:
            counts["new"] += 1
        if not decision.keep:
            counts["rejected"] += 1
            continue
        counts["kept"] += 1
        if not ctx.notifier.enabled:
            continue

        current = ctx.store.get(listing.source, listing.listing_id) or {}
        if current.get("status") != "active" or (current.get("review") or "") == "ignored":
            continue
        notified_price = prev.get("notified_price") if prev else None
        event = None
        if notified_price is None:
            event = "new"
        elif decision.unit_price <= notified_price * (1 - drop_pct):
            event = "price_drop"
        if event is None:
            continue
        if decision.suspicious and suspicious_mode == "never":
            log.info("suspicious, not notified: %s (%s)", listing.title, "; ".join(decision.suspicious))
            continue
        if ctx.notifier.listing_alert(listing, info, decision, event, prev_unit=notified_price):
            ctx.store.mark_notified(listing.source, listing.listing_id, decision.unit_price)
            counts["notified"] += 1
        log.info("%s %s/%s | %s | %.0f €/item | %s", event.upper(), ctx.watch, listing.source, info.label,
                 decision.unit_price, listing.url)

    if result.complete:
        gone_after = int(scfg.get("gone_after_missing_scans", 2))
        counts["gone"] = ctx.store.mark_missing(result.source, seen, gone_after)
    return counts


def run_scan(ctx: Context) -> dict[str, dict[str, int]]:
    """Scan of one watch, source by source. A blocked source is blocked for every watch."""
    summary: dict[str, dict[str, int]] = {}
    system = ctx.system_notifier or ctx.notifier
    for fetcher in ctx.fetchers:
        name = fetcher.name
        until = ctx.store.source_blocked_until(name)
        if until:
            log.warning("source %s paused until %s", name, until.isoformat())
            continue
        scfg = ctx.source_cfg(name)
        try:
            result = fetcher.fetch()
        except BlockedError as e:
            hours = float(scfg.get("cooldown_hours_when_blocked", 12))
            ctx.store.block_source(name, hours, str(e))
            today = datetime.now(ctx.tz).date().isoformat()
            if ctx.store.get_meta(f"degraded_notified:{name}") != today:
                system.degraded(name, str(e), hours)
                ctx.store.set_meta(f"degraded_notified:{name}", today)
            log.error("source %s blocked (%s), pausing %.0f h", name, e, hours)
            continue
        except Exception as e:  # noqa: BLE001
            log.exception("watch %s, source %s: scan failed (%s)", ctx.watch, name, e)
            continue
        if result.complete:
            ctx.store.source_ok(name)
        try:
            counts = process_result(ctx, result, fetcher)
        except BlockedError as e:
            hours = float(scfg.get("cooldown_hours_when_blocked", 12))
            ctx.store.block_source(name, hours, str(e))
            log.error("source %s blocked during enrichment (%s), pausing %.0f h", name, e, hours)
            continue
        summary[name] = counts
        log.info("scan %s/%s: %s", ctx.watch, name, counts)
    ctx.store.set_meta("last_scan", utcnow().isoformat())
    ctx.store.set_meta(f"last_scan:{ctx.watch}", utcnow().isoformat())
    return summary


def run_all(app: App) -> dict[str, dict[str, dict[str, int]]]:
    """Every watch, one after the other. Returns {watch: {source: counts}}."""
    reset_transports(app.transports)
    out: dict[str, dict[str, dict[str, int]]] = {}
    for ctx in app.contexts:
        out[ctx.watch] = run_scan(ctx)
    return out


def run_digest(ctx: Context, force: bool = False) -> bool:
    today = datetime.now(ctx.tz).date().isoformat()
    key = f"digest_date:{ctx.watch}"
    if not force and ctx.store.get_meta(key) == today:
        return False
    if not ctx.notifier.enabled:
        ctx.store.set_meta(key, today)
        return False
    rows = ctx.store.digest_rows()
    sent = ctx.notifier.digest(rows, bool((ctx.cfg.get("schedule") or {}).get("digest_send_if_empty", False)))
    ctx.store.set_meta(key, today)
    log.info("digest %s: %s active listings, sent=%s", ctx.watch, len(rows), sent)
    return sent


def run_digests(app: App, force: bool = False) -> bool:
    sent = False
    for ctx in app.contexts:
        sent = run_digest(ctx, force) or sent
    app.store.set_meta("digest_date", datetime.now(app.tz).date().isoformat())
    return sent
