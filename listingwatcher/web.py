"""Small web interface for reviewing listings (LAN, no authentication) + JSON API.

Deliberately standard-library only: a self-contained HTML page, a threaded HTTP server,
one SQLite connection per request. One tab per watch; the API takes `?watch=<watch>` (first
watch by default), listings are addressed by (watch, source, id). A "Settings" tab edits
the config (listingwatcher/settings.py) when the service runs on its live copy.
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__
from .config import LEGACY_WATCH
from .settings import ConflictError, SettingsError, SettingsManager
from .store import Store

log = logging.getLogger("listingwatcher.web")

REVIEWS = ("", "starred", "seen", "ignored")


class NotFound(Exception):
    pass


class WebApp:
    """State shared between requests: Store factory, scan trigger, watch descriptions.
    `title` and `watches` may be callables: they then follow config reloads."""

    def __init__(self, store_factory: Callable[[], Store], title: str | Callable[[], str] = "Annonces",
                 watches: dict[str, dict[str, Any]] | Callable[[], dict[str, dict[str, Any]]] | None = None,
                 scan_trigger: Optional[Callable[[], bool]] = None, scan_running: Optional[Callable[[], bool]] = None,
                 source_labels: dict[str, str] | None = None, settings: SettingsManager | None = None):
        self.store_factory = store_factory
        self._title = title
        self._watches = watches
        self.scan_trigger = scan_trigger
        self.scan_running = scan_running or (lambda: False)
        self.source_labels = source_labels or {}
        self.settings = settings

    @property
    def title(self) -> str:
        return self._title() if callable(self._title) else self._title

    @property
    def watches(self) -> dict[str, dict[str, Any]]:
        w = self._watches() if callable(self._watches) else self._watches
        return w or {LEGACY_WATCH: {"title": self.title}}

    def watch(self, q: dict[str, str]) -> str:
        w = q.get("watch") or next(iter(self.watches))
        if w not in self.watches:
            raise ValueError(f"veille inconnue : {w}")
        return w

    # ------------------------------------------------------------------ API
    def list_watches(self) -> list[dict[str, Any]]:
        s = self.store_factory()
        try:
            out = []
            for name, w in self.watches.items():
                out.append({
                    "name": name, "title": w.get("title") or name,
                    "unit_divisor": w.get("unit_divisor"), "unit_label": w.get("unit_label") or "",
                    "attr_labels": w.get("attr_labels") or {}, "notify_enabled": bool(w.get("notify_enabled", True)),
                    "kept_active": s.stats(name)["kept_active"], "new_today": s.count_new_today(name),
                    "starred": s.count_review(name, "starred"), "last_scan": s.get_meta(f"last_scan:{name}"),
                })
            return out
        finally:
            s.close()

    def stats(self, watch: str) -> dict[str, Any]:
        s = self.store_factory()
        try:
            w = self.watches[watch]
            out = s.stats(watch)
            out.update({
                "watch": watch, "title": w.get("title") or watch,
                "last_scan": s.get_meta("last_scan"), "last_scan_watch": s.get_meta(f"last_scan:{watch}"),
                "digest_date": s.get_meta(f"digest_date:{watch}"),
                "scan_running": bool(self.scan_running()), "version": __version__, "app_title": self.title,
                "sources": s.source_states(), "starred": s.count_review(watch, "starred"),
                "new_today": s.count_new_today(watch),
                "unit_divisor": w.get("unit_divisor"), "unit_label": w.get("unit_label") or "",
                "attr_labels": w.get("attr_labels") or {}, "notify_enabled": bool(w.get("notify_enabled", True)),
            })
            return out
        finally:
            s.close()

    def listings(self, q: dict[str, str]) -> tuple[list[dict[str, Any]], int]:
        """Filtered rows (capped at `limit`) and the real total for those filters."""
        s = self.store_factory()
        try:
            f = dict(watch=self.watch(q), status=q.get("status", "active"), keep=q.get("keep", "1"),
                     source=q.get("source", ""), review=q.get("review", ""), search=q.get("q", ""),
                     include_suspicious=q.get("suspicious", "1") != "0")
            limit = int(q.get("limit", "1000") or 1000)
            return s.list_listings(limit=limit, **f), s.count_listings(**f)
        finally:
            s.close()

    def history(self, watch: str, source: str, listing_id: str) -> list[dict[str, Any]]:
        s = self.store_factory()
        try:
            return s.price_history(watch, source, listing_id)
        finally:
            s.close()

    # ------------------------------------------------------------------ settings
    def settings_snapshot(self) -> dict[str, Any]:
        if self.settings is None:
            return {"editable": False, "reason": "config en lecture seule (lancée avec --config, ou mode sans copie éditable)"}
        return {"editable": True, "scan_running": bool(self.scan_running()), **self.settings.snapshot()}

    def settings_call(self, parts: list[str], body: dict[str, Any]) -> dict[str, Any]:
        """POST /api/settings/…; `parts` = path after `settings`."""
        s = self.settings
        if s is None:
            raise SettingsError("config en lecture seule")
        ver = body.get("version")
        if parts == ["global"]:
            return s.save_global(body)
        if parts == ["restore"]:
            return s.restore(body.get("file") or None, ver)
        if parts == ["classify"]:
            return self.classify(str(body.get("name") or "_test"), body)
        if parts == ["watches"]:
            return s.save_watch(str(body.get("name") or "").strip(), body, create=True)
        if len(parts) == 2 and parts[0] == "watches":
            return s.save_watch(parts[1], body)
        if len(parts) == 3 and parts[0] == "watches":
            name, action = parts[1], parts[2]
            if action == "duplicate":
                return s.duplicate_watch(name, str(body.get("new_name") or "").strip(), ver)
            if action == "enabled":
                return s.set_enabled(name, bool(body.get("enabled")), ver)
            if action == "delete":
                return s.delete_watch(name, ver)
        raise NotFound()

    def classify(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        """Classify a title with the watch's draft (unsaved); the market median comes from the
        database if the watch already has listings there."""
        from .filtering import Filter
        from .models import Listing
        from .profiles import load_profile
        wcfg = self.settings.preview_watch(name, body)
        profile = load_profile(wcfg)
        store = self.store_factory()
        try:
            flt = Filter(wcfg, market=store.for_watch(name), profile=profile)
            title = str(body.get("title") or "").strip()
            if not title:
                raise SettingsError("titre d'annonce à tester manquant")
            price = float(str(body.get("price") or 0).replace(",", ".") or 0)
            ship = body.get("shipping")
            ship = None if ship in (None, "") else float(str(ship).replace(",", "."))
            listing = Listing("test", "0", "", title, price, shipping=ship, description=str(body.get("desc") or ""))
            info = profile.classify(listing.title, listing.description, listing.condition_code)
            decision = flt.decide(listing, info)
        finally:
            store.close()
        return {"info": info.__dict__, "label": info.label, "decision": decision.__dict__,
                "per_unit": profile.format_per_unit(decision.per_unit) if price else "",
                "attrs": profile.attr_lines(info)}

    def set_review(self, watch: str, source: str, listing_id: str, review: str, note: Optional[str]) -> dict[str, Any]:
        if review not in REVIEWS:
            raise ValueError("review invalide")
        s = self.store_factory()
        try:
            s.set_review(watch, source, listing_id, review, note)
            return s.get(watch, source, listing_id) or {}
        finally:
            s.close()


def make_handler(app: WebApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"ListingWatcher/{__version__}"

        def log_message(self, fmt, *args):  # application log rather than stderr
            log.debug("%s %s", self.address_string(), fmt % args)

        # ---- helpers
        def _json(self, code: int, payload: Any, extra_headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _html(self, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _text(self, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw.decode("utf-8")) if raw else {}

        # ---- routes
        def do_GET(self):
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
            parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
            try:
                if not parts:
                    return self._html(PAGE.replace("__TITLE__", app.title))
                if parts == ["api", "health"]:
                    return self._json(200, {"ok": True, "version": __version__})
                if parts == ["api", "watches"]:
                    return self._json(200, app.list_watches())
                if parts == ["api", "settings"]:
                    return self._json(200, app.settings_snapshot())
                if len(parts) == 4 and parts[:3] == ["api", "settings", "history"] and app.settings is not None:
                    return self._text(app.settings.history_text(parts[3]))
                if parts == ["api", "stats"]:
                    return self._json(200, app.stats(app.watch(q)))
                if parts == ["api", "listings"]:
                    rows, total = app.listings(q)
                    return self._json(200, rows, {"X-Total-Count": str(total)})
                if len(parts) == 5 and parts[:2] == ["api", "listings"] and parts[4] == "history":
                    return self._json(200, app.history(app.watch(q), parts[2], parts[3]))
                return self._json(404, {"error": "not found"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                log.exception("GET %s", self.path)
                return self._json(500, {"error": str(e)})

        def do_POST(self):
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
            parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
            try:
                if parts == ["api", "scan"]:
                    if app.scan_trigger is None:
                        return self._json(501, {"error": "scan non disponible dans ce mode"})
                    started = app.scan_trigger()
                    return self._json(202 if started else 409, {"started": started})
                if len(parts) == 5 and parts[:2] == ["api", "listings"] and parts[4] == "review":
                    body = self._body()
                    watch = app.watch({"watch": body.get("watch") or q.get("watch") or ""})
                    row = app.set_review(watch, parts[2], parts[3], str(body.get("review", "")), body.get("note"))
                    return self._json(200, row)
                if parts[:2] == ["api", "settings"]:
                    return self._json(200, app.settings_call(parts[2:], self._body()))
                return self._json(404, {"error": "not found"})
            except NotFound:
                return self._json(404, {"error": "not found"})
            except ConflictError as e:
                return self._json(409, {"error": str(e)})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                log.exception("POST %s", self.path)
                return self._json(500, {"error": str(e)})

    return Handler


class WebServer:
    def __init__(self, app: WebApp, host: str = "0.0.0.0", port: int = 8080):
        self.httpd = ThreadingHTTPServer((host, port), make_handler(app))
        self.httpd.daemon_threads = True
        self.thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self) -> None:
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="web", daemon=True)
        self.thread.start()
        log.info("web interface on http://%s:%s/", *self.httpd.server_address[:2])

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ---------------------------------------------------------------------- page

PAGE = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--line:#334155;--fg:#e2e8f0;--muted:#94a3b8;--acc:#38bdf8;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--star:#facc15}
@media (prefers-color-scheme: light){:root{--bg:#f1f5f9;--card:#fff;--line:#cbd5e1;--fg:#0f172a;--muted:#64748b}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;padding:12px 18px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:2}
h1{font-size:18px;margin:0 8px 0 0}.stat{color:var(--muted)}.stat b{color:var(--fg)}
button,select,input{font:inherit;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:5px 9px}
button{cursor:pointer}button.primary{background:var(--acc);color:#0f172a;border-color:var(--acc);font-weight:600}button:disabled{opacity:.5;cursor:wait}
nav.tabs{display:flex;flex-wrap:wrap;gap:6px;padding:10px 18px 0}
nav.tabs button{border-radius:8px 8px 0 0;border-bottom:none;padding:7px 14px;background:var(--card);color:var(--muted)}
nav.tabs button.on{color:var(--fg);font-weight:600;box-shadow:inset 0 -3px 0 var(--acc)}nav.tabs button .n{color:var(--acc);margin-left:6px;font-weight:700}
nav.tabs button .mute{margin-left:6px;font-size:11px;color:var(--muted)}
.filters{display:flex;flex-wrap:wrap;gap:8px;padding:10px 18px;align-items:center}
main{padding:0 18px 40px}table{width:100%;border-collapse:collapse;background:var(--card);border-radius:8px;overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em;cursor:pointer;white-space:nowrap}
tr:hover td{background:rgba(148,163,184,.07)}td.price{font-weight:700;white-space:nowrap;font-size:15px}td.price small{display:block;color:var(--muted);font-weight:400;font-size:11px}
.pill{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;font-weight:600;margin:0 4px 2px 0;border:1px solid var(--line);color:var(--muted)}
.pill.urgent{background:#7f1d1d;color:#fecaca;border-color:#991b1b}.pill.default{background:#1e3a8a;color:#bfdbfe;border-color:#1d4ed8}.pill.low{background:#334155;color:#cbd5e1}
.pill.ignore,.pill.reject{color:var(--bad)}.pill.sold,.pill.pending,.pill.gone{color:var(--warn)}.pill.active{color:var(--ok)}.pill.susp{background:#78350f;color:#fde68a;border-color:#b45309}
.pill.flag{color:var(--muted)}.pill.lbc{color:#fb923c}.pill.ebay{color:#a78bfa}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}.title{font-weight:600}.sub{color:var(--muted);font-size:12px}
.actions button{padding:3px 7px;font-size:12px;margin-right:3px}.starred td{background:rgba(250,204,21,.07)}.ignored td{opacity:.45}
.note{width:100%;margin-top:4px;font-size:12px}.empty{padding:40px;text-align:center;color:var(--muted)}#msg{color:var(--warn)}
details{font-size:12px;color:var(--muted)}details ul{margin:4px 0 0 16px;padding:0}
#settings{display:none;grid-template-columns:220px minmax(0,1fr);gap:18px;padding:14px 18px 0}.setmode #settings{display:grid}
.setmode .filters,.setmode main,.setmode header .stat{display:none}nav.tabs button.gear{margin-left:auto}
#st-list{display:flex;flex-direction:column;gap:4px;position:sticky;top:76px;align-self:start}
#st-list button{text-align:left;background:var(--card);display:flex;flex-direction:column;gap:1px;color:var(--muted)}#st-list button.on{box-shadow:inset 3px 0 0 var(--acc);color:var(--fg);font-weight:600}
#st-list .sub{font-weight:400}#st-list .pill{align-self:flex-start;margin-top:2px}
fieldset{border:1px solid var(--line);border-radius:8px;background:var(--card);margin:0 0 14px;padding:10px 14px 14px;min-width:0}legend{font-weight:600;padding:0 6px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px 14px}
.fld{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--muted)}.fld input:not([type=checkbox]),.fld select,.fld textarea{width:100%;color:var(--fg);font-size:14px}.fld small{font-size:11px}
.fld input[type=checkbox]{width:18px;height:18px;margin:6px 0}
.hint{color:var(--muted);font-size:12px;margin:2px 0 8px}code{font:12px ui-monospace,Consolas,monospace}
textarea{font:inherit;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 9px;width:100%;resize:vertical}
textarea.code{font:12.5px/1.45 ui-monospace,Consolas,monospace;white-space:pre;overflow:auto;tab-size:2}
table.mini{width:auto;max-width:100%;margin-bottom:6px;background:transparent}table.mini td,table.mini th{padding:3px 6px;border:none;cursor:default}table.mini tr:hover td{background:none}
.st-head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:10px}.st-head h2{margin:0;font-size:17px}.grow{flex:1}
.st-bar{position:sticky;bottom:0;display:flex;gap:8px;flex-wrap:wrap;padding:10px 0 14px;background:var(--bg);border-top:1px solid var(--line);z-index:1}
button.danger{color:var(--bad);border-color:var(--bad)}
#st-msg{white-space:pre-wrap;margin-bottom:10px;padding:8px 12px;border-radius:8px}#st-msg.ok{background:rgba(74,222,128,.12);color:var(--ok)}#st-msg.err{background:rgba(248,113,113,.12);color:var(--bad)}#st-msg:empty{display:none}
.banner{background:rgba(251,191,36,.12);color:var(--warn);padding:8px 12px;border-radius:8px;margin-bottom:12px}.err{color:var(--bad);white-space:pre-wrap}
.result{display:flex;flex-direction:column;gap:6px;margin-top:10px;padding:10px;border:1px dashed var(--line);border-radius:8px}
label.inline{display:inline-flex;gap:6px;align-items:center;margin-bottom:6px}legend label{display:inline-flex;gap:6px;align-items:center;cursor:pointer}
@media (max-width:760px){#settings{grid-template-columns:1fr}#st-list{position:static;flex-direction:row;flex-wrap:wrap}}
</style></head><body>
<header><h1>__TITLE__</h1>
<span class="stat">Actives retenues <b id="s-kept">–</b></span><span class="stat">Nouvelles aujourd'hui <b id="s-new">–</b></span>
<span class="stat">Favoris <b id="s-star">–</b></span><span class="stat">Total <b id="s-total">–</b></span>
<span class="stat">Dernier scan <b id="s-last">–</b></span><span class="stat" id="s-notify"></span>
<button id="scan" class="primary" title="Lance un scan de toutes les veilles maintenant">Scanner maintenant</button><span id="msg"></span></header>
<nav class="tabs" id="tabs"></nav>
<div class="filters">
<select id="f-status"><option value="active">Actives</option><option value="pending">Achat en cours</option><option value="sold">Vendues</option><option value="gone">Disparues</option><option value="">Tous statuts</option></select>
<select id="f-keep"><option value="1">Retenues par le filtre</option><option value="0">Rejetées / ignorées</option><option value="">Toutes</option></select>
<select id="f-source"><option value="">Toutes sources</option><option value="lbc">leboncoin</option><option value="ebay">eBay</option></select>
<select id="f-review"><option value="">Toute revue</option><option value="none">Non revues</option><option value="starred">Favoris</option><option value="seen">Vues</option><option value="ignored">Ignorées</option></select>
<label><input type="checkbox" id="f-susp" checked> suspectes</label>
<input id="f-q" placeholder="Recherche titre / modèle / vendeur / ville" size="36">
<select id="f-limit" title="Nombre maximal de lignes"><option value="500">500 lignes</option><option value="2000">2000 lignes</option><option value="100000">Tout</option></select>
<button id="refresh">Rafraîchir</button><span class="sub" id="count"></span>
</div>
<main><table><thead><tr>
<th data-k="unit_price">Prix / article</th><th data-k="model">Modèle</th><th>Annonce</th><th data-k="status">Statut</th><th data-k="seller">Vendeur</th><th data-k="location">Lieu</th><th data-k="posted_at">Publiée</th><th data-k="first_seen">Vue le</th><th>Revue</th>
</tr></thead><tbody id="rows"></tbody></table><div id="empty" class="empty" hidden>Aucune annonce pour ces filtres.</div></main>
<section id="settings"><nav id="st-list"></nav><div><div id="st-msg"></div><div id="st-edit"></div></div></section>
<script>
const $=s=>document.querySelector(s);let SETMODE=false;let WATCHES=[],W=null,UNIT=null,UNITL='',ATTRL={},TOTAL=0;let rows=[],sortK='unit_price',sortD=1;
const eur=v=>v==null?'?':new Intl.NumberFormat('fr-FR',{style:'currency',currency:'EUR',maximumFractionDigits:v%1?2:0}).format(v);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const J=s=>{try{return JSON.parse(s||'[]')}catch(e){return []}};
const fmtDate=s=>s?new Date(s).toLocaleString('fr-FR',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'';
async function watches(){WATCHES=await (await fetch('api/watches')).json();const h=decodeURIComponent(location.hash.slice(1));if(!WATCHES.some(w=>w.name===W))W=WATCHES.some(w=>w.name===h)?h:WATCHES[0].name;const cur=WATCHES.find(w=>w.name===W);UNIT=cur.unit_divisor;UNITL=cur.unit_label||'';ATTRL=cur.attr_labels||{};
$('#tabs').innerHTML=WATCHES.map(w=>`<button data-w="${esc(w.name)}" class="${w.name===W&&!SETMODE?'on':''}" title="${esc(w.title)}">${esc(w.title)}<span class="n">${w.kept_active}</span>${w.notify_enabled?'':'<span class="mute">silencieuse</span>'}</button>`).join('')+`<button data-settings class="gear ${SETMODE?'on':''}" title="Réglages des veilles">⚙ Réglages</button>`;document.title=cur.title+' — '+'__TITLE__'}
async function stats(){const s=await (await fetch('api/stats?watch='+encodeURIComponent(W))).json();$('#s-kept').textContent=s.kept_active;$('#s-new').textContent=s.new_today;$('#s-star').textContent=s.starred;$('#s-total').textContent=s.listings;$('#s-last').textContent=s.last_scan?fmtDate(s.last_scan):'jamais';$('#s-notify').textContent=s.notify_enabled?'':'veille silencieuse : pas de notification';$('#scan').disabled=s.scan_running;$('#msg').textContent=s.scan_running?'scan en cours…':'';if(s.scan_running)setTimeout(stats,5000);return s}
async function load(){if(!W)await watches();const p=new URLSearchParams({watch:W,status:$('#f-status').value,keep:$('#f-keep').value,source:$('#f-source').value,review:$('#f-review').value,q:$('#f-q').value,suspicious:$('#f-susp').checked?'1':'0',limit:$('#f-limit').value});const resp=await fetch('api/listings?'+p);rows=await resp.json();TOTAL=parseInt(resp.headers.get('X-Total-Count')||rows.length,10);render();stats()}
function render(){const k=sortK,d=sortD;rows.sort((a,b)=>{const x=a[k]??'',y=b[k]??'';return (x>y?1:x<y?-1:0)*d});const tb=$('#rows');tb.innerHTML=rows.map(r=>{const flags=J(r.flags),reasons=J(r.reasons),susp=J(r.suspicious);const model=(r.family||'')+(r.model?' '+r.model:'')||'Modèle inconnu';const src=r.source==='lbc'?'leboncoin':r.source;
const qty=r.quantity>1?` ×${r.quantity}`:'';const perTb=(UNIT&&r.unit_price!=null)?(r.unit_price/UNIT).toFixed(1).replace('.',',')+' '+UNITL:'';const attrs=(()=>{try{return JSON.parse(r.attrs||'{}')}catch(e){return {}}})();const attrPills=Object.entries(ATTRL).filter(([k])=>attrs[k]!=null&&attrs[k]!==''&&!(Array.isArray(attrs[k])&&!attrs[k].length)).map(([k,l])=>`<span class="pill flag" title="${esc(l)}">${esc(l.replace(/ \(.*\)/,''))} ${esc(Array.isArray(attrs[k])?attrs[k].join('/'):attrs[k])}</span>`).join('');
return `<tr class="${r.review||''}" data-id="${esc(r.listing_id)}" data-src="${esc(r.source)}">
<td class="price">${eur(r.unit_price)}${qty}<small>${perTb}${perTb?'<br>':''}${eur(r.price)} + port ${r.shipping==null?'?':eur(r.shipping)}${r.fees?' + '+eur(r.fees):''}</small></td>
<td><div>${esc(model)}</div><span class="pill ${esc(r.tier)}">${esc(r.tier)}</span>${flags.map(f=>`<span class="pill flag">${esc(f)}</span>`).join('')}${reasons.map(f=>`<span class="pill reject">${esc(f)}</span>`).join('')}${susp.map(f=>`<span class="pill susp" title="${esc(f)}">suspect</span>`).join('')}${attrPills}</td>
<td><a class="title" href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a><div class="sub"><span class="pill ${esc(r.source)}">${esc(src)}</span>${esc(r.condition||'')}${r.delivery===1?' · livraison':r.delivery===0?' · pas de livraison':''}</div>${susp.length?`<details><summary>${susp.length} signal(s) suspect(s)</summary><ul>${susp.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></details>`:''}</td>
<td><span class="pill ${esc(r.status)}">${esc(r.status)}</span>${r.notify_count?`<div class="sub">notifié ×${r.notify_count}</div>`:''}</td>
<td>${esc(r.seller||'?')}<div class="sub">${r.seller_rating!=null?r.seller_rating+' %':''}${r.seller_reviews!=null?' · '+r.seller_reviews+' avis':''}</div></td>
<td>${esc(r.location||'')}</td><td class="sub">${r.posted_at?fmtDate(r.posted_at.replace(' ','T')):'?'}</td><td class="sub">${fmtDate(r.first_seen)}<br>maj ${fmtDate(r.last_seen)}</td>
<td class="actions"><button data-r="starred" title="Favori">★</button><button data-r="seen" title="Vue">✓</button><button data-r="ignored" title="Ignorer : plus de notification ni de digest">✕</button><button data-r="" title="Réinitialiser">↺</button><input class="note" placeholder="note" value="${esc(r.note||'')}"></td></tr>`}).join('');$('#empty').hidden=rows.length>0;$('#count').textContent=(TOTAL>rows.length?rows.length+' affichées sur '+TOTAL+' — augmenter la limite pour tout voir':rows.length+' annonce(s)')}
const reviewUrl=tr=>`api/listings/${encodeURIComponent(tr.dataset.src)}/${encodeURIComponent(tr.dataset.id)}/review?watch=${encodeURIComponent(W)}`;
document.addEventListener('click',async e=>{if(e.target.closest('#tabs button[data-settings]')){if(!SETMODE)openSettings();return}const t=e.target.closest('#tabs button[data-w]');if(t){if(!leaveOk())return;closeSettings();W=t.dataset.w;location.hash=W;await watches();load();return}const b=e.target.closest('button[data-r]');if(!b)return;const tr=b.closest('tr');await fetch(reviewUrl(tr),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({review:b.dataset.r,note:tr.querySelector('.note').value})});load()});
document.addEventListener('change',async e=>{if(!e.target.classList.contains('note'))return;const tr=e.target.closest('tr');const row=rows.find(r=>r.listing_id===tr.dataset.id&&r.source===tr.dataset.src);await fetch(reviewUrl(tr),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({review:row?.review||'',note:e.target.value})})});
document.querySelectorAll('th[data-k]').forEach(th=>th.onclick=()=>{if(sortK===th.dataset.k)sortD=-sortD;else{sortK=th.dataset.k;sortD=1}render()});
['#f-status','#f-keep','#f-source','#f-review','#f-susp','#f-limit'].forEach(s=>$(s).onchange=load);$('#f-q').oninput=()=>{clearTimeout(window._t);window._t=setTimeout(load,300)};$('#refresh').onclick=async()=>{await watches();load()};
$('#scan').onclick=async()=>{$('#scan').disabled=true;const r=await fetch('api/scan',{method:'POST'});$('#msg').textContent=r.status===202?'scan lancé…':r.status===409?'un scan est déjà en cours':'scan indisponible';setTimeout(stats,3000);setTimeout(async()=>{await watches();load()},60000)};
window.addEventListener('hashchange',async()=>{const h=decodeURIComponent(location.hash.slice(1));if(h==='_reglages'){if(!SETMODE)openSettings();return}if(h&&(h!==W||SETMODE)&&WATCHES.some(w=>w.name===h)){if(!leaveOk()){history.replaceState(null,'','#_reglages');return}closeSettings();W=h;await watches();load()}});
window.addEventListener('beforeunload',e=>{if(SETMODE&&DIRTY){e.preventDefault();e.returnValue=''}});
// ------------------------------------------------------------------ settings
let SNAP=null,SEL='_global',DIRTY=false,YAMLMODE=false,PTEXT={},CURTYPE='keywords';
const DEF={schedule:{run_on_start:true,jitter_minutes:10,digest_time:'08:00',digest_send_if_empty:false},notify:{enabled:true,price_drop_pct:5,suspicious:'separate',require_delivery:false,max_age_days:0},market:{history_days:30,min_samples:4},scam:{below_median_ratio:0.55,zero_feedback_below_market:true,max_shipping_eur:40,max_shipping_ratio:0.5}};
const TEMPLATES={keywords:`# Mots-clés en minuscules et sans accents : le titre et la description sont normalisés ainsi.
require_any: []            # au moins un présent, sinon rejet « no_match »
reject: ["pour pieces", "hs", "ne fonctionne pas", "en panne", "for parts"]
reject_title: []           # titre seulement
families: []               # - {name: "RTX 3080", any: ["3080"]}
`,pc:`min_cpu: i5-8400
min_ram_gb: 8
min_storage_gb: 0
require_family: false
families: []               # - {name: Lenovo M720q, any: [m720q], gen: 8}
reject_title: ["portable", "laptop"]
reject_form: ["sff", "tour", "tower"]
mini_any: ["micro", "mini", "tiny"]
`,hdd:`target_capacity_tb: 8
models:
  accept: []               # - {family: IronWolf, brand: Seagate, refs: [ST8000VN004]}
  low_tier: []
  reject: {}
keywords: {}
`};
const PHELP={hdd:'Disques durs : <code>target_capacity_tb</code>, catalogue <code>models</code> (accept / low_tier / reject par motif), <code>keywords</code> (dead, smr, sas, external, bundle, other_device).',
pc:'PC : <code>min_cpu</code> (plancher de performance, ex. i5-8400), <code>min_ram_gb</code>, <code>min_storage_gb</code>, <code>require_family</code>, <code>families</code> (name, any, gen), <code>reject</code>, <code>reject_title</code>, <code>reject_form</code>, <code>mini_any</code>.',
keywords:'Générique : <code>require_any</code>, <code>reject</code>, <code>reject_title</code>, <code>families</code> (name, any), <code>model_regex</code>, <code>lot_regex</code>, <code>unit_divisor</code> + <code>unit_label</code>, <code>attr_labels</code>.'};
const leaveOk=()=>!SETMODE||!DIRTY||confirm('Modifications non enregistrées : les abandonner ?');
async function api(path,body){const r=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const j=await r.json().catch(()=>({error:'réponse illisible (HTTP '+r.status+')'}));if(!r.ok)throw new Error(j.error||('HTTP '+r.status));return j}
function flash(msg,err){const m=$('#st-msg');m.textContent=msg||'';m.className=msg?(err?'err':'ok'):''}
async function openSettings(){SETMODE=true;DIRTY=false;document.body.classList.add('setmode');if(location.hash!=='#_reglages')history.replaceState(null,'','#_reglages');await watches();flash('');
 try{SNAP=await api('api/settings')}catch(e){$('#st-list').innerHTML='';$('#st-edit').innerHTML=`<p class="err">${esc(e.message)}</p>`;return}
 if(!SNAP.editable){$('#st-list').innerHTML='';$('#st-edit').innerHTML=`<div class="empty">${esc(SNAP.reason)}</div>`;return}
 if(SEL!=='_global'&&SEL!=='_new'&&!SNAP.watches.some(w=>w.name===SEL))SEL='_global';renderList();renderEditor()}
function closeSettings(){SETMODE=false;DIRTY=false;document.body.classList.remove('setmode')}
function renderList(){const items=[['_global','Réglages généraux','','']].concat(SNAP.watches.map(w=>[w.name,w.title,w.name,w.enabled?'':'désactivée']));
 $('#st-list').innerHTML=items.map(([k,t,sub,st])=>`<button type="button" data-sel="${esc(k)}" class="${k===SEL?'on':''}">${esc(t)}${sub?`<span class="sub">${esc(sub)}</span>`:''}${st?`<span class="pill gone">${st}</span>`:''}</button>`).join('')+`<button type="button" data-sel="_new" class="${SEL==='_new'?'on':''}">+ Nouvelle veille</button>`}
const fld=(label,html,hint='')=>`<label class="fld"><span>${label}</span>${html}${hint?`<small>${hint}</small>`:''}</label>`;
const inp=(name,val,ph='',type='text',extra='')=>`<input name="${name}" type="${type}" value="${esc(val??'')}" placeholder="${esc(ph??'')}" ${extra}>`;
const num=(name,val,ph)=>inp(name,val,ph,'number','step="any"');
const yn=v=>v==null?'?':v?'oui':'non';
const triSel=(name,val,inh,word='hérité')=>`<select name="${name}"><option value="">${word} (${yn(inh)})</option><option value="true" ${val===true?'selected':''}>oui</option><option value="false" ${val===false?'selected':''}>non</option></select>`;
const SUSP={separate:'à part, priorité basse',never:'jamais notifiées'};const suspSel=(name,val,inh,word='hérité')=>`<select name="${name}"><option value="">${word} (${esc(SUSP[inh]||inh)})</option>${Object.entries(SUSP).map(([v,l])=>`<option value="${v}" ${val===v?'selected':''}>${l}</option>`).join('')}</select>`;
const lbcUrl=(c,s)=>c&&s?`https://www.leboncoin.fr/ck/${encodeURIComponent(c)}/${encodeURIComponent(s)}`:'#';
const thrRow=t=>`<tr data-row="thr"><td><input data-k="max_delivered" type="number" step="any" value="${esc(t.max_delivered??'')}" style="width:110px"></td><td><select data-k="priority">${SNAP.priorities.map(p=>`<option ${p===(t.priority||'default')?'selected':''}>${p}</option>`).join('')}</select></td><td><input data-k="tags" value="${esc((t.tags||[]).join(', '))}" placeholder="fire, …" style="width:130px"></td><td><button type="button" data-del title="Retirer">✕</button></td></tr>`;
const lbcRow=s=>`<tr data-row="lbc"><td><input data-k="category" value="${esc(s.category||'')}" placeholder="ordinateurs"></td><td><input data-k="slug" value="${esc(s.slug||'')}" placeholder="lenovo-m720q"></td><td><a href="${lbcUrl(s.category,s.slug)}" target="_blank" rel="noopener" title="Ouvrir la recherche sur leboncoin">↗</a></td><td><button type="button" data-del title="Retirer">✕</button></td></tr>`;
const thrTable=list=>`<table class="mini"><thead><tr><th>Prix rendu max (€)</th><th>Priorité ntfy</th><th>Tags ntfy</th><th></th></tr></thead><tbody>${list.map(thrRow).join('')}</tbody></table><button type="button" data-add="thr">+ palier</button>`;
function formData(root){const o={};root.querySelectorAll('[name]').forEach(el=>{const path=el.name.split('.');let c=o;path.slice(0,-1).forEach(p=>c=c[p]=c[p]||{});c[path[path.length-1]]=el.type==='checkbox'?el.checked:el.value});return o}
function tableRows(root,kind){return [...root.querySelectorAll(`tr[data-row="${kind}"]`)].map(tr=>{const o={};tr.querySelectorAll('[data-k]').forEach(el=>o[el.dataset.k]=el.value);return o})}
function collectWatch(){const f=$('#st-form'),d=formData(f);return {title:d.title,enabled:d.enabled,notify:d.notify||{},market:d.market||{},scam:d.scam||{},thresholds:tableRows(f,'thr'),
 leboncoin:d.use_lbc?{enabled:d.lbc.enabled,searches:tableRows(f,'lbc')}:null,ebay:d.use_ebay?d.ebay:null,profile_type:d.profile_type,profile_yaml:$('#st-profile').value}}
const payload=()=>YAMLMODE?{yaml:$('#st-yaml').value}:{form:collectWatch()};
const newWatch=()=>({name:'',title:'',enabled:true,notify:{enabled:false},market:{},scam:{},thresholds:SNAP.thresholds.length?[]:[{max_delivered:'',priority:'default',tags:[]}],leboncoin:{enabled:true,searches:[{}]},ebay:null,profile_type:'keywords',profile_yaml:'',yaml:''});
function renderEditor(){const ed=$('#st-edit');if(SEL==='_global'){ed.innerHTML=renderGlobal();return}const isNew=SEL==='_new';const w=isNew?newWatch():SNAP.watches.find(x=>x.name===SEL);
 if(!w){SEL='_global';return renderEditor()}if(isNew)YAMLMODE=false;CURTYPE=w.profile_type;PTEXT={[CURTYPE]:w.profile_yaml||TEMPLATES[CURTYPE]||''};ed.innerHTML=renderWatch(w,isNew)}
function renderWatch(w,isNew){const G=SNAP.global,inh=(sec,k)=>(G[sec]||{})[k]??DEF[sec][k];const n=w.notify||{},m=w.market||{},s=w.scam||{},lbc=w.leboncoin,eb=w.ebay;
 return `<form id="st-form" autocomplete="off"><div class="st-head"><h2>${isNew?'Nouvelle veille':esc(w.title)}</h2>${isNew?'':`<span class="sub">clé <code>${esc(w.name)}</code> · ${w.enabled?'active':'désactivée'}</span>`}<span class="grow"></span>${isNew?'':`<button type="button" id="st-yamlmode">${YAMLMODE?'Revenir au formulaire':'Éditer en YAML'}</button>`}</div>
<div ${YAMLMODE?'hidden':''}>
<fieldset><legend>Veille</legend><div class="grid">
${isNew?fld('Clé',inp('name','','velo','text','required pattern="[a-z0-9][a-z0-9_\\-]{0,31}"'),'minuscules, chiffres, - et _ ; définitive : elle rattache les annonces en base'):''}
${fld("Titre (nom de l'onglet)",inp('title',w.title,'','text','required'))}
${fld('Active',`<input type="checkbox" name="enabled" ${w.enabled?'checked':''}>`,'désactivée : plus scannée, onglet masqué, annonces conservées')}
</div></fieldset>
<fieldset><legend>Notifications</legend><div class="grid">
${fld('Notifier',triSel('notify.enabled',n.enabled,inh('notify','enabled')),'non = veille silencieuse : revue ici, jamais notifiée')}
${fld('Topic ntfy',inp('notify.topic',n.topic,'topic global (NTFY_TOPIC)'))}
${fld('Préfixe des titres',inp('notify.label',n.label,'aucun'))}
${fld('Re-notifier après une baisse de (%)',num('notify.price_drop_pct',n.price_drop_pct,inh('notify','price_drop_pct')))}
${fld('Annonces suspectes',suspSel('notify.suspicious',n.suspicious,inh('notify','suspicious')))}
${fld('Livraison obligatoire',triSel('notify.require_delivery',n.require_delivery,inh('notify','require_delivery')))}
${fld('Âge max des annonces (jours)',num('notify.max_age_days',n.max_age_days,inh('notify','max_age_days')),'0 = pas de limite')}
</div></fieldset>
<fieldset><legend>Paliers de prix</legend><p class="hint">Sur le prix rendu par article (article + port vers la France + frais, divisé par la taille du lot). Au-delà du dernier palier : stocké mais ignoré. ${SNAP.thresholds.length?'Tableau vide = paliers par défaut ('+SNAP.thresholds.map(t=>t.max_delivered+' €').join(', ')+').':'Pas de paliers par défaut : au moins un est requis.'}</p>${thrTable(w.thresholds||[])}</fieldset>
<fieldset><legend>Marché et anti-arnaque</legend><div class="grid">
${fld('Prix de référence (€ rendu par article)',num('market.reference_unit_price',m.reference_unit_price,'aucun'),"médiane de repli tant que l'historique est trop mince")}
${fld('Échantillons min. pour la médiane',num('market.min_samples',m.min_samples,inh('market','min_samples')))}
${fld('Suspecte sous … × la médiane',num('scam.below_median_ratio',s.below_median_ratio,inh('scam','below_median_ratio')))}
${fld('Vendeur sans avis sous le marché = suspect',triSel('scam.zero_feedback_below_market',s.zero_feedback_below_market,inh('scam','zero_feedback_below_market')))}
${fld('Port aberrant au-delà de (€)',num('scam.max_shipping_eur',s.max_shipping_eur,inh('scam','max_shipping_eur')))}
${fld('Port aberrant au-delà de … × le prix',num('scam.max_shipping_ratio',s.max_shipping_ratio,inh('scam','max_shipping_ratio')))}
</div></fieldset>
<fieldset><legend><label><input type="checkbox" name="use_lbc" ${lbc?'checked':''}> leboncoin</label></legend><div class="src" ${lbc?'':'hidden'}>
<label class="inline"><input type="checkbox" name="lbc.enabled" ${!lbc||lbc.enabled?'checked':''}> recherches actives</label>
<p class="hint">Une recherche = la page <code>leboncoin.fr/ck/&lt;catégorie&gt;/&lt;slug&gt;</code> (↗ pour vérifier). Chaque page lue coûte 6 à 11 s par scan, toutes veilles confondues.</p>
<table class="mini"><thead><tr><th>Catégorie</th><th>Slug</th><th></th><th></th></tr></thead><tbody>${((lbc&&lbc.searches)||[]).map(lbcRow).join('')}</tbody></table><button type="button" data-add="lbc">+ recherche</button></div></fieldset>
<fieldset><legend><label><input type="checkbox" name="use_ebay" ${eb?'checked':''}> eBay</label></legend><div class="src" ${eb?'':'hidden'}>
<label class="inline"><input type="checkbox" name="ebay.enabled" ${!eb||eb.enabled?'checked':''}> requêtes actives</label><div class="grid">
${fld('Requêtes (une par ligne)',`<textarea name="ebay.queries" rows="5">${esc(((eb&&eb.queries)||[]).join('\n'))}</textarea>`)}
${fld('Catégories eBay',inp('ebay.category_ids',eb&&eb.category_ids,'toutes'),'identifiants séparés par des virgules')}
${fld('Prix min (€)',num('ebay.price_min',eb&&eb.price_min,'aucun'))}
${fld('Prix max (€)',num('ebay.price_max',eb&&eb.price_max,'aucun'))}
${fld('États (condition_ids)',inp('ebay.condition_ids',((eb&&eb.condition_ids)||[]).join(', '),'liste commune de la source'))}
</div></div></fieldset>
<fieldset><legend>Profil (classification)</legend><div class="grid">${fld('Type',`<select name="profile_type">${SNAP.profile_types.map(t=>`<option ${t===w.profile_type?'selected':''}>${t}</option>`).join('')}</select>`)}</div>
<p class="hint" id="st-phelp">${PHELP[w.profile_type]||''}</p><textarea id="st-profile" class="code" rows="18" spellcheck="false">${esc(PTEXT[CURTYPE])}</textarea></fieldset>
</div>
<div ${YAMLMODE?'':'hidden'}><p class="hint">La veille entière en YAML, commentaires compris. Les clés que le formulaire ne connaît pas (surcharges de transport par veille, etc.) se règlent ici.</p><textarea id="st-yaml" class="code" rows="32" spellcheck="false">${esc(w.yaml||'')}</textarea></div>
<fieldset class="test"><legend>Tester le classifieur</legend><p class="hint">Avec les réglages affichés, même non enregistrés.</p><div class="grid">
${fld("Titre d'annonce",'<input id="t-title" placeholder="Lenovo ThinkCentre M720q i5-8500T 16 Go 256 Go SSD">')}${fld('Prix (€)','<input id="t-price" type="number" step="any">')}${fld('Port (€)','<input id="t-ship" type="number" step="any" placeholder="inconnu">')}</div>
${fld('Description','<textarea id="t-desc" rows="2"></textarea>')}<p><button type="button" id="t-run">Tester</button></p><div id="t-out"></div></fieldset>
<div class="st-bar"><button type="submit" class="primary">${isNew?'Créer la veille':'Enregistrer'}</button><button type="button" id="st-reset">Annuler les modifications</button><span class="grow"></span>
${isNew?'':`<button type="button" id="st-dup">Dupliquer</button><button type="button" id="st-toggle">${w.enabled?'Désactiver':'Activer'}</button><button type="button" id="st-del" class="danger">Supprimer</button>`}</div></form>`}
function renderGlobal(){const G=SNAP.global,sc=G.schedule||{},n=G.notify||{},m=G.market||{},s=G.scam||{},d=(sec,k)=>DEF[sec][k];
 return `<form id="st-global" autocomplete="off">
${SNAP.image_changed?`<div class="banner">Le config.yaml livré avec l'image a changé depuis la création de cette copie : ses nouveautés ne s'appliquent pas d'elles-mêmes. <button type="button" data-restore="">Réinitialiser depuis l'image</button></div>`:''}
<div class="st-head"><h2>Réglages généraux</h2><span class="sub">copie éditable <code>${esc(SNAP.path)}</code></span></div>
<fieldset><legend>Planification</legend><div class="grid">
${fld('Créneaux de scan',inp('schedule.scan_times',(sc.scan_times||[]).join(', '),'08:00, 13:00, 19:00','text','required'),'heure locale, toutes les veilles à la suite')}
${fld('Aléa autour des créneaux (± min)',num('schedule.jitter_minutes',sc.jitter_minutes,d('schedule','jitter_minutes')))}
${fld('Heure du digest',inp('schedule.digest_time',sc.digest_time,d('schedule','digest_time')))}
${fld('Digest même sans annonce',triSel('schedule.digest_send_if_empty',sc.digest_send_if_empty,d('schedule','digest_send_if_empty'),'défaut'))}
${fld('Scan au démarrage',triSel('schedule.run_on_start',sc.run_on_start,d('schedule','run_on_start'),'défaut'))}
${fld("Titre de l'application",inp('web.title',(G.web||{}).title,'Annonces'))}
</div></fieldset>
<fieldset><legend>Notifications : défauts des veilles</legend><div class="grid">
${fld('Notifier',triSel('notify.enabled',n.enabled,d('notify','enabled'),'défaut'))}
${fld('Re-notifier après une baisse de (%)',num('notify.price_drop_pct',n.price_drop_pct,d('notify','price_drop_pct')))}
${fld('Annonces suspectes',suspSel('notify.suspicious',n.suspicious,d('notify','suspicious'),'défaut'))}
${fld('Livraison obligatoire',triSel('notify.require_delivery',n.require_delivery,d('notify','require_delivery'),'défaut'))}
${fld('Âge max des annonces (jours)',num('notify.max_age_days',n.max_age_days,d('notify','max_age_days')),'0 = pas de limite')}
</div></fieldset>
<fieldset><legend>Paliers par défaut</legend><p class="hint">Pour les veilles qui n'ont pas les leurs.</p>${thrTable(SNAP.thresholds)}</fieldset>
<fieldset><legend>Marché et anti-arnaque : défauts</legend><div class="grid">
${fld('Historique de prix (jours)',num('market.history_days',m.history_days,d('market','history_days')),'fenêtre de la médiane glissante')}
${fld('Échantillons min. pour la médiane',num('market.min_samples',m.min_samples,d('market','min_samples')))}
${fld('Suspecte sous … × la médiane',num('scam.below_median_ratio',s.below_median_ratio,d('scam','below_median_ratio')))}
${fld('Vendeur sans avis sous le marché = suspect',triSel('scam.zero_feedback_below_market',s.zero_feedback_below_market,d('scam','zero_feedback_below_market'),'défaut'))}
${fld('Port aberrant au-delà de (€)',num('scam.max_shipping_eur',s.max_shipping_eur,d('scam','max_shipping_eur')))}
${fld('Port aberrant au-delà de … × le prix',num('scam.max_shipping_ratio',s.max_shipping_ratio,d('scam','max_shipping_ratio')))}
</div><p class="hint">Le transport des sources (délais, robots, marketplaces eBay, estimation du port) reste dans le fichier.</p></fieldset>
<fieldset><legend>Historique</legend><p class="hint">Chaque enregistrement garde la version précédente (les 10 dernières).</p><table class="mini">${SNAP.history.map(h=>`<tr><td>${esc(fmtDate(h.when))}</td><td>${esc(h.reason)}</td><td><a href="api/settings/history/${encodeURIComponent(h.file)}" target="_blank" rel="noopener">voir</a></td><td><button type="button" data-restore="${esc(h.file)}">Restaurer</button></td></tr>`).join('')||'<tr><td class="sub">aucune version antérieure</td></tr>'}</table>
<p><button type="button" data-restore="">Réinitialiser depuis le config.yaml de l'image</button></p></fieldset>
<div class="st-bar"><button type="submit" class="primary">Enregistrer</button><button type="button" id="st-reset">Annuler les modifications</button></div></form>`}
function done(r,msg){SNAP=Object.assign({editable:true},r);DIRTY=false;renderList();renderEditor();const rl=r.reload||'';
 flash(rl.startsWith('error')?msg+', mais le service garde l\'ancienne config : '+rl.slice(7):(rl==='unchanged'?'Aucun changement à enregistrer.':msg+(rl==='pending'?' : pris en compte à la fin du scan en cours.':rl==='applied'?' : pris en compte immédiatement.':'.')),rl.startsWith('error'));watches()}
const wurl=(name,act)=>'api/settings/watches/'+encodeURIComponent(name)+(act?'/'+act:'');
async function saveWatch(){const isNew=SEL==='_new';const body=Object.assign({version:SNAP.version},payload());
 try{if(isNew){body.name=$('#st-form [name=name]').value.trim();const r=await api('api/settings/watches',body);SEL=body.name;done(r,'Veille créée')}else done(await api(wurl(SEL),body),'Veille enregistrée')}catch(e){flash(e.message,true)}}
async function saveGlobal(){const f=$('#st-global');const form=formData(f);form.thresholds=tableRows(f,'thr');try{done(await api('api/settings/global',{version:SNAP.version,form}),'Réglages généraux enregistrés')}catch(e){flash(e.message,true)}}
async function runTest(){const out=$('#t-out'),title=$('#t-title').value.trim(),price=$('#t-price').value;if(!title){out.innerHTML='<p class="err">Titre à tester manquant.</p>';return}out.textContent='…';
 const name=SEL==='_new'?($('#st-form [name=name]').value.trim()||'_test'):SEL;
 try{const r=await api('api/settings/classify',Object.assign({name,title,desc:$('#t-desc').value,price,shipping:$('#t-ship').value},payload()));const i=r.info,d=r.decision;
  out.innerHTML=`<div class="result"><div><span class="pill ${i.verdict==='reject'?'reject':'active'}">${i.verdict==='reject'?'rejetée':'acceptée'}</span> <b>${esc(r.label||'modèle inconnu')}</b>${i.quantity>1?' · lot de '+i.quantity:''}</div>
${i.reasons.length?`<div>Motif : ${i.reasons.map(f=>`<span class="pill reject">${esc(f)}</span>`).join('')}</div>`:''}${i.flags.length?`<div>Drapeaux : ${i.flags.map(f=>`<span class="pill flag">${esc(f)}</span>`).join('')}</div>`:''}${r.attrs.length?`<div class="sub">${r.attrs.map(esc).join(' · ')}</div>`:''}
${price?`<div>Palier : <span class="pill ${esc(d.tier)}">${esc(d.tier)}</span> ${d.keep?'retenue':'non retenue'} · ${eur(d.unit_price)} rendu par article${r.per_unit?' · '+esc(r.per_unit):''}${d.keep?' · priorité '+esc(d.priority):''}</div>${d.suspicious.length?`<div>${d.suspicious.map(x=>`<span class="pill susp" title="${esc(x)}">${esc(x)}</span>`).join(' ')}</div>`:''}${d.notes.length?`<div class="sub">${d.notes.map(esc).join(' · ')}</div>`:''}`:'<div class="sub">Donner un prix pour voir le palier et les signaux anti-arnaque.</div>'}</div>`}
 catch(e){out.innerHTML=`<p class="err">${esc(e.message)}</p>`}}
const S=$('#settings');
S.addEventListener('click',async e=>{const t=e.target;const sel=t.closest('[data-sel]');
 if(sel){if(!leaveOk())return;SEL=sel.dataset.sel;YAMLMODE=false;DIRTY=false;flash('');renderList();renderEditor();window.scrollTo(0,0);return}
 const add=t.closest('[data-add]');if(add){add.previousElementSibling.querySelector('tbody').insertAdjacentHTML('beforeend',add.dataset.add==='thr'?thrRow({}):lbcRow({}));DIRTY=true;return}
 if(t.closest('[data-del]')){t.closest('tr').remove();DIRTY=true;return}
 const rs=t.closest('[data-restore]');if(rs){const f=rs.dataset.restore;if(!confirm(f?'Revenir à cette version ? La version actuelle part dans l\'historique.':'Remplacer tous les réglages par le config.yaml de l\'image ? La version actuelle part dans l\'historique.'))return;
  try{done(await api('api/settings/restore',{file:f,version:SNAP.version}),f?'Version restaurée':'Réglages réinitialisés depuis l\'image')}catch(err){flash(err.message,true)}return}
 if(t.id==='st-reset'){if(!DIRTY||confirm('Abandonner les modifications ?')){DIRTY=false;flash('');renderEditor()}return}
 if(t.id==='st-yamlmode'){if(DIRTY&&!confirm('Changer de mode abandonne les modifications non enregistrées. Continuer ?'))return;YAMLMODE=!YAMLMODE;DIRTY=false;renderEditor();return}
 if(t.id==='t-run'){runTest();return}
 if(['st-dup','st-toggle','st-del'].includes(t.id)&&DIRTY&&!confirm('Des modifications non enregistrées seront perdues. Continuer ?'))return;
 try{if(t.id==='st-dup'){const nn=prompt('Clé de la copie (minuscules, chiffres, - et _) :',SEL+'-2');if(!nn)return;const r=await api(wurl(SEL,'duplicate'),{new_name:nn.trim(),version:SNAP.version});SEL=nn.trim();done(r,'Copie créée (désactivée, à relire avant de l\'activer)')}
  else if(t.id==='st-toggle'){const w=SNAP.watches.find(x=>x.name===SEL);done(await api(wurl(SEL,'enabled'),{enabled:!w.enabled,version:SNAP.version}),w.enabled?'Veille désactivée':'Veille activée')}
  else if(t.id==='st-del'){if(!confirm(`Supprimer la veille « ${SEL} » de la config ? Ses annonces restent en base et reviennent si une veille du même nom est recréée.`))return;const r=await api(wurl(SEL,'delete'),{version:SNAP.version});SEL='_global';done(r,'Veille supprimée')}}
 catch(err){flash(err.message,true)}});
S.addEventListener('input',e=>{const t=e.target;if(t.closest('.test'))return;if(t.closest('form'))DIRTY=true;const tr=t.closest('tr[data-row="lbc"]');if(tr)tr.querySelector('a').href=lbcUrl(tr.querySelector('[data-k=category]').value.trim(),tr.querySelector('[data-k=slug]').value.trim())});
S.addEventListener('change',e=>{const t=e.target;if(t.closest('.test'))return;if(t.closest('form'))DIRTY=true;
 if(t.name==='use_lbc'||t.name==='use_ebay')t.closest('fieldset').querySelector('.src').hidden=!t.checked;
 if(t.name==='profile_type'){const ta=$('#st-profile');PTEXT[CURTYPE]=ta.value;CURTYPE=t.value;ta.value=PTEXT[CURTYPE]??TEMPLATES[CURTYPE]??'';$('#st-phelp').innerHTML=PHELP[CURTYPE]||''}});
S.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.tagName==='INPUT'&&e.target.closest('.test')){e.preventDefault();runTest()}});
S.addEventListener('submit',e=>{e.preventDefault();if(e.target.id==='st-global')saveGlobal();else saveWatch()});
if(location.hash==='#_reglages')watches().then(openSettings);else load();
</script></body></html>"""
