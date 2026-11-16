import copy
import gzip
import hashlib
import io
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from io import StringIO
from unittest import mock

import httpx
from botocore.exceptions import NoRegionError
from django.core.management import call_command
from django.test import TestCase, override_settings

from . import cardmarket, catalog, catalog_files, catalog_sync
from .catalog_files import MANIFEST_KEY, CatalogBucket
from .models import (
    Blueprint, Card, CardmarketPrice, Category, CollectionItem, Expansion, Game, Listing, ScanLine, ScanSession,
    SyncLog,
)
from .tests import LedgerTestCase, game

BUCKET = "test-catalog"
TOKEN, UNMATCHED = 501250, 501203
GENERATED_AT = "2026-11-16T18:25:56.894205+00:00"


def expansion(code, name, released_at, language=""):
    return {"type": "expansion", "code": code, "name": name, "released_at": released_at, "language": language}


def product(id_product, name, code, number, category, category_name, rarity="", image=""):
    return {"type": "product", "id_product": id_product, "name": name, "expansion_code": code, "number": number,
            "rarity": rarity, "image_url": f"https://img/{image}-small.jpg" if image else "",
            "image_large_url": f"https://img/{image}-normal.jpg" if image else "",
            "category_id": category, "category_name": category_name}


MAGIC = [
    expansion("dmr", "Dominaria Remastered", "2023-01-13"),
    expansion("tdmr", "Dominaria Remastered Tokens", "2023-01-13"),
    expansion("dmu", "Dominaria United", "2022-09-09"),
    product(501201, "Counterspell", "dmr", "45", 1, "Magic Single", "uncommon", "counterspell"),
    product(501204, "Ponder", "dmr", "46", 1, "Magic Single", "common", "ponder"),
    product(TOKEN, "Soldier Token", "tdmr", "1", 1, "Magic Single", "common", "token"),
    product(UNMATCHED, "Brainstorm", "dmr", "", 1, "Magic Single"),  # a single without catalog data
    product(501301, "Sheoldred, the Apocalypse", "dmu", "107", 1, "Magic Single", "mythic", "sheoldred"),
    product(501390, "Dominaria United Draft Booster Box", "dmu", "", 5, "Magic Display"),
    product(501391, "Dominaria United Playmat", "dmu", "", 13, "Magic Miscellaneous"),
]
POKEMON = [
    expansion("sv1", "Scarlet & Violet", "2023-03-31"),
    expansion("sv1-ja", "Scarlet ex", "2023-01-20", "ja"),
    product(700001, "Pikachu", "sv1", "25", 51, "Pokémon Single", "Common", "pikachu"),
    product(700002, "Scarlet ex Booster", "sv1-ja", "", 52, "Pokémon Booster"),
]
MAGIC_GUIDE = {"version": 1, "createdAt": "2026-11-14T02:45:31+0100", "priceGuides": [
    {"idProduct": 501201, "trend": 1.2, "trend-foil": 3.5},
    {"idProduct": 501390, "trend": 150.0},
    {"idProduct": 999999, "trend": 9.0},  # a product the catalog does not hold
]}
POKEMON_GUIDE = {"version": 1, "createdAt": "2026-11-14T02:50:00+0100", "priceGuides": [
    {"idProduct": 700001, "trend": 2.5, "avg": 2.0, "trend-holo": 9.5, "avg-holo": 8.0, "low-holo": 6.0},
]}


def jsonl_gz(lines):
    return gzip.compress("".join(json.dumps(x) + "\n" for x in lines).encode(), mtime=0)


class FakeBucket:
    """A boto3 S3 client holding the generator's v1 files: a manifest and one gzipped JSON lines file per game."""

    def __init__(self):
        self.games = {"mtg": copy.deepcopy(MAGIC), "pokemon": copy.deepcopy(POKEMON)}
        self.corrupt = set()  # slugs whose file is not gzip (its checksum still matches)
        self.manifest = None  # raw manifest replacing the generated one
        self.reads = []

    def _file(self, slug):
        return b"not a gzip file" if slug in self.corrupt else jsonl_gz(self.games[slug])

    def _manifest(self):
        if self.manifest is not None:
            return json.dumps(self.manifest).encode()
        return json.dumps({"format_version": 1, "games": {
            slug: {"key": f"catalog/v1/{slug}.jsonl.gz", "sha256": hashlib.sha256(self._file(slug)).hexdigest(),
                   "generated_at": GENERATED_AT}
            for slug in self.games
        }}).encode()

    def get_object(self, Bucket, Key):
        assert Bucket == BUCKET
        self.reads.append(Key)
        if Key == MANIFEST_KEY:
            data = self._manifest()
        else:
            data = self._file(Key.removeprefix("catalog/v1/").removesuffix(".jsonl.gz"))
        return {"Body": io.BytesIO(data)}

    def sha256(self, slug):
        return hashlib.sha256(self._file(slug)).hexdigest()


class FakeCardmarket:
    """Cardmarket's price guide files, one per game, served through httpx.MockTransport."""

    def __init__(self):
        self.guides = {1: copy.deepcopy(MAGIC_GUIDE), 6: copy.deepcopy(POKEMON_GUIDE)}
        self.fail = set()  # Cardmarket game ids whose guide answers 500
        self.urls = []

    def handler(self, request):
        url = str(request.url)
        self.urls.append(url)
        for cm_game_id, guide in self.guides.items():
            if url == cardmarket.price_guide_url(cm_game_id):
                if cm_game_id in self.fail:
                    return httpx.Response(500, text="boom")
                return httpx.Response(200, json=guide)
        return httpx.Response(404)

    def client(self):
        return cardmarket.make_client(transport=httpx.MockTransport(self.handler))


@override_settings(CARDLEDGER_CATALOG_BUCKET=BUCKET)
class CatalogSyncTestCase(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.files = FakeBucket()
        self.cm = FakeCardmarket()

    def bucket(self):
        return CatalogBucket(client=self.files)

    def sync(self):
        return {r.game: r for r in catalog_sync.sync(self.bucket(), self.cm.client())}

    def run_sync(self):
        return catalog_sync.run_sync(self.bucket(), self.cm.client())


class SyncTest(CatalogSyncTestCase):
    def test_sync_imports_magic_file(self):
        results = self.sync()
        mtg = results["mtg"]
        self.assertEqual((mtg.imported, mtg.error, mtg.expansions, mtg.blueprints, mtg.priced),
                         (True, "", 3, 7, 2))
        bp = Blueprint.objects.get(cm_product_id=501201)
        self.assertEqual((bp.name, bp.expansion.code, bp.expansion.game.slug, bp.collector_number, bp.rarity,
                          bp.category.cm_id, bp.is_product),
                         ("Counterspell", "dmr", "mtg", "45", "uncommon", 1, False))
        self.assertEqual((bp.image_url, bp.image_large_url),
                         ("https://img/counterspell-small.jpg", "https://img/counterspell-normal.jpg"))
        box = Blueprint.objects.get(cm_product_id=501390)
        self.assertEqual((box.expansion.code, box.collector_number, box.is_product), ("dmu", "", True))
        dmr = Expansion.objects.get(game=game(), code="dmr")
        self.assertEqual((dmr.name, dmr.released_at, dmr.language), ("Dominaria Remastered", date(2023, 1, 13), ""))
        self.assertEqual(Category.objects.get(game=game(), cm_id=5).name, "Magic Display")
        magic = game()
        self.assertEqual(magic.catalog_sha256, self.files.sha256("mtg"))
        self.assertEqual(magic.catalog_generated_at, datetime(2026, 11, 16, 18, 25, 56, 894205, tzinfo=timezone.utc))
        self.assertEqual(self.ct.calls, [])  # CardTrader is never contacted

    def test_each_game_gets_its_own_catalog(self):
        self.sync()
        pikachu = Blueprint.objects.get(cm_product_id=700001)
        self.assertEqual((pikachu.expansion.game.slug, pikachu.expansion.code, pikachu.collector_number),
                         ("pokemon", "sv1", "25"))
        self.assertEqual(Expansion.objects.get(game=game("pokemon"), code="sv1-ja").language, "ja")
        self.assertFalse(Expansion.objects.filter(game=game(), code="sv1").exists())

    def test_unchanged_file_is_not_reimported(self):
        self.sync()
        Blueprint.objects.filter(cm_product_id=501201).update(name="Edited")
        self.cm.guides[1]["priceGuides"][0]["trend"] = 2.5
        self.files.reads.clear()
        results = self.sync()
        self.assertFalse(results["mtg"].imported)
        self.assertEqual(Blueprint.objects.get(cm_product_id=501201).name, "Edited")
        self.assertEqual(self.files.reads, [MANIFEST_KEY])  # no game file is downloaded again
        self.assertEqual(CardmarketPrice.objects.get(pk=501201).trend, Decimal("2.50"))  # prices still are
        self.assertEqual((results["mtg"].expansions, results["mtg"].blueprints, results["mtg"].priced), (3, 7, 2))

    def test_changed_file_is_reimported(self):
        self.sync()
        self.files.games["mtg"][3]["name"] = "Counterspell (Retro)"
        results = self.sync()
        self.assertTrue(results["mtg"].imported)
        self.assertFalse(results["pokemon"].imported)
        self.assertEqual(Blueprint.objects.get(cm_product_id=501201).name, "Counterspell (Retro)")
        self.assertEqual(Blueprint.objects.count(), 9)

    def test_prices_are_downloaded_for_every_game(self):
        self.sync()
        self.assertEqual(sorted(self.cm.urls), sorted([cardmarket.price_guide_url(1), cardmarket.price_guide_url(6)]))
        self.assertEqual(CardmarketPrice.objects.get(pk=501201).trend_foil, Decimal("3.50"))
        self.assertEqual(CardmarketPrice.objects.get(pk=700001).trend, Decimal("2.50"))
        self.assertEqual(CardmarketPrice.objects.get(pk=999999).trend, Decimal("9.00"))  # not in the catalog
        self.assertEqual(CardmarketPrice.objects.get(pk=501201).prices_date.isoformat(), "2026-11-14T01:45:31+00:00")

    def test_holo_prices_fill_foil_fields(self):
        results = self.sync()
        price = CardmarketPrice.objects.get(pk=700001)
        self.assertEqual((price.trend_foil, price.avg_foil, price.low_foil, price.avg),
                         (Decimal("9.50"), Decimal("8.00"), Decimal("6.00"), Decimal("2.00")))
        self.assertEqual(results["pokemon"].priced, 1)

    def test_category_is_single_from_name(self):
        for name, single in [("Magic Single", True), ("Pokémon Single", True), ("YUGIOH SINGLES", True),
                             ("Magic Display", False), ("Singles Lot", False), ("Pokémon Booster", False)]:
            with self.subTest(name=name):
                self.assertIs(catalog_sync.is_single_category(name), single)
        self.sync()
        self.assertTrue(Category.objects.get(game=game("pokemon"), cm_id=51).is_single)
        self.assertFalse(Category.objects.get(game=game("pokemon"), cm_id=52).is_single)
        self.assertTrue(Blueprint.objects.get(cm_product_id=700002).is_product)
        self.assertTrue(Category.objects.get(game=game(), cm_id=1).is_single)

    def test_sync_twice_no_duplicates_prices_overwritten(self):
        self.sync()
        self.cm.guides[1]["priceGuides"][0]["trend"] = 2.5
        self.sync()
        self.assertEqual(Blueprint.objects.count(), 9)
        self.assertEqual(CardmarketPrice.objects.get(pk=501201).trend, Decimal("2.50"))

    def test_sync_written_in_batches(self):
        with mock.patch.object(catalog_sync, "BATCH", 2):
            self.sync()
        self.assertEqual(Blueprint.objects.count(), 9)
        self.assertEqual(CardmarketPrice.objects.count(), 4)

    def test_corrupt_game_file_skips_only_that_game(self):
        self.files.corrupt.add("pokemon")
        results = self.sync()
        self.assertIn("Invalid catalog file", results["pokemon"].error)
        self.assertFalse(results["pokemon"].imported)
        self.assertFalse(Blueprint.objects.filter(expansion__game=game("pokemon")).exists())
        self.assertEqual(game("pokemon").catalog_sha256, "")
        self.assertFalse(CardmarketPrice.objects.filter(pk=700001).exists())
        self.assertEqual((results["mtg"].error, Blueprint.objects.filter(expansion__game=game()).count()), ("", 7))

    def test_price_guide_failure_leaves_that_game_unchanged(self):
        self.cm.fail.add(6)
        results = self.sync()
        self.assertIn("Cardmarket price guide", results["pokemon"].error)
        self.assertFalse(Blueprint.objects.filter(expansion__game=game("pokemon")).exists())
        self.assertEqual(game("pokemon").catalog_sha256, "")  # imported on the next run
        self.assertEqual(results["mtg"].error, "")

    def test_product_of_an_unknown_expansion_skips_that_game(self):
        self.files.games["pokemon"].append(product(700003, "Eevee", "nope", "1", 51, "Pokémon Single"))
        results = self.sync()
        self.assertIn("nope", results["pokemon"].error)
        self.assertFalse(Blueprint.objects.filter(expansion__game=game("pokemon")).exists())

    def test_game_missing_from_the_manifest_is_an_error(self):
        results = self.sync()
        self.assertIn("not in the catalog manifest", results["lorcana"].error)
        self.assertEqual(list(results), sorted(Game.objects.values_list("slug", flat=True)))


class PruneTest(CatalogSyncTestCase):
    def old_blueprint(self, cm, slug="mtg", **fields):
        old = Expansion.objects.get_or_create(game=game(slug), code="ctold", defaults={"name": "Old set"})[0]
        return Blueprint.objects.create(cm_product_id=cm, expansion=old, name="Old lot", **fields)

    def test_blueprints_no_longer_in_the_file_are_removed(self):
        self.old_blueprint(888001)
        results = self.sync()
        self.assertEqual(results["mtg"].pruned, 1)
        self.assertFalse(Blueprint.objects.filter(cm_product_id=888001).exists())
        self.assertFalse(Expansion.objects.filter(code="ctold").exists())

    def test_prune_never_touches_another_game(self):
        self.old_blueprint(888004, slug="yugioh")  # Yu-Gi-Oh! is not in the manifest
        self.sync()
        self.assertTrue(Blueprint.objects.filter(cm_product_id=888004).exists())

    def test_blueprint_of_an_open_scan_line_is_kept(self):
        bp = self.old_blueprint(888002)
        session = ScanSession.objects.create(owner=self.user)
        ScanLine.objects.create(session=session, blueprint=bp, condition="NM", language="EN")
        self.sync()
        self.assertTrue(Blueprint.objects.filter(cm_product_id=888002).exists())

    def test_saved_scan_line_keeps_its_history_without_the_blueprint(self):
        bp = self.old_blueprint(888003)
        session = ScanSession.objects.create(owner=self.user, status=ScanSession.Status.SAVED)
        line = ScanLine.objects.create(session=session, blueprint=bp, condition="NM", language="EN")
        self.sync()
        line.refresh_from_db()
        self.assertIsNone(line.blueprint)

    def test_suspiciously_small_file_prunes_nothing(self):
        for i in range(20):
            self.old_blueprint(889000 + i, slug="pokemon")
        self.old_blueprint(888005)
        results = self.sync()
        # Pokémon's file holds 2 printings out of 22: nothing goes. Magic's share is its own: 7 out of 8.
        self.assertEqual(Blueprint.objects.filter(cm_product_id__gte=889000).count(), 20)
        self.assertEqual(results["pokemon"].pruned, 0)
        self.assertFalse(Blueprint.objects.filter(cm_product_id=888005).exists())

    def test_empty_expansions_are_removed_only_in_that_game(self):
        Expansion.objects.create(game=game(), code="old", name="Old Magic set")
        Category.objects.create(game=game(), cm_id=99, name="Old Magic category")
        Expansion.objects.create(game=game("yugioh"), code="old", name="Old Yu-Gi-Oh! set")
        Category.objects.create(game=game("yugioh"), cm_id=99, name="Old Yu-Gi-Oh! category")
        self.sync()
        self.assertFalse(Expansion.objects.filter(game=game(), code="old").exists())
        self.assertFalse(Category.objects.filter(game=game(), cm_id=99).exists())
        self.assertTrue(Expansion.objects.filter(game=game("yugioh"), code="old").exists())
        self.assertTrue(Category.objects.filter(game=game("yugioh"), cm_id=99).exists())


class RunSyncTest(CatalogSyncTestCase):
    def test_run_sync_logs_one_line_per_game(self):
        Card.objects.create(game=game(), name="Nowhere", set_code="ZZZ", collector_number="1")
        before = SyncLog.objects.count()
        results = self.run_sync()
        self.assertEqual([r.game for r in results], sorted(Game.objects.values_list("slug", flat=True)))
        logs = {log.message.split(" catalog")[0]: log for log in SyncLog.objects.order_by("pk")[before:]}
        self.assertEqual(len(logs), Game.objects.count())
        magic = logs["Magic: The Gathering"]
        self.assertEqual(magic.level, SyncLog.Level.OK)
        self.assertTrue(magic.message.startswith(
            "Magic: The Gathering catalog: 3 expansions, 7 printings, 2 priced (guide 2026-11-14)"), magic.message)
        self.assertIn("cards linked", magic.message)
        self.assertEqual(logs["Pokémon"].level, SyncLog.Level.OK)
        self.assertEqual(logs["Disney Lorcana"].level, SyncLog.Level.WARNING)
        self.assertIn("not in the catalog manifest", logs["Disney Lorcana"].message)

    def test_unchanged_file_is_told_in_the_log(self):
        self.run_sync()
        self.run_sync()
        message = SyncLog.objects.filter(message__startswith="Magic: The Gathering").latest("pk").message
        self.assertIn("file unchanged", message)

    def test_missing_bucket_logs_and_changes_nothing(self):
        with override_settings(CARDLEDGER_CATALOG_BUCKET=""):
            self.assertIsNone(catalog_sync.run_sync(cm_client=self.cm.client()))
        log = SyncLog.objects.latest("pk")
        self.assertEqual(log.level, SyncLog.Level.WARNING)
        self.assertEqual(log.message, "Catalog sync failed: CARDLEDGER_CATALOG_BUCKET is not set")
        self.assertFalse(Blueprint.objects.exists())
        self.assertEqual(self.cm.urls, [])

    def test_unusable_bucket_client_logs_and_changes_nothing(self):
        for error in (NoRegionError(), ValueError("Invalid endpoint: nope")):
            with self.subTest(error=error), \
                    mock.patch.object(catalog_files.boto3, "client", side_effect=error):
                self.assertIsNone(catalog_sync.run_sync(cm_client=self.cm.client()))
                log = SyncLog.objects.latest("pk")
                self.assertEqual(log.level, SyncLog.Level.WARNING)
                self.assertTrue(log.message.startswith("Catalog sync failed: "), log.message)
        self.assertFalse(Blueprint.objects.exists())

    def test_unreadable_manifest_logs_and_changes_nothing(self):
        self.files.manifest = {"format_version": 2, "games": {}}
        self.assertIsNone(self.run_sync())
        self.assertIn("Unsupported catalog format version", SyncLog.objects.latest("pk").message)
        self.assertFalse(Blueprint.objects.exists())

    def test_manifest_without_games_logs_and_changes_nothing(self):
        for games in (None, [], "mtg"):
            with self.subTest(games=games):
                self.files.manifest = {"format_version": 1} if games is None else {"format_version": 1,
                                                                                   "games": games}
                self.assertIsNone(self.run_sync())
                log = SyncLog.objects.latest("pk")
                self.assertEqual((log.level, log.message),
                                 (SyncLog.Level.WARNING, "Catalog sync failed: the catalog manifest has no games"))
        self.assertFalse(Blueprint.objects.exists())

    def test_unexpected_failure_is_logged_and_raised(self):
        with mock.patch.object(catalog_sync, "_write", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                self.run_sync()
        self.assertIn("RuntimeError: db down", SyncLog.objects.latest("pk").message)


class ReconcileTest(CatalogSyncTestCase):
    def setUp(self):
        super().setUp()
        self.sync()

    def stack(self, card, quantity=1, listing=None, owner=None):
        item = CollectionItem.objects.create(owner=owner or self.user, card=card, condition="NM", language="EN",
                                             quantity=quantity)
        if listing:
            Listing.objects.create(item=item, quantity=1, price=Decimal("1.00"), status=listing)
        return item

    def test_sync_reconciles_the_cards(self):
        card = Card.objects.create(game=game(), name="Counterspell", set_code="DMR", collector_number="045")
        results = self.sync()
        card.refresh_from_db()
        self.assertEqual((card.cm_product_id, card.collector_number), (501201, "45"))
        self.assertEqual(results["mtg"].reconciled.linked, 1)

    def test_existing_card_rewritten_to_catalog_code(self):
        mat = Card.objects.create(game=game(), name="Dominaria United Playmat", set_code="DMU",
                                  collector_number="CT1391", cm_product_id=501391)
        token = Card.objects.create(game=game(), name="Soldier Token", set_code="DMR", collector_number="CT9999",
                                    cm_product_id=TOKEN)
        catalog.reconcile_cards()
        mat.refresh_from_db()
        token.refresh_from_db()
        self.assertEqual((mat.set_code, mat.collector_number, mat.is_product), ("DMU", "CM501391", True))
        self.assertEqual((token.set_code, token.collector_number), ("TDMR", "1"))

    def test_rewrite_collision_merges_cards(self):
        old = Card.objects.create(game=game(), name="Counterspell", set_code="DMR", collector_number="CT1201",
                                  cm_product_id=501201)
        csv = Card.objects.create(game=game(), name="Counterspell", set_code="DMR", collector_number="45")
        self.stack(old, 2)
        self.stack(csv, 3)
        result = catalog.reconcile_cards()
        self.assertEqual(result.merged, 1)
        [card] = Card.objects.filter(name="Counterspell", collector_number="45")
        self.assertEqual(card.cm_product_id, 501201)
        self.assertEqual(card.items.get(owner=self.user).quantity, 5)
        self.assertFalse(Card.objects.filter(collector_number="CT1201").exists())

    def test_duplicate_product_with_live_listings_warns_owner(self):
        first = Card.objects.create(game=game(), name="Counterspell", set_code="DMR", collector_number="45",
                                    cm_product_id=501201)
        second = Card.objects.create(game=game(), name="Counterspell", set_code="DMR", collector_number="CT1299",
                                     cm_product_id=501201)
        self.stack(first, listing=Listing.Status.PUBLISHED)
        self.stack(second, listing=Listing.Status.TO_PUBLISH)
        result = catalog.reconcile_cards()
        self.assertEqual(result.clashes, 1)
        second.refresh_from_db()
        self.assertIsNone(second.cm_product_id)
        warning = SyncLog.objects.filter(owner=self.user, level=SyncLog.Level.WARNING).latest("pk")
        self.assertIn("both have live listings", warning.message)

    def test_duplicate_product_without_listings_is_merged(self):
        first = Card.objects.create(game=game(), name="Counterspell", set_code="DMR", collector_number="45",
                                    cm_product_id=501201)
        second = Card.objects.create(game=game(), name="Counterspell", set_code="DMR", collector_number="CT1299",
                                     cm_product_id=501201)
        self.stack(second, 2)
        catalog.reconcile_cards()
        self.assertFalse(Card.objects.filter(pk=second.pk).exists())
        self.assertEqual(first.items.get().quantity, 2)

    def test_unlinked_card_linked_by_set_and_number(self):
        card = Card.objects.create(game=game(), name="Counterspell", set_code="DMR", collector_number="045")
        result = catalog.reconcile_cards()
        card.refresh_from_db()
        self.assertEqual((card.cm_product_id, card.collector_number), (501201, "45"))
        self.assertGreaterEqual(result.linked, 1)

    def test_single_without_number_keeps_its_own(self):
        card = Card.objects.create(game=game(), name="Brainstorm", set_code="2XM", collector_number="129",
                                   cm_product_id=UNMATCHED)
        catalog.reconcile_cards()
        card.refresh_from_db()
        self.assertEqual((card.set_code, card.collector_number), ("2XM", "129"))

    def test_duplicate_product_with_different_numbers_is_unlinked_not_merged(self):
        base = Card.objects.create(game=game(), name="Counterspell", set_code="DMR", collector_number="45",
                                   cm_product_id=501201)
        showcase = Card.objects.create(game=game(), name="Counterspell", set_code="DMR", collector_number="300",
                                       cm_product_id=501201)
        self.stack(showcase, 2)
        catalog.reconcile_cards()
        showcase.refresh_from_db()
        self.assertIsNone(showcase.cm_product_id)
        self.assertEqual(showcase.items.get().quantity, 2)
        self.assertTrue(Card.objects.filter(pk=base.pk, cm_product_id=501201).exists())

    def test_card_with_unknown_product_stays_as_is(self):
        card = Card.objects.create(game=game(), name="Gone", set_code="OLD", collector_number="CT1",
                                   cm_product_id=424242)
        result = catalog.reconcile_cards()
        card.refresh_from_db()
        self.assertEqual((card.set_code, card.collector_number), ("OLD", "CT1"))
        self.assertGreaterEqual(result.unlinked, 1)


class SyncCommandTest(TestCase):
    def test_command_only_enqueues(self):
        from . import tasks

        with mock.patch.object(tasks.sync_catalog, "delay", return_value=mock.Mock(id="abc")) as delay, \
                mock.patch.object(catalog_sync, "run_sync") as run:
            out = StringIO()
            call_command("sync_catalog", stdout=out)
        delay.assert_called_once_with()
        run.assert_not_called()
        self.assertIn("Catalog sync queued (task abc)", out.getvalue())

    def test_task_runs_sync(self):
        from . import tasks

        with mock.patch.object(catalog_sync, "run_sync") as run:
            tasks.sync_catalog.apply()
        run.assert_called_once_with()

    def test_no_beat_schedule(self):
        from config.celery import app

        self.assertFalse(app.conf.beat_schedule)
