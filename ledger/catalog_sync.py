"""Nightly catalog sync: each game's catalog from the generator's files on S3, its prices from Cardmarket.

cardledger_database generates one file per game (expansions and Cardmarket products, with set code, collector number,
rarity and images) and a manifest with their checksums (ledger/catalog_files.py); a game's file is imported only when
its checksum changed. Cardmarket's public price guide of each game gives the prices, every night. Then the Ledger's
cards of that game are aligned with the catalog (catalog.reconcile_cards). One SyncLog line per game.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from itertools import batched

from botocore.exceptions import BotoCoreError
from django.db import transaction

from . import cardmarket, catalog
from .cardmarket import PRICE_FIELDS, CardmarketError
from .catalog_files import CatalogBucket, CatalogFilesError
from .models import Blueprint, CardmarketPrice, Category, Expansion, Game, SyncLog

BATCH = 2000
# A game's blueprints missing from its file are removed only when the file holds at least this share of that game's
# catalog: a truncated or emptied file must not wipe it.
PRUNE_MIN_SHARE = 0.5

logger = logging.getLogger(__name__)


class CatalogSyncError(Exception):
    pass


@dataclass
class GameResult:
    game: str  # slug
    imported: bool  # its file changed and was imported
    expansions: int
    blueprints: int
    priced: int
    pruned: int
    reconciled: catalog.ReconcileResult
    guide_date: datetime | None
    error: str = ""


def is_single_category(name):
    """Whether a Cardmarket category holds single cards ("Magic Single", "Yugioh Singles"), not products."""
    return name.strip().lower().endswith(("single", "singles"))


def _check(file):
    """Refuse a game file that cannot be written: a product of an unknown expansion, a set code too long."""
    codes = {e.code for e in file.expansions}
    too_long = sorted(c for c in codes if len(c) > Expansion._meta.get_field("code").max_length)
    if too_long:
        raise CatalogSyncError(f"set codes too long: {', '.join(too_long[:5])}")
    unknown = sorted({p.expansion_code for p in file.products} - codes)
    if unknown:
        raise CatalogSyncError(f"products of unknown expansions: {', '.join(unknown[:5])}")


def _prune(game, written):
    """Remove game's blueprints its file no longer holds (not those an open scan line uses). Returns how many went."""
    from .models import ScanLine, ScanSession

    stale = Blueprint.objects.filter(expansion__game=game).exclude(cm_product_id__in=written)
    in_use = ScanLine.objects.filter(session__status=ScanSession.Status.OPEN).exclude(blueprint=None)
    stale = stale.exclude(pk__in=in_use.values("blueprint_id"))
    ScanLine.objects.filter(blueprint__in=stale).update(blueprint=None)  # saved sessions keep their history
    return stale.delete()[1].get("ledger.Blueprint", 0)


def _import(game, file):
    """Write game's file: its expansions, categories and blueprints; then its stale blueprints and its emptied
    expansions and categories go. Returns how many blueprints were pruned."""
    products = {p.id_product: p for p in file.products}  # a product listed twice would fail the batch
    prune = len(products) >= PRUNE_MIN_SHARE * Blueprint.objects.filter(expansion__game=game).count()
    expansions = {e.code: e for e in file.expansions}
    Expansion.objects.bulk_create(
        [Expansion(game=game, code=e.code, name=e.name[:200], released_at=e.released_at,
                   language=(e.language or "")[:5]) for e in expansions.values()],
        update_conflicts=True, unique_fields=["game", "code"], update_fields=["name", "released_at", "language"],
    )
    categories = {p.category_id: p.category_name for p in products.values()}
    Category.objects.bulk_create(
        [Category(game=game, cm_id=i, name=name[:100], is_single=is_single_category(name))
         for i, name in categories.items()],
        update_conflicts=True, unique_fields=["game", "cm_id"], update_fields=["name", "is_single"],
    )
    expansion_pk = dict(Expansion.objects.filter(game=game).values_list("code", "pk"))
    category_pk = dict(Category.objects.filter(game=game).values_list("cm_id", "pk"))
    # Models are built and written one batch at a time: all of them at once do not fit in the worker.
    for chunk in batched(products.values(), BATCH):
        Blueprint.objects.bulk_create(
            [
                Blueprint(cm_product_id=p.id_product, name=p.name[:200], expansion_id=expansion_pk[p.expansion_code],
                          collector_number=p.number[:40], rarity=p.rarity[:40], image_url=p.image_url,
                          image_large_url=p.image_large_url, category_id=category_pk[p.category_id])
                for p in chunk
            ],
            update_conflicts=True, unique_fields=["cm_product"],
            update_fields=["expansion", "name", "collector_number", "rarity", "image_url", "image_large_url",
                           "category"],
        )
    pruned = 0
    if prune:
        pruned = _prune(game, list(products))
    else:
        logger.warning("Catalog sync: %s file holds %s printings, too few to prune its catalog",
                       game.slug, len(products))
    Category.objects.filter(game=game, blueprints__isnull=True).delete()
    Expansion.objects.filter(game=game, blueprints__isnull=True).delete()
    game.catalog_sha256 = file.sha256
    game.catalog_generated_at = file.generated_at
    game.save(update_fields=["catalog_sha256", "catalog_generated_at"])
    return pruned


def _write(game, file, prices, guide_date):
    """Write game's file (None: unchanged) and its prices in one transaction. Returns how many blueprints went."""
    with transaction.atomic():
        pruned = _import(game, file) if file is not None else 0
        for chunk in batched(prices.items(), BATCH):
            CardmarketPrice.objects.bulk_create(
                [cardmarket.price_row(id_product, values, guide_date) for id_product, values in chunk],
                update_conflicts=True, unique_fields=["id_product"],
                update_fields=[*PRICE_FIELDS.values(), "prices_date"],
            )
    return pruned


def _sync_game(bucket, cm_client, game, entry):
    """Import game's file when it changed and its prices, then align its cards. Everything is downloaded before
    anything is written: a file or guide that cannot be read leaves the game's catalog as it was."""
    try:
        if not isinstance(entry, dict):
            raise CatalogSyncError("not in the catalog manifest" if entry is None else "invalid manifest entry")
        file = None
        if not game.catalog_sha256 or entry.get("sha256") != game.catalog_sha256:
            file = bucket.game_file(game.slug, entry)
            _check(file)
        prices, guide_date = cardmarket.fetch_guide(cm_client, game.cm_id)
    except (CatalogSyncError, CatalogFilesError, CardmarketError) as exc:
        return GameResult(game=game.slug, imported=False, expansions=0, blueprints=0, priced=0, pruned=0,
                          reconciled=catalog.ReconcileResult(), guide_date=None, error=str(exc))
    imported = file is not None
    pruned = _write(game, file, prices, guide_date)
    del file
    blueprints = priced = 0
    for cm in Blueprint.objects.filter(expansion__game=game).values_list("cm_product_id", flat=True):
        blueprints += 1
        priced += cm in prices
    return GameResult(
        game=game.slug, imported=imported, expansions=Expansion.objects.filter(game=game).count(),
        blueprints=blueprints, priced=priced, pruned=pruned, reconciled=catalog.reconcile_cards(game),
        guide_date=guide_date,
    )


def sync(bucket, cm_client):
    """Sync every game, in slug order; a game that fails is told in its result and does not stop the others."""
    games = bucket.manifest().get("games")
    if not isinstance(games, dict):
        raise CatalogSyncError("the catalog manifest has no games")
    return [_sync_game(bucket, cm_client, game, games.get(game.slug)) for game in Game.objects.order_by("slug")]


def _summary(name, result):
    if result.error:
        return f"{name} catalog sync failed: {result.error}"
    r = result.reconciled
    message = (
        f"{name} catalog: {result.expansions} expansions, {result.blueprints} printings, {result.priced} priced "
        f"(guide {result.guide_date:%Y-%m-%d}), {r.linked} cards linked, {r.unlinked} not linked"
    )
    if not result.imported:
        message += ", file unchanged"
    if result.pruned:
        message += f", {result.pruned} printings removed"
    if r.clashes:
        message += f", {r.clashes} duplicates to merge by hand"
    return message


def _failed(exc):
    SyncLog.objects.create(message=f"Catalog sync failed: {exc}"[:300], level=SyncLog.Level.WARNING)


def run_sync(bucket=None, cm_client=None):
    """sync() with its outcome written to the SyncLog, one line per game. Returns the results, or None when it could
    not run (no bucket, unreadable manifest)."""
    try:
        bucket = bucket if bucket is not None else CatalogBucket()
    except (CatalogFilesError, BotoCoreError, ValueError) as exc:  # not configured, no region, bad endpoint
        _failed(exc)
        return None
    own_cm = cm_client is None
    cm_client = cm_client or cardmarket.make_client()
    try:
        results = sync(bucket, cm_client)
    except (CatalogSyncError, CatalogFilesError) as exc:
        _failed(exc)
        return None
    except Exception as exc:
        # Unexpected (database, bug): still visible in the Activity log, and the task is marked failed.
        logger.exception("Catalog sync failed")
        _failed(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if own_cm:
            cm_client.close()
    names = dict(Game.objects.values_list("slug", "name"))
    for result in results:
        warn = result.error or result.reconciled.clashes
        SyncLog.objects.create(message=_summary(names[result.game], result)[:300],
                               level=SyncLog.Level.WARNING if warn else SyncLog.Level.OK)
    return results
