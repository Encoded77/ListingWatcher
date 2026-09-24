---
name: listingwatcher-watches
description: Add, edit, duplicate, disable or remove a watch ("veille") in ListingWatcher, the leboncoin + eBay listing monitor at C:\Code\ListingWatcher. Use whenever the request is to watch a new kind of item (a GPU, a bike, a mini PC, a disk model…), change what an existing watch searches for (leboncoin slugs, eBay queries), adjust its price tiers, notification, anti-scam or profile rules (keywords, catalogue, CPU floor), or test how a listing title would be classified. Covers config.yaml `watches`, the three profiles (hdd, pc, keywords), the CLI checks (probe, classify, pytest) and the live settings API of a running instance.
---

# ListingWatcher watches

A **watch** (`veille`) is one monitored item type: its profile (how a listing title is classified),
its price tiers, its searches per source, its notification toggle. All watches share the transport
(delays, robots, eBay credentials) declared under `sources`. Listings themselves are never edited by
hand: they are fetched, classified, stored in SQLite under the key `(watch, source, listing_id)` and
reviewed in the web UI. Editing a watch means editing its entry under `watches:` in `config.yaml`.

Code, comments, YAML comments, docs and log messages are in **English**. Only what the end user sees stays
**French**: web UI labels, ntfy notification wording, CLI help, settings error messages, watch titles, and
of course the keyword lists that match French listings. Follow that split in every edit.

## Which config file

| File | Role |
|---|---|
| `config.yaml` (repo root) | The **seed** baked into the Docker image. Edit it for anything that should ship. |
| `data/config.yaml` (volume `/data`, `$LISTINGWATCHER_LIVE_CONFIG`) | The **live copy**, created from the seed on first `run`/`serve`. It is the one every command reads afterwards, and the one the ⚙ Réglages tab and `/api/settings` edit. |

Changing the seed does **not** change a running deployment: the UI shows a banner with
« Réinitialiser depuis l'image » (current live copy goes to `config-history/`). For a one-off change on a
running instance, use the settings API (below) or edit the live copy and restart. `--config <file>`
bypasses the copy (read-only, settings tab disabled). Never put secrets in either file: they come from
`NTFY_*` / `EBAY_*` env vars, `${VAR}` / `${VAR:-default}` are substituted at load.

## Anatomy of a watch

```yaml
watches:
  <name>:                          # ^[a-z0-9][a-z0-9_-]{0,31}$ ; FINAL: it keys the listings in the DB
    title: Cartes graphiques       # user-facing, French, shown as the UI tab
    enabled: true                  # false = not scanned, still validated, still in the DB
    notify:
      enabled: true                # false = silent watch: stored + reviewable, never notified, no digest
      # topic: annonces-gpu        # own ntfy topic (default NTFY_TOPIC) ; label: title prefix
      # price_drop_pct, suspicious (separate|never), require_delivery, max_age_days override the root
    thresholds:                    # on the DELIVERED price per item (price + shipping to FR + buyer fee) / lot size
      - {max_delivered: 300, priority: urgent, tags: [fire]}
      - {max_delivered: 400, priority: default, tags: []}
      - {max_delivered: 500, priority: low, tags: []}   # above the last tier: stored but ignored
    market:
      reference_unit_price: 380    # anti-scam fallback median until the watch has history (min_samples)
    scam:                          # optional overrides: below_median_ratio, max_shipping_eur, max_shipping_ratio…
      max_shipping_eur: 30
    sources:                       # a source not listed here is not scanned for this watch
      leboncoin:
        searches:                  # /ck/<category>/<slug> ; category = [a-z0-9_]+, slug = [a-z0-9-]+
          - {category: ordinateurs, slug: rtx-3080}
          - {category: accessoires_informatique, slug: rtx-3080}
      ebay:
        category_ids: "27386"      # comma-separated numeric ids ; omit for all
        queries: ["RTX 3080", "GeForce 3080"]
        price_min: 100
        price_max: 600
        # condition_ids: [1000, 3000]   # overrides sources.ebay.condition_ids
    profile:
      type: keywords               # hdd | pc | keywords ; the section below carries the same name
      keywords:
        ...
```

Root-level `notify`, `market`, `scam` and `thresholds` are defaults every watch inherits; a watch only
needs the keys it changes. Priorities are ntfy priorities: `urgent`, `default`, `low` (tiers are sorted
by `max_delivered`). Flags `model_unknown`, `low_tier`, `ref_unverified` (plus the profile's
`low_flags`) demote an accepted listing to `low`.

Reference examples in the repo `config.yaml`: `hdd` (catalogue profile, notifying) and `minipc`
(pc profile, silent).

## Profiles

Pick the least code that classifies correctly. Titles and descriptions are matched after `norm()`:
lowercase, accents stripped, whitespace collapsed, quotes unified. **Write keywords without accents and
in lowercase** (`"defectueux"`, not `"défectueux"`). Keywords match at word boundaries, so `"hs"` does
not match `"hsbc"`, but multi-word phrases are literal (`"pour pieces"`).

### `keywords` (no code, fits most items)

```yaml
profile:
  type: keywords
  keywords:
    require_any: ["rtx 3080", "3080"]           # at least one in title+description, else reject `no_match`
    reject: ["pour pieces", "hs", "ne fonctionne pas", "defectueux"]   # title + description
    reject_title: ["pc complet", "boitier", "tour"]                    # title only
    families:                                    # first match wins → family (UI grouping, market median)
      - {name: "RTX 3080 Ti", any: ["3080 ti", "3080ti"]}
      - {name: "RTX 3080", any: ["3080"]}
    model_regex: "\\b(?:TUF|ROG|GAMING X|VENTUS)[ \\w-]{0,12}\\b"      # optional, sets `model`
    lot_regex: "\\blot de (\\d{1,2})\\b"        # optional, default shown ; group 1 = quantity (2..50)
    unit_divisor: null                           # e.g. 8 with unit_label "€/To" for a per-unit metric
    unit_label: ""
    attr_labels: {}
```

Put the most specific family first (`3080 Ti` before `3080`). No family and no model → flag
`model_unknown` → priority `low`.

### `pc` (performance floor, not an exact CPU)

Keys: `min_cpu` (e.g. `i5-10500T`, score = generation + tier bonus i3 0 / i5 +2 / i7 +3 / i9 +4),
`min_ram_gb`, `min_storage_gb`, `families` (`{name, any: [...], gen: N}`, `gen` fills in the generation
when the title only says « i5 »; precise generations before the bare name), `require_family`, `reject`,
`reject_title`, `reject_form` + `mini_any` (form factors refused unless a mini keyword remains). Details
in the module docstring of `listingwatcher/profiles/pc.py`. RAM/storage missing → flagged, not rejected.

### `hdd` (catalogue)

Keys: `target_capacity_tb`, `models.accept` / `models.low_tier` (`{family, brand, refs: [...]}`),
`models.reject.<reason>: [refs]` (reasons `smr`, `sas`, `4kn`), `keywords.{dead,smr,sas,external,bundle,other_device}`.
Structural rules (Seagate/WD/Toshiba/HGST reference patterns) cover refs absent from the catalogue.
Used by the `hdd` watch; to watch another capacity, duplicate the watch and change
`target_capacity_tb` + the refs.

### New profile (only when keywords are not enough)

Subclass `Profile` in `listingwatcher/profiles/<name>.py`: implement `classify(title, description,
condition_code) -> ModelInfo` (verdict `accept`/`reject`, `family`, `model`, `reasons`, `flags`,
`attrs`, `quantity`), optionally `unit_divisor`/`unit_label`, `attr_labels`, `low_flags`, `flag_notes`,
`market_key()`. Register it in `REGISTRY` (`listingwatcher/profiles/__init__.py`) and add tests in
`tests/test_profiles.py`.

## Finding searches

- **leboncoin**: open a search on the site, the URL is `https://www.leboncoin.fr/ck/<category>/<slug>`.
  Common categories: `ordinateurs`, `accessoires_informatique`, `image_son`, `consoles`, `jeux_video`,
  `telephones_objets_connectes`, `velos`, `sport_plein_air`… Several slugs per category are normal
  (spelling variants, brand names): the fetcher de-duplicates ads seen through several slugs. Each page
  costs one polite request (6-11 s), so keep the list to what brings distinct results.
- **eBay**: `category_ids` from the category page URL / Browse API; queries are plain keyword searches
  across `sources.ebay.marketplaces` (FR + DE by default). Without `EBAY_CLIENT_ID` the source is off.

## Workflow

1. Edit `config.yaml` under `watches:` (add, or change an existing entry). Comments in English, titles in
   French; the live editor uses ruamel and preserves comments and `${VAR}`.
2. Validate the classifier on real titles, nothing is stored or sent:

   ```bash
   .venv/Scripts/python -m listingwatcher classify "Lot de 2 RTX 3080 TUF, une HS" --watch gpu --price 350
   .venv/Scripts/python -m listingwatcher probe --watch gpu --source leboncoin --all
   ```

   `probe` fetches the real pages (rate-limited, ~10 s per page) and prints verdict, flags, tier and
   anti-scam signals per listing; `--all` includes rejects. Iterate on keywords until the rejects are
   right; a `no_match` flood means `require_any` is too narrow, an accepted junk listing needs a
   `reject`/`reject_title` word.
3. Run the tests: `.venv/Scripts/python -m pytest -q`. Config loading is exercised by
   `tests/test_config.py`; when a change to `hdd`/`minipc` rules is intended to fix a real title, add
   that title to `tests/test_normalize.py` or `tests/test_profiles.py`.
4. Ship: rebuild the image (`docker compose build`, or build + `docker save`/`docker load` for a remote host,
   see README « Sur un hôte Docker distant »), recreate the container, then « Réinitialiser depuis l'image »
   in ⚙ Réglages, or apply the same change through the settings API so the live copy matches the seed.

Never `git commit`: leave the change ready and say so.

## Live instance: settings API (`http://<host>:8004/api/settings`)

All POSTs take a JSON body; pass `version` (from `GET /api/settings` → `version`) to avoid clobbering a
concurrent edit (409 on mismatch). Every write is validated (profile loads, regexes compile, at least one
tier and one source, disabled watches included) and the service rebuilds hot, or after the running scan.

| Call | Body |
|---|---|
| `GET /api/settings` | snapshot: `version`, `global`, `watches[]`, `history`, `image_changed` |
| `POST /api/settings/watches` | create: `{name, version, yaml: "<watch YAML>"}` or `{name, version, form: {...}}` |
| `POST /api/settings/watches/<name>` | save: `{version, yaml}` or `{version, form}` |
| `POST /api/settings/watches/<name>/duplicate` | `{version, new_name}` (copy is created **disabled**) |
| `POST /api/settings/watches/<name>/enabled` | `{version, enabled: true|false}` |
| `POST /api/settings/watches/<name>/delete` | `{version}` (listings in the DB survive) |
| `POST /api/settings/classify` | `{name, title, desc, price, shipping, yaml|form}` → verdict with the draft, unsaved |
| `POST /api/settings/global` | `{version, form: {schedule, notify, market, scam, web, thresholds}}` |
| `POST /api/settings/restore` | `{version, file}` (history file) or `{version}` (seed from the image) |

`yaml` is the whole watch mapping as text (the same block as under `watches.<name>:`, without the name).
`form` mirrors the UI: `title`, `enabled`, `notify`/`market`/`scam` sections (empty value = inherit),
`thresholds` rows, `leboncoin: {searches: [{category, slug}], enabled}`, `ebay: {queries, category_ids,
price_min, price_max, condition_ids, enabled}`, `profile_type`, `profile_yaml` (the profile section as
text). Prefer `yaml` for anything the form does not know.

Then `POST /api/scan` (202, 409 if a scan runs) to fetch immediately, `GET /api/listings?watch=<name>`
to check what came in.

## Pitfalls

- A watch **name is permanent**: renaming means a new watch and orphaned listings. Choose short
  lowercase names (`gpu`, `velo-route`).
- `thresholds` compare the delivered price **per item** (lot size divides), in € unless the profile sets
  `unit_divisor`; `reference_unit_price` is in the same unit.
- `require_any` empty = accept everything the searches return; the slugs then do all the filtering.
- `condition_code == "parts"` (leboncoin « pour pièces », eBay 7000) is rejected by every profile
  before keywords run.
- `notify.enabled: false` also silences the digest; scan-blocked alerts still go to the global topic.
- Old single-watch config (profile at the root, no `watches`) is still accepted and wrapped into a watch
  named `hdd`; do not write new configs that way.
- Fixtures under `tests/fixtures/` are real leboncoin/eBay payloads with seller and location fields
  replaced by synthetic values; when capturing new ones, scrub `owner`, `location`, `seller`,
  `itemLocation` the same way.
