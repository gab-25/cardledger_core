# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

CardLedger server: a Django 6 + Django REST Framework app (Python 3.14, managed with `uv`) that holds a trading card
collection (Magic, Pokémon, Lorcana, One Piece, Yu-Gi-Oh! and Riftbound) and manages CardTrader sales. It exposes
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
- **Dealer data**: `CollectionItem.cost` (purchase price per copy) and `location` (storage, e.g. "A3·12"), in the
  item API, the CSV columns and the order packing list (`orders/<pk>/` items carry `location`, sorted by it).
  `GET /api/collection/` rows add `trend` (= `price`), `cost`, `location`, `listing_price`, `added_at`; `?view=`
  (`_views()` in `ledger/api/collection.py`: `below_trend`, `not_listed`, `stale`, `bulk`; a sold listing counts as
  none) with `views` counts that honour the other filters. `POST /api/collection/bulk/` acts on the user's items of
  the game (`set_location`, `delete`, `list`/`reprice` through `services.list_at_trend`, priced by
  `price_over_trend`). `GET /api/overview/` (`ledger/api/overview.py`) is the dashboard: revenue and profit from the
  global orders, stock value per game, the game's price movers. Price guide expressions for items (`trend_price`,
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
