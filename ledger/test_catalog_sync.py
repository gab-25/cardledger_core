import copy
import hashlib
import io
import json
from datetime import date
from decimal import Decimal
from io import StringIO
from unittest import mock

import httpx
from botocore.exceptions import ClientError, NoRegionError
from django.core.management import call_command
from django.test import TestCase, override_settings

from . import cardmarket, catalog, catalog_files, catalog_sync
from .catalog_files import CatalogBucket, catalog_key
from .models import (
    Blueprint, Card, CardmarketPrice, Category, CollectionItem, Expansion, Game, Listing, ScanLine, ScanSession,
    SyncLog,
)
from .tests import LedgerTestCase, game, load_catalog

BUCKET = "test-catalog"
TOKEN, UNMATCHED = 501250, 501203


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
LORCANA = [
    expansion("tfc", "The First Chapter", "2023-08-18"),
    product(800001, "Mickey Mouse", "tfc", "1", 1601, "Lorcana Single", "Rare", "mickey"),
]
LORCANA_GUIDE = {"version": 1, "createdAt": "2026-11-14T02:55:00+0100", "priceGuides": [
    {"idProduct": 800001, "trend": 4.0},
]}
POKEMON_GUIDE = {"version": 1, "createdAt": "2026-11-14T02:50:00+0100", "priceGuides": [
    {"idProduct": 700001, "trend": 2.5, "avg": 2.0, "trend-holo": 9.5, "avg-holo": 8.0, "low-holo": 6.0},
]}


def catalog_json(rows):
    """A game file as the generator writes it: one JSON object with the expansions and the products (the rows built
    above carry a "type" only to tell them apart here)."""
    doc = {
        "expansions": [{k: v for k, v in r.items() if k != "type"} for r in rows if r["type"] == "expansion"],
        "products": [{k: v for k, v in r.items() if k != "type"} for r in rows if r["type"] == "product"],
    }
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


class FakeBucket:
    """A boto3 S3 client holding the generator's v1 files: one JSON document per game, no manifest."""

    def __init__(self):
        self.games = {"mtg": copy.deepcopy(MAGIC), "pokemon": copy.deepcopy(POKEMON)}
        self.corrupt = set()  # slugs whose file is not valid JSON
        self.denied = set()  # slugs whose file cannot be read (access denied)
        self.empty = set()  # slugs whose file holds no expansions and no products
        self.reads = []

    def _file(self, slug):
        if slug in self.empty:
            return catalog_json([])
        return b"{not json" if slug in self.corrupt else catalog_json(self.games[slug])

    def get_object(self, Bucket, Key):
        assert Bucket == BUCKET
        self.reads.append(Key)
        slug = Key.removeprefix("catalog/v1/").removesuffix(".json")
        if slug in self.denied:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}, "GetObject")
        if slug not in self.games:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "Not Found"}}, "GetObject")
        return {"Body": io.BytesIO(self._file(slug))}

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
        with mock.patch.object(CatalogBucket, "parse", side_effect=AssertionError("parsed")):
            results = self.sync()  # the same bytes: not even parsed
        self.assertFalse(results["mtg"].imported)
        self.assertEqual(results["mtg"].error, "")
        self.assertEqual(Blueprint.objects.get(cm_product_id=501201).name, "Edited")
        # Every game's file is downloaded, in slug order, to compare its sha256.
        self.assertEqual(self.files.reads, [catalog_key(g) for g in Game.objects.order_by("slug").values_list(
            "slug", flat=True)])
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

    def test_game_file_missing_is_an_error(self):
        results = self.sync()
        self.assertIn("No catalog file catalog/v1/lorcana.json", results["lorcana"].error)
        self.assertFalse(results["lorcana"].unexpected)
        self.assertEqual(list(results), sorted(Game.objects.values_list("slug", flat=True)))

    def with_lorcana(self):
        self.files.games["lorcana"] = copy.deepcopy(LORCANA)
        self.cm.guides[game("lorcana").cm_id] = copy.deepcopy(LORCANA_GUIDE)

    def test_unexpected_error_in_one_game_does_not_stop_the_others(self):
        self.with_lorcana()
        real_import = catalog_sync._import

        def failing_import(g, file):
            real_import(g, file)  # written, then rolled back
            if g.slug == "lorcana":
                raise RuntimeError("boom")
            return 0

        with mock.patch.object(catalog_sync, "_import", side_effect=failing_import):
            results = self.sync()
        self.assertEqual(results["lorcana"].error, "RuntimeError: boom")
        self.assertTrue(results["lorcana"].unexpected)
        self.assertFalse(Expansion.objects.filter(game=game("lorcana")).exists())
        self.assertFalse(CardmarketPrice.objects.filter(pk=800001).exists())
        self.assertEqual(game("lorcana").catalog_sha256, "")
        # Magic and Pokémon come after Lorcana in slug order: still imported and priced.
        self.assertEqual((results["mtg"].error, results["mtg"].imported, results["mtg"].priced), ("", True, 2))
        self.assertEqual(CardmarketPrice.objects.get(pk=501201).trend, Decimal("1.20"))
        self.assertEqual(results["pokemon"].error, "")

    def test_missing_and_oversize_fields_are_written(self):
        self.files.games["mtg"][3].update(name=None, number=None, rarity=None, image_url="https://img/" + "x" * 600,
                                          image_large_url=None, category_name=None)
        self.files.games["mtg"][0].update(name=None, language=None)
        results = self.sync()
        self.assertEqual(results["mtg"].error, "")
        bp = Blueprint.objects.get(cm_product_id=501201)
        self.assertEqual((bp.name, bp.collector_number, bp.rarity, bp.image_large_url), ("", "", "", ""))
        self.assertEqual(len(bp.image_url), 500)
        self.assertEqual(Expansion.objects.get(game=game(), code="dmr").name, "")

    def test_empty_file_is_refused_and_retried(self):
        self.sync()
        sha = game("pokemon").catalog_sha256
        self.files.empty.add("pokemon")
        self.cm.guides[6]["priceGuides"][0]["trend"] = 3.0
        results = self.sync()
        self.assertIn("empty catalog file", results["pokemon"].error)
        self.assertEqual(game("pokemon").catalog_sha256, sha)
        self.assertTrue(Blueprint.objects.filter(cm_product_id=700001).exists())
        self.assertEqual(CardmarketPrice.objects.get(pk=700001).trend, Decimal("3.00"))
        self.files.empty.discard("pokemon")
        self.files.games["pokemon"][2]["name"] = "Pikachu (new)"
        self.assertEqual(self.sync()["pokemon"].error, "")
        self.assertEqual(Blueprint.objects.get(cm_product_id=700001).name, "Pikachu (new)")

    def test_long_set_codes_are_imported(self):
        # The generator builds unique codes like "jump-shonen-jump-may-2010-subscription-b" (up to 40 characters).
        code = "jump-shonen-jump-may-2010-subscription-b"
        self.files.games["pokemon"].append(expansion(code, "Shonen Jump promos", None))
        self.files.games["pokemon"].append(product(700099, "Promo", code, "1", 51, "Pokémon Single"))
        results = self.sync()
        self.assertEqual(results["pokemon"].error, "")
        self.assertEqual(Blueprint.objects.get(cm_product_id=700099).expansion.code, code)

    def test_set_codes_over_40_characters_skip_that_game(self):
        self.files.games["pokemon"].append(expansion("x" * 41, "Too long", None))
        results = self.sync()
        self.assertIn("too long", results["pokemon"].error)

    def test_expansion_without_code_skips_that_game(self):
        self.files.games["pokemon"].append(expansion(None, "No code", None))
        results = self.sync()
        self.assertIn("expansions without a set code", results["pokemon"].error)
        self.assertEqual(results["mtg"].error, "")

    def test_corrupt_file_of_an_existing_catalog_still_updates_its_prices(self):
        self.sync()
        sha = game("pokemon").catalog_sha256
        self.files.games["pokemon"][2]["name"] = "Pikachu (new)"
        self.files.corrupt.add("pokemon")
        self.cm.guides[6]["priceGuides"][0]["trend"] = 3.0
        results = self.sync()
        pokemon = results["pokemon"]
        self.assertIn("Invalid catalog file", pokemon.error)
        self.assertEqual((pokemon.imported, pokemon.blueprints, pokemon.priced), (False, 2, 1))
        self.assertEqual(CardmarketPrice.objects.get(pk=700001).trend, Decimal("3.00"))
        self.assertEqual(Blueprint.objects.get(cm_product_id=700001).name, "Pikachu")
        self.assertEqual(game("pokemon").catalog_sha256, sha)

    def test_missing_game_file_keeps_catalog_and_updates_prices(self):
        self.sync()
        sha = game("pokemon").catalog_sha256
        del self.files.games["pokemon"]
        self.cm.guides[6]["priceGuides"][0]["trend"] = 3.0
        results = self.sync()
        pokemon = results["pokemon"]
        self.assertIn("No catalog file", pokemon.error)
        self.assertEqual((pokemon.imported, pokemon.blueprints, pokemon.priced), (False, 2, 1))
        self.assertEqual(CardmarketPrice.objects.get(pk=700001).trend, Decimal("3.00"))
        self.assertEqual(Blueprint.objects.filter(expansion__game=game("pokemon")).count(), 2)
        self.assertEqual(game("pokemon").catalog_sha256, sha)
        self.assertEqual(results["mtg"].error, "")


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
        self.old_blueprint(888004, slug="yugioh")  # Yu-Gi-Oh! has no file
        self.sync()
        self.assertTrue(Blueprint.objects.filter(cm_product_id=888004).exists())

    def test_blueprint_of_an_open_scan_line_is_kept(self):
        bp = self.old_blueprint(888002)
        session = ScanSession.objects.create(owner=self.user, game=game())
        ScanLine.objects.create(session=session, blueprint=bp, condition="NM", language="EN")
        self.sync()
        self.assertTrue(Blueprint.objects.filter(cm_product_id=888002).exists())

    def test_saved_scan_line_keeps_its_history_without_the_blueprint(self):
        bp = self.old_blueprint(888003)
        session = ScanSession.objects.create(owner=self.user, game=game(), status=ScanSession.Status.SAVED)
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
        self.assertIn("No catalog file", logs["Disney Lorcana"].message)

    def test_unchanged_file_is_told_in_the_log(self):
        self.run_sync()
        self.run_sync()
        message = SyncLog.objects.filter(message__startswith="Magic: The Gathering").latest("pk").message
        self.assertIn("file unchanged", message)

    def game_logs(self, before):
        return {log.message.split(" catalog")[0]: log for log in SyncLog.objects.order_by("pk")[before:]}

    def test_missing_bucket_logs_and_changes_nothing(self):
        before = SyncLog.objects.count()
        with override_settings(CARDLEDGER_CATALOG_BUCKET=""):
            catalog_sync.run_sync(cm_client=self.cm.client())
        logs = self.game_logs(before)
        self.assertEqual(len(logs), Game.objects.count())
        for log in logs.values():
            self.assertEqual(log.level, SyncLog.Level.WARNING)
            self.assertIn("CARDLEDGER_CATALOG_BUCKET is not set", log.message)
        self.assertFalse(Blueprint.objects.exists())
        self.assertEqual(self.cm.urls, [])  # no game has a catalog to price

    def test_missing_bucket_still_updates_prices_of_existing_catalogs(self):
        load_catalog()
        blueprints = set(Blueprint.objects.values_list("cm_product_id", "name", "collector_number"))
        before = SyncLog.objects.count()
        with override_settings(CARDLEDGER_CATALOG_BUCKET=""):
            results = {r.game: r for r in catalog_sync.run_sync(cm_client=self.cm.client())}
        self.assertEqual(CardmarketPrice.objects.get(pk=501201).trend, Decimal("1.20"))
        self.assertEqual(results["mtg"].priced, 2)
        self.assertEqual(self.cm.urls, [cardmarket.price_guide_url(1)])  # only Magic has a catalog
        self.assertEqual(set(Blueprint.objects.values_list("cm_product_id", "name", "collector_number")), blueprints)
        self.assertEqual(game().catalog_sha256, "")
        magic = self.game_logs(before)["Magic: The Gathering"]
        self.assertEqual(magic.level, SyncLog.Level.WARNING)
        self.assertIn("CARDLEDGER_CATALOG_BUCKET is not set", magic.message)
        self.assertIn("2 priced (guide 2026-11-14)", magic.message)

    def test_unusable_bucket_client_logs_and_changes_nothing(self):
        for error in (NoRegionError(), ValueError("Invalid endpoint: nope")):
            with self.subTest(error=error), \
                    mock.patch.object(catalog_files.boto3, "client", side_effect=error):
                catalog_sync.run_sync(cm_client=self.cm.client())
                log = SyncLog.objects.latest("pk")
                self.assertEqual(log.level, SyncLog.Level.WARNING)
                self.assertIn("catalog sync failed: ", log.message)
        self.assertFalse(Blueprint.objects.exists())

    def test_unreadable_game_file_still_updates_prices_of_existing_catalog(self):
        self.run_sync()
        self.cm.guides[1]["priceGuides"][0]["trend"] = 2.5
        self.files.games["mtg"][3]["name"] = "Counterspell (Retro)"
        self.files.denied.add("mtg")
        before = SyncLog.objects.count()
        self.run_sync()
        logs = self.game_logs(before)
        self.assertIn("Cannot read catalog/v1/mtg.json", logs["Magic: The Gathering"].message)
        self.assertEqual(logs["Magic: The Gathering"].level, SyncLog.Level.WARNING)
        self.assertEqual(CardmarketPrice.objects.get(pk=501201).trend, Decimal("2.50"))
        self.assertEqual(Blueprint.objects.get(cm_product_id=501201).name, "Counterspell")
        self.assertEqual(logs["Pokémon"].level, SyncLog.Level.OK)

    def test_unreadable_game_files_log_and_change_nothing(self):
        self.files.denied.update({"mtg", "pokemon"})
        self.run_sync()
        self.assertIn("Cannot read catalog/v1/pokemon.json",
                      SyncLog.objects.filter(message__startswith="Pokémon").latest("pk").message)
        self.assertFalse(Blueprint.objects.exists())

    def test_unexpected_failure_of_one_game_logs_every_game_and_raises(self):
        self.files.games["lorcana"] = copy.deepcopy(LORCANA)
        self.cm.guides[game("lorcana").cm_id] = copy.deepcopy(LORCANA_GUIDE)
        real_write = catalog_sync._write

        def failing_write(g, file, prices, guide_date):
            if g.slug == "lorcana":
                raise RuntimeError("db down")
            return real_write(g, file, prices, guide_date)

        before = SyncLog.objects.count()
        with mock.patch.object(catalog_sync, "_write", side_effect=failing_write):
            with self.assertRaises(catalog_sync.CatalogSyncError):  # the task is marked failed
                self.run_sync()
        logs = self.game_logs(before)
        self.assertEqual(len(logs), Game.objects.count())
        self.assertEqual(logs["Disney Lorcana"].level, SyncLog.Level.WARNING)
        self.assertIn("RuntimeError: db down", logs["Disney Lorcana"].message)
        self.assertEqual(logs["Magic: The Gathering"].level, SyncLog.Level.OK)
        self.assertEqual(Blueprint.objects.filter(expansion__game=game()).count(), 7)

    def test_unexpected_global_failure_is_logged_and_raised(self):
        with mock.patch.object(catalog_sync.Game.objects, "order_by", side_effect=RuntimeError("db down")):
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
