# cardledger_core
CardLedger — trading card collection and CardTrader sales manager (Django).

This repository is the server: the data, the CardTrader and OpenRouter integrations and a JSON API under `/api/`.
The interface is a React single-page app in [cardledger_ui](https://github.com/gab-25/cardledger_ui); Django serves
its `index.html` for every page URL and its assets under `/static/ui/`.

## Structure

- `config/` — Django project (settings, root URLs); `config/views.py` serves the built React UI (`index.html` for
  every URL that is not `/api/`, `/admin/` or `/static/`)
- `accounts/` — account API (`accounts/api.py`): who is logged in, login/logout, profile, password, CardTrader token
- `ledger/` — domain: cards, collection, sale listings, shipments, CardTrader client and sync, CSV import/export;
  `ledger/api/` is the JSON API of every page (collection, sale, cards, orders, sync, scan)

## Getting started

The app uses PostgreSQL. Start a local instance with Docker:

```bash
docker compose up -d db
```

Connection settings come from environment variables (defaults match `compose.yaml`):
`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` (all default `cardledger`), `POSTGRES_HOST` (`localhost`),
`POSTGRES_PORT` (`5432`), `POSTGRES_CONN_MAX_AGE` (`60` seconds).

```bash
uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

Tests: `uv run python manage.py test`

The pages come from the React UI, a git submodule in `ui/` pinned to a commit of cardledger_ui: build it once and
Django serves `ui/dist` (set `UI_APP_DIR` to serve another build). Without an `index.html` there every page answers
*UI not built*.

```bash
git submodule update --init                     # or clone with --recurse-submodules
(cd ui && npm ci && npm run build)
uv run python manage.py runserver
```

To work on the interface, run Django as above and `npm run dev` in `ui/` (or in a separate cardledger_ui checkout):
Vite serves the whole site with live reload and forwards `/api/`, `/admin/` and `/static/` to Django.

## Docker

The image is published to `ghcr.io/gab-25/cardledger_core` only when a GitHub release is published (`.github/workflows/release.yml`, after the tests pass). Tests run on every PR and push to `main` (`.github/workflows/tests.yml`).

Run the whole stack (PostgreSQL + app built from the local `Dockerfile`) with Docker Compose:

```bash
docker compose up -d --build
docker compose exec cardledger python manage.py createsuperuser
```

The app is served on http://localhost:8000 (`APP_PORT` to change it). Compose reads overrides from the environment
or a `.env` file — set at least `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD` outside local use.

To run the published image against an existing PostgreSQL:

```bash
docker run -p 8000:8000 \
  -e POSTGRES_HOST=db.example.com -e POSTGRES_PASSWORD=... \
  -e DJANGO_SECRET_KEY=... -e DJANGO_ALLOWED_HOSTS=ledger.example.com \
  -e DJANGO_CSRF_TRUSTED_ORIGINS=https://ledger.example.com \
  ghcr.io/gab-25/cardledger_core:latest
```

Environment variables: the `POSTGRES_*` ones above, `DJANGO_SECRET_KEY`, `DJANGO_DEBUG` (default `false` in the image),
`DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` (comma-separated), `DJANGO_BEHIND_HTTPS_PROXY` (`true` when a
reverse proxy terminates HTTPS and sets `X-Forwarded-Proto`), `GUNICORN_WORKERS`,
`OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `CELERY_BROKER_URL` (default `redis://localhost:6379/0`), and for the catalog
files `CARDLEDGER_CATALOG_BUCKET`, `CARDLEDGER_CATALOG_ENDPOINT` (only for S3-compatible storage, empty = AWS),
`CARDLEDGER_CATALOG_REGION` (empty = `us-east-1`, `auto` for Cloudflare R2), `CARDLEDGER_CATALOG_ACCESS_KEY_ID` and
`CARDLEDGER_CATALOG_SECRET_ACCESS_KEY` (the `AWS_*` variables are never read); see below.
The app container applies pending migrations (`python manage.py migrate`) every time it starts, before gunicorn; the
Celery worker does not. If a migration fails the container exits without serving, and its logs show why.

## Users

Several people can share one Ledger, each with their own collection: every user sees and changes only their own
cards, listings, orders, scan sessions, sync conflicts and log entries (another user's object answers 404). The card
catalog and the prices are shared. The administrator creates the accounts in the Django admin (`/admin/`); there is no
sign-up page.

Upgrading a single-user Ledger: the existing data goes to the first superuser, so create one before upgrading if there
is none (the migration stops with a message otherwise, and the new container does not start: create the superuser with
the old image, or with a one-off container of the new one, e.g.
`docker compose run --rm cardledger python manage.py createsuperuser`, which skips the migrations).

## CardTrader

Every user connects their own CardTrader token from **Profile and settings → Connected services** (the token is checked
with CardTrader before it is saved; only its owner can show or copy it there); it is used only for that user's listings, orders and
shipments, and a CardTrader account can be connected to one user only. Nothing else uses a CardTrader token: the card
catalog, prices and scanning need none. `CARDTRADER_API_TOKEN` is no longer read (nor passed by `compose.yaml`): on upgrade, if it is
still set where `migrate` runs and no token was saved in the profile, the migration copies it into the first
superuser's profile; otherwise connect the token from the profile after upgrading.

- **Publish**: listings "To publish" are created/updated on CardTrader with the bulk API. Items added from the catalog
  already know their CardTrader product; cards imported from CSV are matched to the catalog (blueprint) by set code +
  name the first time, and unmatched cards stay pending with a warning. Sealed products and accessories are sold with
  only the properties CardTrader takes for them (a language, when it has one).
- **Sync all**: imports sale orders (removing sold copies from the Ledger following CardTrader's CardTrader Zero
  rules), then compares published listings with your CardTrader inventory and opens conflicts.
- **Shipments**: orders to ship with packing number, address, printable packing list, tracking code and
  "Mark as shipped".

Client: `ledger/cardtrader.py` (endpoints from https://www.cardtrader.com/docs/api/full/reference).
Condition mapping: M→Mint, NM→Near Mint, EX→Slightly Played, GD→Moderately Played, LP→Played,
PL→Heavily Played, PO→Poor.

## Card database and Cardmarket prices

The Ledger holds the catalog of six games: Magic, Pokémon, Lorcana, One Piece, Yu-Gi-Oh! and Riftbound (`Game`).
Expansions, categories and cards belong to a game and every lookup stays inside one game. The catalog API takes
`?game=<slug>` (`mtg` by default); users still see only Magic until the UI lets them choose the game.

The Ledger rebuilds the catalog from the files of the generator
[cardledger_database](https://github.com/gab-25/cardledger_database), kept in a private S3 bucket
(one `catalog/v1/<slug>.json` per game, a JSON object with `expansions` and `products`, no manifest). The generator is run by hand (`cardledger-db generate`, then
`cardledger-db upload`). The catalog sync is run by hand (`sync_catalog`, see below): it downloads each game's file and
imports it only when the sha256 of its bytes differs from the one it last imported. It also downloads Cardmarket's public price guide of each game (low, trend, average and the
1/7/30-day averages, regular and foil or holo) and matches your cards to the catalog printings, game by game; it logs
how many cards have no printing. Scryfall and Cardmarket's product lists are no longer read by the app, and no token
is needed. The app only needs read access to the bucket (`s3:GetObject`, plus `s3:ListBucket` so a missing key is
reported as missing).

- **Cards** page: search the card database by name, set code or collector number (or only the cards you own) and see
  how many copies you own and the Cardmarket prices.
- **Collection**: the price of each copy is the Cardmarket trend of its printing (foil trend for foil copies),
  computed on every page load, never typed; the estimated value is trend × quantity. Copies without a trend show "–".
- **Listing form**: the Cardmarket trend (foil trend for foil copies) is shown and prefills the price of a new
  listing.

The sync runs on a Celery worker (Redis broker) and is started by hand: `sync_catalog` queues it. Run it after
uploading new generator files, and whenever you want fresh prices: Cardmarket regenerates the price guide once a day
(around 02:45 CET), and prices change only when the sync runs.

```bash
docker compose up -d redis
uv run celery -A config worker --loglevel=info      # the worker
uv run python manage.py sync_catalog                # queue a sync (outcome in Sync → Activity log)
```

With Docker Compose the `worker` and `redis` services start with the stack; queue a sync with
`docker compose exec cardledger python manage.py sync_catalog`.

When the catalog files cannot be read (bucket not configured, a game without its file in the bucket, a file that cannot
be downloaded or a corrupt one), the games that already have a catalog keep it as it is but still get Cardmarket's current prices; their line
in the Activity log is a warning that tells why the catalog was not updated. A game that fails for any other reason is
rolled back without stopping the others.

### Deploying this version

- Set the `CARDLEDGER_CATALOG_*` variables (bucket, region, access key and secret, plus the endpoint for
  S3-compatible storage) on the worker, where the sync runs, and upload the generator's files to the bucket first
  (`cardledger-db generate` then `cardledger-db upload`; no manifest is needed): without them the catalog is not
  rebuilt (Magic's prices still are).
- Restart the app and the worker together: the app applies the migrations when it starts, and an old worker running
  the sync after them would fail.
- Rolling back below migration `0028` (or `0032`, which allows set codes up to 40 characters) is clean only before
  the first multi-game sync, or after deleting the `Expansion`, `Category`, `Blueprint` and `Card` rows of the games
  other than Magic.

## Scanning cards with the camera

**Collection → Scan cards** opens a phone-friendly flow: pick an expansion (or *All expansions*) and a default
condition, then frame one card at a time. **Auto-shoot** takes the photo by itself as soon as a card is in the box
and in focus, and re-arms only once the box is empty again (the shutter button still works). The photo is
recognised by a vision model on OpenRouter, matched against the local catalog and added to a draft.
Uncertain cards ask you to pick among candidates; **Review** shows the draft (duplicates merged), lets you set a
"list for sale above €X" threshold and saves everything at once (listed copies get the CardTrader market price; listings
are created as *To publish*).

- The scan screens are part of the React UI ([cardledger_ui](https://github.com/gab-25/cardledger_ui)), under
  `/scan/...`; they talk to the JSON API under `/api/scan/`. All the scan logic stays here.
- The API is built with Django REST Framework (`ledger/api/`, `accounts/api.py`), with the Django session and the
  `csrftoken` cookie (sent back as `X-CSRFToken`). Its OpenAPI schema is at `/api/schema/` and the interactive docs
  at `/api/docs/` (both public); `uv run python manage.py spectacular --file schema.yaml` writes the schema.
  Errors use DRF's format: `{"field": ["message"]}` for validation (400), otherwise `{"detail", "code",
  "help_url"?}`, with `code` such as `not_authenticated` (403), `not_found`, `closed` (409: the session was already
  saved), `blocked` (409), `service_missing` (503) or `service_error` (502: CardTrader failed). Actions answer
  `{"messages": [{"level", "text"}]}`, shown by the UI like Django's messages.
- The Docker image builds the UI from the `ui` submodule (a Node stage in the `Dockerfile`), so the submodule must be
  checked out before `docker build`. To ship a new version of the interface, move the submodule to its release tag
  and commit it: `git -C ui fetch --tags && git -C ui checkout vX.Y.Z && git add ui`.
- OpenRouter is configured only on the server, through `OPENROUTER_API_KEY` (it cannot be set from the profile);
  `OPENROUTER_MODEL` picks the vision model (e.g. `qwen/qwen3.8-flash`) and has no default: card recognition
  needs both.
- The catalog and prices are loaded and refreshed by the catalog sync, run by hand
  (`uv run python manage.py sync_catalog`); run it once after the first migrate.
- The camera works on **smartphones only** (webcams can't focus on a card up close). A session started on a
  computer shows a **QR code** that opens the same session on the phone; Review, confirmation and search work on
  both.
- The browser only gives camera access over **HTTPS**.

### Trying it from your phone in development (Tailscale)

The camera needs HTTPS. [Tailscale](https://tailscale.com) gives the development server an HTTPS address with a
real certificate that only your own devices can reach; cardledger_ui's `npm run dev:phone` publishes it.

1. Install Tailscale on the computer and on the phone, signed in with the same account, and keep it connected on
   the phone while testing. In the Tailscale admin console enable **MagicDNS** and **HTTPS Certificates**. If
   `tailscale serve` is refused, allow your user once with `sudo tailscale set --operator=$USER`.
2. Allow the tailnet name of this computer (`<pc>.<tailnet>.ts.net`, shown by `tailscale status`) in `.env`:
   ```
   DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,<pc>.<tailnet>.ts.net
   DJANGO_CSRF_TRUSTED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000,https://<pc>.<tailnet>.ts.net
   DJANGO_BEHIND_HTTPS_PROXY=true
   ```
   With `DJANGO_BEHIND_HTTPS_PROXY` Django trusts `X-Forwarded-Proto` from Tailscale; plain `localhost` requests
   carry no such header and stay HTTP.
3. Start the database (`docker compose up -d db`) and Django (`uv run python manage.py runserver`), with
   `OPENROUTER_API_KEY` and `OPENROUTER_MODEL` set for card recognition.
4. In cardledger_ui run `npm run dev:phone`: it checks Tailscale and that Django accepts the tailnet name, then
   publishes the Vite dev server (the UI with live reload, `/api/`, `/admin/` and `/static/` forwarded to Django) with
   `tailscale serve` and prints the `https://<pc>.<tailnet>.ts.net` address. Ctrl+C removes the Tailscale rule.
5. Open that address on the computer, connect CardTrader in **Profile and settings**, start a session in
   **Scan cards** and scan the QR code with the phone.

To try the production setup instead (Django serving the built UI, as in the Docker image), build it with
`npm run build` in `ui/`, start Django (it serves `ui/dist` by default) and expose port 8000
with `tailscale serve --https=443 8000`.

If the QR page says the address is not HTTPS, the proxy did not send `X-Forwarded-Proto`.

## CSV

Columns: `name,set,condition,language,foil,quantity,number` (`number` = collector number, optional: it identifies the exact printing and ties the card to the catalog). Old files with `type`, `color` or `price` columns still import: those columns are ignored.
