# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

CardLedger server: a Django 6 + Django REST Framework app (Python 3.14, managed with `uv`) that holds a trading card
collection (Magic, Pokémon, Lorcana, One Piece, Yu-Gi-Oh! and Riftbound) and manages sales on Cardmarket and CardTrader (synced by a browser extension). It exposes
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
  module per UI page; business logic lives in `ledger/services.py` (CardTrader sync, publish, orders, CSV) and
  `ledger/scan.py`, not in the views.
- **Multi-user ownership**: every personal row (collection items, listings, orders, scan sessions, conflicts, logs)
  has an owner, reached through `owned_manager(path)` / `OwnedQuerySet.owned_by(user)` in `ledger/models.py`. Views
  must always start from `Model.objects.owned_by(request.user)` so another user's object answers 404
  (`ledger/test_ownership.py`). `SyncLog.objects.visible_to(user)` adds the global entries. The card catalog
  (`Expansion`, `Blueprint`) and prices are shared.
- **CardTrader tokens**: each user has their own token on a `SyncService` row (`services.get_cardtrader(user)`);
  `services.user_client(user)` never falls back to another user's token, and nothing uses a shared or admin token:
  CardTrader is only reached to sell (publish, orders, shipments). All clients are built by `services.make_client`,
  which tests patch.
- **Cards vs printings**: `Card` is the Ledger's own row (name + set + collector number) that `CollectionItem`
  points to, tied to its printing by `Card.cm_product_id`; `Blueprint` is a product of the local catalog, keyed by
  its Cardmarket product id (`cm_product`, also the row of its Cardmarket price). **Games**: `Game` (mtg, pokemon,
  lorcana, onepiece, yugioh, riftbound); `Expansion`, `Category` and `Card` belong to a game, uniqueness is per game,
  and every catalog lookup (recognition, reconcile, resolve_blueprints, API) stays inside one game. Collection, import,
  export, listings, catalog and scan setup take `?game=<slug>` through `game_param(params)` / `GAME` in
  `ledger/api/common.py` (default `mtg`, unknown slug 400); `GET /api/scan/sessions/`, `POST /api/listings/publish/` and `me`'s
  `pending_changes` without `game` (absent or blank) cover every game; item and listing detail (`collection/<pk>/`, `listings/<pk>/`) return their `game`;
  `GET /api/games/` lists the games. Orders, shipments, sync and `orders_to_ship` stay global. The catalog is rebuilt
  only by the nightly `ledger/catalog_sync.py`, with no
  CardTrader token: it imports the files of the separate generator repo gab-25/cardledger_database from a private
  S3 bucket (`ledger/catalog_files.py`: one `catalog/v1/<slug>.json` per game, a JSON object with `expansions` and `products`, no manifest; the generator is run
  by hand, `cardledger-db generate` then `upload`; settings `CARDLEDGER_CATALOG_*`: bucket, endpoint, region, access
  key and secret; `AWS_*` is never read): each game's file is downloaded every night and imported only when the
  sha256 of its bytes differs from `Game.catalog_sha256` (a missing file leaves that game's catalog as it is),
  and downloads Cardmarket's price guide per game (`ledger/cardmarket.py`, `-foil` or `-holo`). Scryfall and
  Cardmarket's product lists are not read by the app. `Category.is_single` tells cards from sealed products and
  accessories (`is_product`). After each sync `catalog.reconcile_cards` links, folds and rewrites cards to the
  catalog's set code and number, per game. Collection items are added only from the catalog
  (`catalog.card_for_blueprint`); products are stored Near Mint, non-foil. When publishing,
  `services.resolve_blueprints` finds the CardTrader blueprint with the seller's token (CardTrader's `card_market_ids`, then
  number, then name) and what CardTrader takes for a product (its language is sent only if taken).
  `services.ct_game_id(client, game)` finds CardTrader's game through `/games` and stores it in `Game.ct_game_id`;
  expansions are matched per CardTrader game, and `ledger/ct_properties.py` derives the language and foil property
  names per game from the blueprint's `editable_properties` (Magic's payload is unchanged); a non-Magic listing
  CardTrader can't describe fails instead of being published. The catalog and
  collection JSON keep the old field names (`ct_id`, `category_id`), now holding Cardmarket ids.
- **Markets and the browser extension**: `Market` ("cm" Cardmarket, "ct" CardTrader). Both are synced by the
  Chrome extension (separate repo cardledger_extension) from the user's browser, only when the user presses Sync;
  the server never calls a market for it. The old CardTrader endpoints (`listings/publish/`, `sync/run/`,
  `orders/import/`, `orders/<pk>/ship/`, single listing DELETE) still call CardTrader with the user's token, for
  CardTrader rows only. `Listing` is one per (item, market) (`item.listings`, `item.listing_for(market)`,
  `active_listings()`; there is no `item.listing`): `market_listing_id` (CardTrader product id / Cardmarket article
  id; `Listing.ct_product_id` is a property over it), status to_publish|published|paused|error|sold, `error_message`,
  `listed_at`, and `pending_delete` (removed by the user while on its market: hidden everywhere, deleted there by the
  next sync, then here; re-listing reuses the row). `Order` has `market` + `market_order_id` (unique per owner and
  market; `Order.ct_id` is a property), `buyer_name`, `items_total`, `shipping_cost`, `market_fee`, `net`;
  `OrderItem` has `market_item_id`/`market_listing_id`. `services.save_order` upserts any market's order and removes
  the sold copies once (`stock_applied`); **shared stock** (`services.share_stock`): the item's listings on the other
  markets are capped to its remaining copies, a published one goes back to to_publish, one left with no copies is
  removed (pending_delete). `SyncConflict.market` (`service` is CardTrader's SyncService or null);
  `services.diff_listings(user, market, remote)` serves both markets. `MarketConnection(user, market)`: handle,
  status off|never|ok|error, last_sync, last_error, orders_synced_at (`services.connection`/`mark_synced`/
  `mark_failed`); `services.market_states(user)` is `me`'s and the extension's `markets` (CardTrader keeps its old
  token + log rule, Cardmarket is its connection: off until pairing). Data migrations 0036-0038 turned the existing
  rows into market "ct" (`test_markets_migration.py`).
  Extension (`ledger/extension.py`, views `ledger/api/extension.py`, `/api/extension/`): pairing (`pair/` with the
  session → a 10-minute `ABCD-1234` code; `claim/` without session, rate-limited per IP in the cache → the raw token,
  shown once; `ExtensionToken` keeps only its sha256; `tokens/` list and revoke). The other endpoints accept only
  `Authorization: Token <token>` (`ExtensionTokenAuthentication`, used nowhere else): `status/`, `cardtrader-token/`,
  `plan/?market=` (`action_of`: create/update for to_publish|error, delete for paused or pending_delete rows on the
  market), `report/` (`apply_report`, idempotent per `ExtensionRun.run_id`: results, orders, stock reconciliation
  with `unmatched` rows, connection, SyncLog), `shipped/`. `me` adds `extension: {paired, last_seen_at}`.
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
- **Account extras** (`accounts/`): login takes the username or a unique email (case-insensitive), `remember`
  false ends the session with the browser; password reset by email (`templates/accounts/`, link to the SPA's
  `/login/reset/<uid>/<token>/`); `AuditEvent` (`record(user, kind, request)`: sign-in ok/failed on a known account,
  password changed/reset, CardTrader token saved/removed; IP is `REMOTE_ADDR`) behind `GET /api/account/activity/`;
  `BusinessDetails` (`business_of`; shop name and VAT stay on `Profile`) with the logo in the default storage
  (`MEDIA_ROOT`, env `DJANGO_MEDIA_ROOT`, served by `config.views.media` under `/media/`; a compose volume).
  `me` adds `counts.copies`/`live_listings` (per `?game`), `markets` (Cardmarket and CardTrader with `handle`, see
  Markets; CardTrader: off without a token, never before any sync, error when the user's latest `CardTrader:` log
  entry has `SyncLog.Level.ERROR`, written when a sync or publish raises, else ok), `extension` and
  `prices_updated_at`. Price guide expressions for items (`trend_price`,
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
with demo data and a CardTrader token, a second user `ami`, and `FakeCardTrader` patched into `services.make_client`
(no network). Tests also live in `ledger/test_*.py`, `accounts/tests.py` and `config/tests.py`. `SchemaTest` in
`ledger/test_scan_api.py` fails on any drf-spectacular warning.

## Conventions

- The image's CMD runs `migrate --noinput` before gunicorn (not in the worker); data migrations exist for the single-
  to multi-user upgrade.
- `docs/superpowers/` is gitignored (local design notes): never commit it.
- Work on a branch in the main checkout; PRs are squash-merged into `main` and named after the change.
  Releases (GitHub release → Docker image on ghcr.io) bump `version` in `pyproject.toml` and move the `ui` submodule
  to the matching cardledger_ui tag.
