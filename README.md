# ListingWatcher

Listing watch (hard drives, mini PCs, or any object described by a profile) on **leboncoin** and **eBay**
(FR + DE) with **ntfy** notifications: new kept listing, price drop ≥ 5 %, daily digest, review web UI.
Docker container, SQLite database, secrets via environment variables.

Several **watches** live in the same process without mixing their flows: each has its own profile,
price tiers, searches, tab in the UI, digest and notification toggle. The shipped watches
(`config.yaml > watches`):

- `hdd`: 8 TB hard drives, 3.5", SATA, CMR: reference catalogue, SMR/SAS/4Kn rules, price per TB.
  This is the historical watch, the one that notifies.
- `minipc`: high-end 1-litre mini PCs (Lenovo M90q / M70q / M80q / M75q Gen 2, OptiPlex 7080 / 7090 /
  7000 Micro, HP EliteDesk 800 G6 / 805 G6 / Elite Mini 800 G9…) with a **performance floor** rather
  than an exact CPU (i5-10500T, 16 GB, 256 GB), 600 € delivered cap. **Silent** watch: stored and reviewed
  in the UI, never notified (`notify.enabled: false`).

The core is **generic**: sources, delivered price, tiers, anti-scam, persistence, notifications and
UI know nothing about the watched object. What is specific lives in a **profile**
(`listingwatcher/profiles/`), chosen by `watches.<watch>.profile.type`:

- `hdd`: hard drives: catalogue, target capacity, €/TB.
- `pc`: PCs: CPU reduced to a score (generation + family: i5-8400 = 10, like an i7-7700 or an i3-10100),
  RAM and storage read in GB, chassis by keywords, rejection of SFF/tower form factors and laptops, barebones
  flagged. The label becomes "Lenovo M720q i5-8500T", the market median is computed per CPU then per chassis.
- `keywords`: generic, no code: required / forbidden keywords, families by keywords, reference
  by regular expression, lots. Enough for a graphics card, a bike, a camera…

For an object that needs more intelligence, write a class inheriting from `Profile`
(`classify()`, optionally `unit_divisor`/`unit_label`, `attr_labels`, `low_flags`, `flag_notes`) and
add it to the `REGISTRY`.

## How it works

At each scan slot, the watches are scanned **one after the other**, source by source. The transport is
shared: a single polite leboncoin client for the whole process (never two requests in parallel), a single
eBay client and token, and a page already read during the scan is not read again by another watch.

1. **Fetch**: `listingwatcher/fetchers/leboncoin.py` reads the result pages `/ck/<category>/<slug>[/p-N]`
   (server-side rendered `__NEXT_DATA__` JSON) with a browser-TLS-fingerprint client, one request every
   6-11 s, robots.txt, backoff on 403/429. `listingwatcher/fetchers/ebay.py` goes through the official Browse API.
2. **Normalization (profile)**: the watch's profile classifies the listing: for `hdd`, `listingwatcher/normalize.py`
   extracts the reference (ST8000VN004, WD80EFZZ, HUH728080ALE600, MG08ADA800E…) from title + description, classifies
   it through the catalogue then structural rules, rejects dead drives, SMR, SAS, 4Kn, external, 2.5", complete
   NAS units, capacities ≠ 8 TB; for `pc`, `listingwatcher/profiles/pc.py` extracts CPU, RAM, storage and chassis.
3. **Filtering**: `listingwatcher/filtering.py` computes the **delivered price per item** (item + shipping to France
   + buyer fee, divided by the lot size), converts it to the profile's unit (€/TB for `hdd`), ignores
   listings older than `notify.max_age_days`, puts it in a tier (urgent / default / low / ignored)
   and sets the anti-scam flags (price < 55 % of the watch's **own** 30-day median, seller without feedback
   below market, abnormal shipping, neither delivery nor secure payment).
4. **Store**: `listingwatcher/store.py`, SQLite keyed by `(watch, source, listing_id)`: first/last_seen, status
   (active / pending / sold / gone, the worst one wins), price history, notified price. A database
   predating watches is migrated at startup, its rows attached to the `hdd` watch.
5. **Notification**: `listingwatcher/notify.py`, one notifier per watch: one alert per new kept listing or
   drop ≥ 5 %, never a duplicate; digest at 8 am of the listings still active. A silent watch
   sends nothing. Technical alerts (blocked source) always go out, on the global topic.
6. **Web UI**: `listingwatcher/web.py`, port 8080 in the container (8004 on the host): one tab per watch,
   listing review (filters status / kept / source / review, search, sort), starred ★, seen ✓,
   ignored ✕ (no more notification nor digest), notes, a "Scanner maintenant" (scan now) button (all watches),
   and a **"⚙ Réglages" (Settings)** tab that edits the config (see below).
   JSON API under `/api/`: `watches`, `stats?watch=`, `listings?watch=`, `listings/<source>/<id>/history?watch=`,
   `listings/<source>/<id>/review?watch=`, `scan`, `health`, `settings` (+ `settings/…` via POST). Without `watch`,
   the first watch.

Kept listings are **enriched in a second step** (leboncoin `/ad/` detail page for the description and the
SMART hours, eBay `getItem` for quantity, returns, MPN), never the others.

## Installation

```bash
git clone <this repo> && cd ListingWatcher
cp .env.example .env        # fill in NTFY_* and EBAY_*
docker compose build
docker compose run --rm listingwatcher test-notify      # checks ntfy
docker compose run --rm listingwatcher probe --watch minipc --source leboncoin   # fetch + classification, nothing is stored
docker compose up -d        # continuous service
docker compose logs -f
```

The database lives in `./data/listingwatcher.sqlite` (volume `/data`), the editable config in `./data/config.yaml`
(see "Settings from the UI").

### eBay credentials

On <https://developer.ebay.com>: *My Account → Application Keys → Production* → create a keyset.
Copy **App ID (Client ID)** and **Cert ID (Client Secret)** into `.env`. The Browse API in
*client credentials* mode is enough (scope `https://api.ebay.com/oauth/api_scope`), no user consent.
Without these variables, the eBay source is cleanly disabled at startup.

### On a remote Docker host

Build the image on the workstation (`docker build -t listingwatcher:latest .`), transfer it
(`docker save listingwatcher:latest | gzip | ssh <host> 'gunzip | docker load'`) and run it with the
repo's `docker-compose.yml` or the host's tool (Portainer, Container Manager…). Secrets stay
environment variables of the service, never in the image; the `/data` volume holds the database and the
editable config. UI: `http://<host>:8004/`, no authentication, keep it on the local network.

### On a Synology (DSM)

Two options:

- **Container Manager**: import the project (`docker-compose.yml`), the `run` service runs continuously.
- **Task Scheduler**: run `docker compose run --rm listingwatcher scan` 2 to 4 times a day and
  `… digest` at 8 am. The `scan` mode does one pass then exits, and the database keeps the memory of the listings.

## Commands

| Command | Role |
|---|---|
| `run` | service: web UI + scans of all watches at the `schedule.scan_times` slots + digests at `digest_time` |
| `serve` | web UI only (when scans are launched by a cron / the DSM scheduler) |
| `scan [--watch X] [--source leboncoin\|ebay]` | one full pass, then exit |
| `digest [--watch X] [--force]` | sends the digest of active listings (notifying watches) |
| `probe [--watch X] [--source X] [--all]` | fetch + classification, printed on screen, nothing stored |
| `classify "<title>" [--watch X] [--desc …] [--price N]` | tests a watch's classifier on a title |
| `test-notify` | sends a test notification on the global topic |
| `stats` | database state, total and per watch |

Global options: `--dry-run` (nothing is sent to ntfy), `--config` (a file taken as is, without an editable
copy), `--db`, `--log-level`.
`--watch` takes a key of `watches`; without it, all watches (or the first one for `classify`).

## Configuration (`config.yaml`)

- Root: `timezone`, `schedule`, `web`, and the **default values** of `notify`, `market`, `scam`,
  `thresholds` that each watch can override.
- `sources.<source>`: the shared **transport**: `min_delay_s`/`max_delay_s`, `respect_robots`,
  `cooldown_hours_when_blocked`, `detail_pages`, `shipping_estimate` (leboncoin does not expose the "dès X €"
  shipping, it is estimated from the declared weight); `marketplaces`, `condition_ids`, `delivery_zip`, `limit` (eBay).
- `watches.<watch>`: `title`, `enabled`, `notify` (`enabled`, `price_drop_pct`, `suspicious`
  (`separate` = separate low-priority alert, `never`), `require_delivery`, `max_age_days`, per-watch ntfy
  `topic`, `label` prefix for titles), `thresholds` (tiers on the delivered price per item → ntfy priority and
  tags), `market.reference_unit_price`, `scam`, `profile` (`type` + section of the same name) and `sources`:
  `leboncoin.searches` (category/slug pairs), `ebay.queries`, `category_ids`, `price_min`/`price_max`,
  `condition_ids`. A watch that does not declare a source does not scan it.
- `profile.hdd.models`: accepted / surveillance / rejected catalogue; `profile.hdd.keywords`: keywords
  (dead, SMR, SAS, external…). `profile.pc`: `min_cpu`, `min_ram_gb`, `min_storage_gb`, `families`,
  `require_family`, `reject`, `reject_title`, `reject_form`, `mini_any` (details in `listingwatcher/profiles/pc.py`).

A `config.yaml` in the legacy format (profile and searches at the root, no `watches`) is still accepted: it is
wrapped in a single `hdd` watch. `${VAR}` / `${VAR:-default}` variables are substituted from the
environment.

## Settings from the UI

The image's `config.yaml` is only a **seed**. On the first start of the service (`run` or `serve`), it is
copied into the data volume (`/data/config.yaml`, or `$LISTINGWATCHER_LIVE_CONFIG`, by default next to the database),
and **this copy is the live (authoritative) one** from then on, for every command. `--config <file>` bypasses the
copy (read-only, Settings tab disabled).

The **"⚙ Réglages" (Settings)** tab edits this copy:

- **General settings**: scan slots, jitter, digest, title, default values of `notify`, `market`,
  `scam` and tiers. The sources' transport (delays, robots, marketplaces, shipping estimate) stays in
  the file.
- **One form per watch**, including disabled ones: title, enabled, notification (empty = inherited from the root,
  value shown), tiers, reference price, anti-scam, leboncoin searches (with a ↗ link to the page),
  eBay queries, and the **profile** in a YAML editor (catalogue, families, keywords). A button switches the whole
  watch to YAML for the keys the form does not know.
- **Create, duplicate** (the copy is born disabled), **enable / disable**, **delete** a watch. A watch's key
  is permanent: it attaches the listings in the database, which survive deletion.
- **Test the classifier**: a title, a description, a price → verdict, flags, tier and anti-scam
  signals, computed with the settings shown, even unsaved ones.

Every save is **validated** (profile loaded, regular expressions, tiers, searches, scan slots,
disabled watches included) before being written, then the service **rebuilds itself hot**: right away, or at
the end of the running scan. Editing goes through ruamel.yaml: comments, key order and `${VAR}` are preserved
(only long flow lists folded over several lines are put back on one line). The 10 previous
versions are kept in `config-history/` next to the copy, viewable and restorable from the tab.

When the image's `config.yaml` changes (new deployment), its changes **do not apply** by themselves:
a banner reports it in the general settings, with a "Réinitialiser depuis l'image" (reset from the image) button
(the current version goes into the history). The UI has no authentication: it stays on the LAN.

## Degraded mode

If leboncoin returns a persistent block (403/429), the source is paused for all watches
(`cooldown_hours_when_blocked`) and a single notification per day reports it. Fallback: leboncoin's native
"Sauvegarder la recherche" (save search) feature sends alerts by email.

Note: the header of leboncoin's `robots.txt` states that automated access is forbidden, but its rules do not
block `/ck/` nor `/ad/accessoires_informatique/`. The client deliberately stays slow and sequential.

## Adding a watch

From the UI: "⚙ Réglages" → "+ Nouvelle veille" (new watch) (or "Dupliquer" (duplicate) a similar watch), test a few
real titles with the classifier, save. Nothing to redeploy.

By hand:

1. An entry under `watches`: `title`, `notify.enabled`, `thresholds`, `market.reference_unit_price`,
   `sources` (searches), `profile` (an existing `type`, or a new profile, see below).
2. `python -m listingwatcher probe --watch <name> --source leboncoin --all` to validate slugs and classification.
3. Edit `data/config.yaml` (the live copy) and restart, or change the repo's `config.yaml`,
   redeploy and "Réinitialiser depuis l'image" (reset from the image).

## Adding a source

1. Create `listingwatcher/fetchers/<source>.py` with a class inheriting from `BaseFetcher` (short `name`,
   `fetch() -> FetchResult`, optionally `enrich(listing)`, `supports_enrich = True` and
   `make_transport(scfg)` for the client shared between watches).
   Produce `Listing` objects (`listingwatcher/models.py`): price, shipping to France (None if unknown), fees,
   condition, seller, status.
2. Register it in `REGISTRY` (`listingwatcher/fetchers/__init__.py`), key = `sources.<source>` section of
   `config.yaml`; the per-watch search keys are listed in `config.SEARCH_KEYS`.
3. Add the config section and a test with a fixture.

## Tests

```bash
python -m pytest -q
```

The drive classifier is covered by the real titles from the brief and by those found while scanning leboncoin
(fixtures `tests/fixtures/lbc_page*.json`, captured on 9 September 2026). `tests/test_multiwatch.py` covers
watch isolation, database migration, shared transport, the silent watch, the tabs and the
`pc` profile.
