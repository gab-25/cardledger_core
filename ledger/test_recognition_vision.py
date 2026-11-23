from unittest import mock

from cardledger_vision import Result

from . import recognition, vision
from .models import Blueprint, Expansion, GameVisionIndex
from .recognition import match_photo
from .test_vision_utils import make_index, photo_of_card
from .tests import LedgerTestCase, cm_id, game, load_catalog

PHOTO = b"photo"


def fake(outcome, product_id=None, candidates=()):
    return mock.patch.object(recognition, "recognize", return_value=Result(outcome, product_id, list(candidates)))


class MapTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        load_catalog()
        self.magic = game("mtg")
        self.neo = Expansion.objects.get(code="neo")
        self.a, self.b = Blueprint.objects.filter(expansion=self.neo).order_by("cm_product_id")[:2]

    def test_certain_match_maps_to_blueprint(self):
        with fake("ok", self.a.cm_product_id, [self.a.cm_product_id, self.b.cm_product_id]):
            m = match_photo(PHOTO, None, self.magic, self.neo)
        self.assertEqual((m.reason, m.blueprint, m.candidates), ("ok", self.a, [self.a, self.b]))

    def test_lock_is_the_session_expansion_code(self):
        with fake("not_found") as rec:
            match_photo(PHOTO, "index", self.magic, self.neo)
            match_photo(PHOTO, "index", self.magic, None)
        self.assertEqual([c.kwargs["lock"] for c in rec.call_args_list], ["neo", None])
        self.assertEqual(rec.call_args_list[0].args, (PHOTO, "index"))

    def test_unknown_ids_are_dropped_keeping_order(self):
        ids = [999999999, self.b.cm_product_id, 888888888, self.a.cm_product_id]
        with fake("ambiguous", None, ids):
            m = match_photo(PHOTO, None, self.magic, None)
        self.assertEqual((m.reason, m.blueprint, m.candidates), ("ambiguous", None, [self.b, self.a]))

    def test_certain_id_missing_becomes_ambiguous(self):
        with fake("ok", 999999999, [999999999, self.b.cm_product_id]):
            m = match_photo(PHOTO, None, self.magic, None)
        self.assertEqual((m.reason, m.blueprint, m.candidates), ("ambiguous", None, [self.b]))

    def test_certain_id_missing_without_others_is_not_found(self):
        with fake("ok", 999999999, [999999999]):
            m = match_photo(PHOTO, None, self.magic, None)
        self.assertEqual((m.reason, m.blueprint, m.candidates), ("not_found", None, []))

    def test_other_games_ids_are_ignored(self):
        with fake("ok", self.a.cm_product_id, [self.a.cm_product_id]):
            m = match_photo(PHOTO, None, game("pokemon"), None)
        self.assertEqual((m.reason, m.blueprint, m.candidates), ("not_found", None, []))

    def test_no_card(self):
        with fake("no_card"):
            m = match_photo(PHOTO, None, self.magic, None)
        self.assertEqual((m.reason, m.blueprint, m.candidates), ("no_card", None, []))

    def test_not_found_drops_the_unrelated_nearest_candidates(self):
        with fake("not_found", None, [self.a.cm_product_id, self.b.cm_product_id]):
            m = match_photo(PHOTO, None, self.magic, None)
        self.assertEqual((m.reason, m.blueprint, m.candidates), ("not_found", None, []))


class IndexForTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        load_catalog()
        vision._cache.clear()
        self.magic = game("mtg")
        self.bps = list(Blueprint.objects.order_by("cm_product_id")[:2])

    def store(self, data, sha):
        GameVisionIndex.objects.update_or_create(game=self.magic, defaults={"data": data, "sha256": sha})

    def test_index_for_game_without_index_is_none(self):
        self.assertIsNone(vision.index_for(self.magic))

    def test_index_cache_reloads_on_new_sha(self):
        self.store(make_index(self.magic, [(self.bps[0], None)]), "a" * 64)
        first = vision.index_for(self.magic)
        self.assertEqual(len(first), 1)
        self.assertIs(vision.index_for(self.magic), first)
        self.store(make_index(self.magic, [(b, None) for b in self.bps]), "b" * 64)
        second = vision.index_for(self.magic)
        self.assertIsNot(second, first)
        self.assertEqual(len(second), 2)

    def test_same_sha_does_not_read_the_bytes_again(self):
        self.store(make_index(self.magic, [(self.bps[0], None)]), "a" * 64)
        vision.index_for(self.magic)
        with mock.patch.object(vision.Index, "load") as load:
            vision.index_for(self.magic)
        load.assert_not_called()


class EndToEndTest(LedgerTestCase):
    """The real library on a synthetic photo (no mocks)."""

    def setUp(self):
        super().setUp()
        load_catalog()
        vision._cache.clear()
        self.magic = game("mtg")
        self.neo = Expansion.objects.get(code="neo")
        self.bps = list(Blueprint.objects.filter(expansion=self.neo).order_by("cm_product_id")[:3])
        self.cards = [photo_of_card(seed) for seed in range(len(self.bps))]
        GameVisionIndex.objects.create(
            game=self.magic, sha256="c" * 64,
            data=make_index(self.magic, [(b, c[0]) for b, c in zip(self.bps, self.cards)]),
        )

    def test_photo_of_an_indexed_card_is_recognized(self):
        index = vision.index_for(self.magic)
        for bp, (_, photo) in zip(self.bps, self.cards):
            m = match_photo(photo, index, self.magic, self.neo)
            self.assertEqual((m.reason, m.blueprint), ("ok", bp))

    def test_photo_without_a_card(self):
        import cv2, numpy as np
        _, jpg = cv2.imencode(".jpg", np.full((600, 400, 3), 128, np.uint8))
        m = match_photo(jpg.tobytes(), vision.index_for(self.magic), self.magic, None)
        self.assertEqual(m.reason, "no_card")
