# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

CardLedger server: a Django 6 + Django REST Framework app (Python 3.14, managed with `uv`) that holds a trading card
collection (Magic, Pokémon, Lorcana, One Piece, Yu-Gi-Oh! and Riftbound) and manages sales on Cardmarket and CardTrader (synced only by a browser extension: no market token on the server). It exposes
only a JSON API under `/api/`; the interface is a
separate React SPA ([cardledger_ui](https://github.com/gab-25/cardledger_ui)), included as the git submodule `ui/`
(pinned to a release tag; the Dockerfile builds it in a Node stage), whose built `index.html` Django serves for every
other URL (`config/views.py`, catch-all route last in `config/urls.py`, files from `UI_APP_DIR`, default `ui/dist`).

## Commands

```bash
docker compose up -d db                          # PostgreSQL (required, also for tests)
uv sync
uv run python manage.py migrate
git submodule update --init && (cd ui && npm ci && npm run build)   # the UI build Django serves
uv run python manage.py runserver

uv run python manage.py test                                      # all tests
uv run python manage.py test ledger.test_scan_api                 # one module
uv run python manage.py test ledger.test_scan_api.SchemaTest.test_docs_are_public   # one test

uv run python manage.py check                                     # CI also runs these two
uv run python manage.py makemigrations --check --dry-run

uv run python manage.py spectacular --validate --fail-on-warn --file /dev/null   # OpenAPI schema check

docker compose up -d redis && uv run celery -A config worker --loglevel=info     # catalog sync worker
uv run python manage.py sync_catalog             # run the nightly catalog + price sync in-process (right after
                                                 # the first migrate: the catalog starts empty)
```

There is no linter or formatter configured. Settings come only from the environment / `.env` (see `.env.example`);
`OPENROUTER_API_KEY` and `OPENROUTER_MODEL` deliberately have no defaults in code.

## Architecture

- **Apps**: `config/` (settings, root URLs, the UI view), `accounts/` (auth and profile API in `accounts/api.py`;
  `accounts.Profile`, one-to-one with the user and created on demand by `profile_of`, holds the business details
  `shop_name`/`vat_number`, shown in `me`'s `user` and saved by `PUT /api/account/profile/`),
  `ledger/` (the domain). Views in `ledger/api/*.py` and `accounts/api.py` are function-based DRF `@api_view`s, one
  module per UI page; business logic lives in `ledger/services.py` (CSV, market connections, orders, shared stock,
  conflicts), `ledger/extension.py` and `ledger/scan.py`, not in the views.
- **Multi-user ownership**: every personal row (collection items, listings, orders, scan sessions, conflicts, logs)
  has an owner, reached through `owned_manager(path)` / `OwnedQuerySet.owned_by(user)` in `ledger/models.py`. Views
  must always start from `Model.objects.owned_by(request.user)` so another user's object answers 404
  (`ledger/test_ownership.py`). `SyncLog.objects.visible_to(user)` adds the global entries. The card catalog
  (`Expansion`, `Blueprint`) and prices are shared.
- **No market tokens**: the server stores no Cardmarket or CardTrader credential and never calls either market (no
  CardTrader client; `SyncService` and its tokens were dropped by migration 0039). Both markets are synced only by the
  browser extension (see Markets).
- **Cards vs printings**: `Card` is the Ledger's own row (name + set + collector number) that `CollectionItem`
  points to, tied to its printing by `Card.cm_product_id`; `Blueprint` is a product of the local catalog, keyed by
  its Cardmarket product id (`cm_product`, also the row of its Cardmarket price). **Games**: `Game` (mtg, pokemon,
  lorcana, onepiece, yugioh, riftbound); `Expansion`, `Category` and `Card` belong to a game, uniqueness is per game,
  and every catalog lookup (recognition, reconcile, resolve_blueprints, API) stays inside one game. Collection, import,
  export, listings, catalog and scan setup take `?game=<slug>` through `game_param(params)` / `GAME` in
  `ledger/api/common.py` (default `mtg`, unknown slug 400); `GET /api/scan/sessions/` and `me`'s
  `pending_changes` without `game` (absent or blank) cover every game; item and listing detail (`collection/<pk>/`, `listings/<pk>/`) return their `game`;
  `GET /api/games/` lists the games. Orders, shipments, sync and the order/shipment counters stay global. The catalog is rebuilt
  only by the nightly `ledger/catalog_sync.py`: it imports the files of the separate generator repo gab-25/cardledger_database from a private
  S3 bucket (`ledger/catalog_files.py`: one `catalog/v1/<slug>.json` per game, a JSON object with `expansions` and `products`, no manifest; the generator is run
  by hand, `cardledger-db generate` then `upload`; settings `CARDLEDGER_CATALOG_*`: bucket, endpoint, region, access
  key and secret; `AWS_*` is never read): each game's file is downloaded every night and imported only when the
  sha256 of its bytes differs from `Game.catalog_sha256` (a missing file leaves that game's catalog as it is),
  and downloads Cardmarket's price guide per game (`ledger/cardmarket.py`, `-foil` or `-holo`). Scryfall and
  Cardmarket's product lists are not read by the app. `Category.is_single` tells cards from sealed products and
  accessories (`is_product`). After each sync `catalog.reconcile_cards` links, folds and rewrites cards to the
  catalog's set code and number, per game. Collection items are added only from the catalog
  (`catalog.card_for_blueprint`); products are stored Near Mint, non-foil. `Card.ct_blueprint_id` (unique) is the
  CardTrader blueprint, matched by the extension and stored when a CardTrader report gives it (`ct_blueprint_id` in a
  result, or in a stock row of a known listing: `extension._learn_blueprint`; one already used by another card is
  skipped with a warning in the user's log). The catalog and
  collection JSON keep the old field names (`ct_id`, `category_id`), now holding Cardmarket ids.
- **Markets and the browser extension**: `Market` ("cm" Cardmarket, "ct" CardTrader). Both are synced only by the
  Chrome extension (separate repo cardledger_extension) from the user's browser, in a logged-in tab of each market
  (seller area CSV export/import, orders pages), only when the user presses Sync; the server never calls a market.
  There is no publish, sync, order import or ship endpoint: publishing is the plan/report cycle, orders arrive in
  reports, shipping is `extension/shipped/` (both markets). `Listing` is one per (item, market) (`item.listings`, `item.listing_for(market)`,
  `active_listings()`; there is no `item.listing`): `market_listing_id` (CardTrader product id / Cardmarket article
  id; `Listing.ct_product_id` is a property over it), status to_publish|published|paused|error|sold, `error_message`,
  `listed_at`, and `pending_delete` (removed by the user while on its market: hidden everywhere, deleted there by the
  next sync, then here; re-listing reuses the row). `Order` has `market` + `market_order_id` (unique per owner and
  market; `Order.ct_id` is a property), `buyer_name`, `items_total`, `shipping_cost`, `market_fee`, `net`;
  `OrderItem` has `market_item_id`/`market_listing_id`. `services.save_order` upserts any market's order and removes
  the sold copies once (`stock_applied`); **shared stock** (`services.share_stock`): the item's listings on the other
  markets are capped to its remaining copies, a published one goes back to to_publish, one left with no copies is
  removed (pending_delete; `services.remove_listing` never deletes a listing still on its market, it flags it).
  `SyncConflict.market`; `services.diff_listings(user, market, remote)` serves both markets. `MarketConnection(user,
  market)`: handle, status off|never|ok|error, last_sync, last_error, orders_synced_at (`services.connection`/
  `mark_synced`); `services.market_states(user)` is `me`'s, the extension's and the sync page's `markets`, both from
  their connection only: off until pairing creates them, then the status the reports set. Data migrations 0036-0038
  turned the existing rows into market "ct", 0039 dropped `SyncService` (tokens), `SyncConflict.service` and
  `Game.ct_game_id` (`test_markets_migration.py`).
  Extension (`ledger/extension.py`, views `ledger/api/extension.py`, `/api/extension/`): pairing (`pair/` with the
  session → a 10-minute `ABCD-1234` code; `claim/` without session, rate-limited per IP in the cache → the raw token,
  shown once, and both markets' connections turned on; `ExtensionToken` keeps only its sha256; `tokens/` list and
  revoke). The other endpoints accept only `Authorization: Token <token>` (`ExtensionTokenAuthentication`, used
  nowhere else): `status/`, `plan/?market=` (`action_of`: create/update for to_publish|error, delete for paused or pending_delete rows on the
  market), `report/` (`apply_report`, idempotent per `ExtensionRun.run_id`: results, orders, stock reconciliation
  with `unmatched` rows, CardTrader blueprints, connection, SyncLog), `shipped/` (any market: tracking code, paid or
  hub_pending → sent). `GET /api/sync/` `connections` are the two markets (`market_states` + `orders_synced_at`,
  `open_conflicts`); `GET /api/orders/` `orders_synced_at` comes from the connections. `me` adds
  `extension: {paired, last_seen_at}`.
- **Dealer data**: `CollectionItem.cost` (purchase price per copy) and `location` (storage, e.g. "A3·12"), in the
  item API, the CSV columns and the order packing list (`orders/<pk>/` items carry `location`, sorted by it).
  `GET /api/collection/` rows add `trend` (= `price`), `cost`, `location`, `listing_price` (lowest active price, any market),
  `listings` (`[{market, status, price}]`), `added_at`; `?view=` (`_views()` in `ledger/api/collection.py`:
  `below_trend`, `not_listed`, `stale`, `bulk`; Exists over the active listings of any market, a sold or removed
  listing counts as none) with `views` counts that honour the other filters. `POST /api/collection/bulk/` acts on the user's items of
  the game (`set_location`, `delete`, `list`/`reprice` through `services.list_at_trend` on `markets`, default `["ct"]`,
  priced by `price_over_trend`). `GET /api/listings/` has `?market=`, rows per (item, market) and `by_market` stats;
  `listings/<item>/` reads all of an item's listings and PUTs/DELETEs one by `market` (default ct);
  `POST /api/listings/bulk/` pauses/resumes/deletes the listings of item ids (optionally of one market). `GET /api/overview/` (`ledger/api/overview.py`) is the dashboard: revenue and profit from the
  global orders, stock value per game, the game's price movers. The user's `accounts.SellerSettings` (created on
  demand, `seller_settings_of`) feed it: `price_rounding` ("" = 0.05, else 0.05/0.10/0.50/1.00) is the bulk price
  step, `hold_below_cost` skips items whose new price would be under their cost, `default_condition` is the add
  form's default (`auto_import_orders`/`push_prices` are stored only). Catalog rows add `rarity`, `year` (expansion
  release year), `sold_30d` (order items → listing → item → card's printing, last 30 days, canceled/merged orders
  out), `owned_locations`; filters `rarity`, `finish=foil`, `ordering`; `rarities`, and `suggestion` (pg_trgm, the
  game's closest name) when a search finds nothing; `POST /api/catalog/report-missing/` stores a
  `MissingCardReport` (admin).
- **Orders pick/pack/ship** (`ledger/api/orders.py`, helpers `ledger/shipping.py`): `Order.packed_at`, `issue_note`/
  `issue_at`, `invoice_number`, `OrderItem.picked`. `Order.objects.with_stage()` annotates `stage` (first that
  applies): issue (an issue note, request_for_cancel, lost, canceled with paid_at), shipped (sent/arrived/done, also
  closed and unpaid canceled: nothing to pack), packed, picking (a line picked), to_pack. Orders still to ship (paid,
  hub_pending) have `due_by` = local date of paid_at + max_shipping_days **working** days (Mon-Fri, 2 when null) and
  `due` overdue/today/ok (Europe/Rome), else null. `GET /api/orders/?tab=to_pack|packed|shipped|issues` (old keys
  to_ship/ct_zero → to_pack, completed → shipped): to_pack = to_pack + picking, overdue first then paid_at; shipped
  (sent/arrived/done) newest first; tab counts and stats (+ `shipped_today`) honour `?market`. Detail adds `next_id`
  (next in the to-pack queue, wrapping; null when alone). `PATCH orders/<pk>/items/<id>/ {picked}`, `POST
  orders/<pk>/pack/ {force}` (409 `blocked` if a line is unpicked or the order is not paid/hub_pending; answers
  `next_id`), `unpack/`, `issue/ {note}` (blank clears). `GET orders/pick-list/?ids=` merges lines by collection item
  (else name/expansion/condition/language/foil), sorted by location (empty last) then name; `GET orders/slips/?ids=`
  gives `business` (`accounts.api.business_data`) and the order details, numbering those without an invoice number
  (`services.assign_invoice_numbers`: prefix + `invoice_next_number` zero-padded to 4, under `select_for_update` of
  the `BusinessDetails` row). Unknown or other users' ids are ignored.
- **Shipments** (`ledger/api/shipments.py`): `Shipment` (1:1 order) is created by `services.record_shipment(order)`
  once the order is sent/arrived/done, called by `services.save_order` (extension reports) and
  `extension.mark_shipped`; later calls only fill blanks (seller edits stay) and set `delivered_at` the first time
  the order is seen arrived/done. Carrier from the shipping method's words (`shipping.carrier_for`), `expected_by` =
  shipped date + max_shipping_days calendar days (tracked 5 / untracked 10 when null). Migration 0041 backfilled the
  shipped orders (`test_orders_flow.ShipmentBackfillTest`). `Shipment.objects.with_status(today)` annotates `status`
  (returned > delivered (delivered_at or arrived/done) > no_tracking (untracked method and no code) > late (past
  expected_by) > in_transit) and `problem` (late, or no_tracking past expected_by). `GET /api/shipments/?tab=
  in_transit|delivered|problems|returned&market=&page=` (in_transit includes late and no_tracking), `kpis`
  (`on_time_pct` one decimal string, over deliveries with a date in the last 30 days), paginated;
  `PATCH shipments/<id>/` (carrier, tracking_code, postage_cost, note kept when absent; delivered/returned booleans).
  `me.counts` adds `orders_to_pack` and `shipment_problems` (every market). No tracking provider yet.
- **Account extras** (`accounts/`): login takes the username or a unique email (case-insensitive), `remember`
  false ends the session with the browser; password reset by email (`templates/accounts/`, link to the SPA's
  `/login/reset/<uid>/<token>/`); `AuditEvent` (`record(user, kind, request)`: sign-in ok/failed on a known account,
  password changed/reset; `token_saved`/`token_removed` only on old rows; IP is `REMOTE_ADDR`) behind `GET /api/account/activity/`;
  `BusinessDetails` (`business_of`; shop name and VAT stay on `Profile`) with the logo in the default storage
  (`MEDIA_ROOT`, env `DJANGO_MEDIA_ROOT`, served by `config.views.media` under `/media/`; a compose volume).
  `me` adds `counts.copies`/`live_listings` (per `?game`), `markets` (Cardmarket and CardTrader with `handle`, from
  their MarketConnection, see Markets), `extension` and `prices_updated_at`; `GET /api/account/` is only
  `password_help`. Price guide expressions for items (`trend_price`,
  `item_guide_price`, foil-aware) live in `ledger/cardmarket.py`.
- **Scan pipeline**: photo → `ledger/openrouter.py` (vision model reads the card) → `ledger/recognition.py` (matches
  it to a `Blueprint` of the local catalog) → `ledger/scan.py` (draft lines of a `ScanSession`, saved into the
  collection at the end; no prices, nothing is put on sale). `ScanSession.game` is set at creation and every later step
  takes the game from the session: `recognition.match(rec, expansion, game)`, a per-game prompt
  (`openrouter.prompt_for(slug)`), non-Magic names compared without a trailing ` [...]`/` (...)` catalog suffix,
  `catalog.expansion_by_code` falling back to a single `<code>-…` slug code; the phone URL and the save redirect
  carry `/<game>/`.
  The scan API (`ledger/api/scan.py`) is the only one with real DRF serializers (`ledger/api/serializers.py`).
- **API conventions** (`ledger/api/common.py`, `ledger/api/errors.py`): session auth + CSRF (`X-CSRFToken` from the
  `csrftoken` cookie); validation goes through Django forms and `form_errors(form)` turns them into DRF 400s;
  actions answer `{"messages": [{"level", "text"}]}` via `messages_response`/`msg`; other errors are
  `{"detail", "code"}` with custom codes (`Blocked` 409, `ServiceError` 502, ...). Every non-scan view is documented
  with `@doc(tags, summary, request=..., parameters=...)`; request bodies are generated from the validating form with
  `form_body()`, so keep the form and the docs in sync through it. Responses are documented as a generic object.
  Swagger UI is public at `/api/docs/`, schema at `/api/schema/`, version read from `pyproject.toml`.
- **Background work**: Celery with Redis, no beat; no tasks are defined: `sync_catalog` runs the sync in-process and is scheduled
  externally.

## Tests

Django's `TestCase` on PostgreSQL. Most ledger tests extend `LedgerTestCase` (`ledger/tests.py`): a superuser `gab`
with demo data (a synced CardTrader connection; `publish()` marks its to-publish listings published as an extension
sync would) and a second user `ami`; nothing touches the network. Tests also live in `ledger/test_*.py`, `accounts/tests.py` and `config/tests.py`. `SchemaTest` in
`ledger/test_scan_api.py` fails on any drf-spectacular warning.

## Conventions

- The image's CMD runs `migrate --noinput` before gunicorn (not in the worker); data migrations exist for the single-
  to multi-user upgrade.
- `docs/superpowers/` is gitignored (local design notes): never commit it.
- Work on a branch in the main checkout; PRs are squash-merged into `main` and named after the change.
  Releases (GitHub release → Docker image on ghcr.io) bump `version` in `pyproject.toml` and move the `ui` submodule
  to the matching cardledger_ui tag.
