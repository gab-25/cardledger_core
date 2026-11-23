"""Nightly catalog sync: each game's catalog from the generator's files on S3, its prices from Cardmarket.

cardledger_database generates one file per game (expansions and Cardmarket products, with set code, collector number,
rarity and images; ledger/catalog_files.py), with no manifest: each game's file is downloaded every night and imported
only when the sha256 of its bytes differs from the one last imported. Cardmarket's public price guide of each game gives the prices, every night. Then the Ledger's
cards of that game are aligned with the catalog (catalog.reconcile_cards). One report line per game (not in the
SyncLog).

A game whose file cannot be read (no bucket, no file for it, corrupt file) keeps its catalog but still gets its prices
when it has a catalog; a game that fails for any other reason is rolled back without stopping the others.
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from itertools import batched

from botocore.exceptions import BotoCoreError
from cardledger_vision import Index
from django.db import transaction

from . import cardmarket, catalog
from .cardmarket import PRICE_FIELDS, CardmarketError
from .catalog_files import CatalogBucket, CatalogFileMissing, CatalogFilesError
from .models import Blueprint, CardmarketPrice, Category, Expansion, Game, GameVisionIndex

BATCH = 2000
# A game's blueprints missing from its file are removed only when the file holds at least this share of that game's
# catalog: a truncated or emptied file must not wipe it.
PRUNE_MIN_SHARE = 0.5

VISION_OK = ("", "updated", "unchanged", "missing")  # any other GameResult.vision is an error text

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
    error: str = ""  # with guide_date set: the catalog was not updated, the prices were
    unexpected: bool = False  # error is a bug or a database failure, not a file or Cardmarket problem
    vision: str = ""  # the vision index: "updated", "unchanged", "missing" or what kept it from being stored


def is_single_category(name):
    """Whether a Cardmarket category holds single cards ("Magic Single", "Yugioh Singles"), not products."""
    return name.strip().lower().endswith(("single", "singles"))


def _check(file):
    """Refuse a game file that cannot be written: an empty file (e.g. a failed upload), a product of an unknown
    expansion, a set code missing or too long."""
    if not file.products:
        raise CatalogSyncError("empty catalog file")
    codes = {e.code for e in file.expansions}
    if not all(isinstance(c, str) and c for c in codes):
        raise CatalogSyncError("expansions without a set code")
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


def _text(value, field):
    """A text field of the file as the model can hold it: missing becomes empty, too long is cut."""
    return str(value or "")[:field.max_length]


def _import(game, file):
    """Write game's file: its expansions, categories and blueprints; then its stale blueprints and its emptied
    expansions and categories go. Returns how many blueprints were pruned."""
    products = {p.id_product: p for p in file.products}  # a product listed twice would fail the batch
    prune = len(products) >= PRUNE_MIN_SHARE * Blueprint.objects.filter(expansion__game=game).count()
    expansions = {e.code: e for e in file.expansions}
    field = Expansion._meta.get_field
    Expansion.objects.bulk_create(
        [Expansion(game=game, code=e.code, name=_text(e.name, field("name")), released_at=e.released_at,
                   language=_text(e.language, field("language"))) for e in expansions.values()],
        update_conflicts=True, unique_fields=["game", "code"], update_fields=["name", "released_at", "language"],
    )
    categories = {p.category_id: p.category_name for p in products.values()}
    Category.objects.bulk_create(
        [Category(game=game, cm_id=i, name=_text(name, Category._meta.get_field("name")),
                  is_single=is_single_category(name or "")) for i, name in categories.items()],
        update_conflicts=True, unique_fields=["game", "cm_id"], update_fields=["name", "is_single"],
    )
    expansion_pk = dict(Expansion.objects.filter(game=game).values_list("code", "pk"))
    category_pk = dict(Category.objects.filter(game=game).values_list("cm_id", "pk"))
    field = Blueprint._meta.get_field
    # Models are built and written one batch at a time: all of them at once do not fit in the worker.
    for chunk in batched(products.values(), BATCH):
        Blueprint.objects.bulk_create(
            [
                Blueprint(cm_product_id=p.id_product, name=_text(p.name, field("name")),
                          expansion_id=expansion_pk[p.expansion_code],
                          collector_number=_text(p.number, field("collector_number")),
                          rarity=_text(p.rarity, field("rarity")), image_url=_text(p.image_url, field("image_url")),
                          image_large_url=_text(p.image_large_url, field("image_large_url")),
                          category_id=category_pk[p.category_id])
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
    game.save(update_fields=["catalog_sha256"])
    return pruned


def _write(game, file, prices, guide_date):
    """Write game's file (None: unchanged) and its prices. Returns how many blueprints went."""
    pruned = _import(game, file) if file is not None else 0
    for chunk in batched(prices.items(), BATCH):
        CardmarketPrice.objects.bulk_create(
            [cardmarket.price_row(id_product, values, guide_date) for id_product, values in chunk],
            update_conflicts=True, unique_fields=["id_product"],
            update_fields=[*PRICE_FIELDS.values(), "prices_date"],
        )
    return pruned


def _failure(game, error, unexpected=False):
    return GameResult(game=game.slug, imported=False, expansions=0, blueprints=0, priced=0, pruned=0,
                      reconciled=catalog.ReconcileResult(), guide_date=None, error=error, unexpected=unexpected)


def _game_file(bucket, game):
    """game's file when its bytes changed (None: unchanged), checked."""
    data = bucket.fetch(game.slug)
    if game.catalog_sha256 and hashlib.sha256(data).hexdigest() == game.catalog_sha256:
        return None
    file = bucket.parse(game.slug, data)
    del data
    _check(file)
    return file


def _sync_vision(bucket, game):
    """Download game's vision index and store it when its bytes changed. A file that is missing or does not load
    leaves the previous index as it was. Returns "updated", "unchanged" or "missing"; raises on anything else."""
    try:
        data = bucket.fetch_vision(game.slug)
    except CatalogFileMissing:
        return "missing"
    digest = hashlib.sha256(data).hexdigest()
    if GameVisionIndex.objects.filter(game=game).values_list("sha256", flat=True).first() == digest:
        return "unchanged"
    index = Index.load(data)  # InvalidIndex when the bytes are not an index
    if index.header.game != game.slug:
        raise CatalogSyncError(f"index of game {index.header.game!r} in the file of {game.slug!r}")
    GameVisionIndex.objects.update_or_create(game=game, defaults={"data": data, "sha256": digest})
    return "updated"


def _vision_result(bucket, game):
    """_sync_vision, a failure of which is told in its text and never reaches the catalog."""
    try:
        return _sync_vision(bucket, game)
    except Exception as exc:
        logger.warning("Vision index of %s not updated: %s: %s", game.slug, type(exc).__name__, exc)
        return f"{type(exc).__name__}: {exc}"


def _sync_game(bucket, cm_client, game, problem=""):
    """Import game's file when it changed and its prices, then align its cards. Everything is downloaded before
    anything is written, and written in one transaction. A file that cannot be read (or the problem that kept the
    whole catalog from being read) leaves the game's catalog as it was, but a game that has one still gets its
    prices; a price guide that cannot be read leaves the game as it was."""
    try:
        file, error = None, problem
        if not error:
            try:
                file = _game_file(bucket, game)
            except (CatalogSyncError, CatalogFilesError) as exc:
                error = str(exc)
        if error and not Expansion.objects.filter(game=game).exists():
            return _failure(game, error)
        try:
            prices, guide_date = cardmarket.fetch_guide(cm_client, game.cm_id)
        except CardmarketError as exc:
            return _failure(game, f"{error}; {exc}" if error else str(exc))
        with transaction.atomic():
            imported = file is not None
            pruned = _write(game, file, prices, guide_date)
            del file
            blueprints = priced = 0
            for cm in Blueprint.objects.filter(expansion__game=game).values_list("cm_product_id", flat=True):
                blueprints += 1
                priced += cm in prices
            result = GameResult(
                game=game.slug, imported=imported, expansions=Expansion.objects.filter(game=game).count(),
                blueprints=blueprints, priced=priced, pruned=pruned, reconciled=catalog.reconcile_cards(game),
                guide_date=guide_date, error=error,
            )
        # After the catalog's transaction, so that a vision failure never rolls it back.
        if bucket is not None:
            result.vision = _vision_result(bucket, game)
        return result
    except Exception as exc:
        # A bug or a database failure: this game is rolled back, the others still run.
        logger.exception("Catalog sync of %s failed", game.slug)
        return _failure(game, f"{type(exc).__name__}: {exc}", unexpected=True)


def sync(bucket, cm_client, problem="", on_result=None):
    """Sync every game, in slug order; a game that fails is told in its result and does not stop the others.
    on_result(game, result), if given, is called as soon as each game is done.

    bucket None (or a problem) means the catalog files cannot be read: only the prices of the games that have a
    catalog are updated, and every result carries the problem."""
    if not problem and bucket is None:
        problem = "no catalog bucket"
    results = []
    for game in Game.objects.order_by("slug"):
        result = _sync_game(bucket, cm_client, game, problem)
        if on_result is not None:
            on_result(game, result)
        results.append(result)
    return results


def _summary(name, result):
    if result.error and result.guide_date is None:
        return f"{name} catalog sync failed: {result.error}"
    r = result.reconciled
    counts = (
        f"{result.blueprints} printings, {result.priced} priced (guide {result.guide_date:%Y-%m-%d}), "
        f"{r.linked} cards linked, {r.unlinked} not linked"
    )
    if result.error:
        message = f"{name} catalog not updated ({result.error}), prices updated: {counts}"
    else:
        message = f"{name} catalog: {result.expansions} expansions, {counts}"
        if not result.imported:
            message += ", file unchanged"
    if result.vision and result.vision != "unchanged":
        message += f", vision index {result.vision}"
    if result.pruned:
        message += f", {result.pruned} printings removed"
    if r.clashes:
        message += f", {r.clashes} duplicates to merge by hand"
    return message


def log_report(message, warning):
    """The default report of run_sync: the Python log."""
    (logger.warning if warning else logger.info)("%s", message)


def run_sync(bucket=None, cm_client=None, report=log_report):
    """sync() with its outcome told to report(message, warning), one line per game as soon as that game is done,
    not to the SyncLog. Returns the results.

    Without a usable bucket the prices are still updated. Raises CatalogSyncError, after reporting every game, when
    a game failed unexpectedly, so the command fails."""
    problem = ""
    try:
        bucket = bucket if bucket is not None else CatalogBucket()
    except (CatalogFilesError, BotoCoreError, ValueError) as exc:  # not configured, no region, bad endpoint
        bucket, problem = None, str(exc)
    own_cm = cm_client is None
    cm_client = cm_client or cardmarket.make_client()

    def report_game(game, result):
        warning = bool(result.error or result.reconciled.clashes or result.vision not in VISION_OK)
        report(_summary(game.name, result), warning)

    try:
        results = sync(bucket, cm_client, problem, on_result=report_game)
    except Exception as exc:
        # Unexpected (database, bug): reported, and the command fails.
        report(f"Catalog sync failed: {type(exc).__name__}: {exc}", True)
        raise
    finally:
        if own_cm:
            cm_client.close()
    failed = [r.game for r in results if r.unexpected]
    if failed:
        raise CatalogSyncError(f"unexpected failure in: {', '.join(failed)}")
    return results
