import hashlib
import json
import tempfile
import tomllib
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

import cv2
import numpy as np

from . import catalog, recognition, vision
from .api.scan import SESSION_LIST_LIMIT
from .api.serializers import MAX_FRAME_BYTES
from .forms import CatalogItemForm, ItemForm, ListingForm
from .models import Blueprint, CollectionItem, Expansion, GameVisionIndex, ScanCapture, ScanLine, ScanSession
from .tests import LedgerTestCase, cm_id, game, load_catalog
from .test_game_api import pokemon_setup
from .test_vision_utils import make_index, photo_of_card

IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
DESKTOP = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"


def blank_photo():
    """A photo of an empty grey table: no card in it."""
    ok, jpg = cv2.imencode(".jpg", np.full((600, 400, 3), 128, np.uint8))
    assert ok
    return jpg.tobytes()


class ScanApiBase(LedgerTestCase):
    def setUp(self):
        super().setUp()
        load_catalog()
        self.dmr = Expansion.objects.get(code="dmr")
        self.counterspell = Blueprint.objects.get(cm_product_id=cm_id(1201))
        # Magic's vision index: every single with its own synthetic art; self.photo is a photo of Counterspell.
        vision._cache.clear()
        singles = list(Blueprint.objects.filter(expansion__game=game(), category__is_single=True)
                       .select_related("expansion").order_by("cm_product_id"))
        self.arts = {bp.cm_product_id: photo_of_card(seed) for seed, bp in enumerate(singles, start=1)}
        self.index_entries = [(bp, self.arts[bp.cm_product_id][0]) for bp in singles]
        self.give_index(game(), self.index_entries)
        self.photo = self.arts[self.counterspell.cm_product_id][1]
        patcher = mock.patch.object(recognition, "match_photo", wraps=recognition.match_photo)
        self.recognize = patcher.start()
        self.addCleanup(patcher.stop)
        self.client.defaults["HTTP_USER_AGENT"] = IPHONE

    def give_index(self, the_game, entries):
        """Store the vision index of the_game made of entries [(blueprint, card_png or None)]."""
        data = make_index(the_game, entries)
        GameVisionIndex.objects.update_or_create(
            game=the_game, defaults={"data": data, "sha256": hashlib.sha256(data).hexdigest()})

    def same_art(self, name="Counterspell", number="300"):
        """A second DMR printing (name, number) drawn with Counterspell's art: a photo of Counterspell can't tell
        them apart. Returns it."""
        twin = Blueprint.objects.create(cm_product_id=cm_id(1299), expansion=self.dmr, name=name,
                                        collector_number=number, category=self.counterspell.category)
        self.give_index(game(), [*self.index_entries, (twin, self.arts[self.counterspell.cm_product_id][0])])
        return twin

    @property
    def calls(self):
        """How many photos were recognised."""
        return self.recognize.call_count

    def call(self, method, name, *args, data=None, **extra):
        body = json.dumps(data) if data is not None else ""
        return getattr(self.client, method)(reverse(f"ledger:{name}", args=args), body,
                                            content_type="application/json", **extra)

    def start(self, expansion_id=None, condition="NM", language=None):
        data = {"expansion_id": expansion_id or self.dmr.pk, "condition": condition}
        if language:
            data["language"] = language
        resp = self.call("post", "scan_api_sessions", data=data)
        self.assertEqual(resp.status_code, 201, resp.content)
        return ScanSession.objects.get(uuid=resp.json()["session"]["uuid"])

    def capture(self, session, capture_id="c1", photo=None):
        frame = SimpleUploadedFile("f.jpg", photo or self.photo, content_type="image/jpeg")
        return self.client.post(reverse("ledger:scan_api_captures", args=[session.uuid]),
                                {"frame": frame, "capture_id": capture_id})

    def assertError(self, resp, status, code):
        """A non-validation error: {"detail", "code", "help_url"?}."""
        self.assertEqual(resp.status_code, status, resp.content)
        body = resp.json()
        self.assertEqual(body["code"], code)
        self.assertIsInstance(body["detail"], str)
        return body

    def assertInvalid(self, resp, *fields):
        """A validation error: 400 with the messages of exactly these fields."""
        self.assertEqual(resp.status_code, 400, resp.content)
        body = resp.json()
        self.assertEqual(sorted(body), sorted(fields))
        for messages in body.values():
            self.assertTrue(messages and all(isinstance(m, str) for m in messages), body)
        return body


class SetupAndSessionTest(ScanApiBase):
    def test_setup_lists_expansions_and_choices(self):
        body = self.call("get", "scan_api_setup").json()
        self.assertIn({"id": self.dmr.pk, "code": "dmr", "name": "Dominaria Remastered"}, body["expansions"])
        self.assertIn({"code": "NM", "label": "Near Mint"}, body["conditions"])
        self.assertIn({"code": "IT", "label": "Italian"}, body["languages"])
        self.assertEqual(body["defaults"], {"expansion_id": None, "condition": "NM", "language": "EN"})
        self.assertEqual((body["missing"], body["warnings"]), ([], []))

    def test_setup_defaults_follow_the_last_session(self):
        self.start(condition="EX")
        self.assertEqual(self.call("get", "scan_api_setup").json()["defaults"],
                         {"expansion_id": self.dmr.pk, "condition": "EX", "language": "EN"})

    def test_setup_defaults_to_last_session_language(self):
        self.start(language="it")
        self.assertEqual(self.call("get", "scan_api_setup").json()["defaults"]["language"], "IT")

    def test_session_created_with_language(self):
        session = self.start(language="IT")
        self.assertEqual(session.default_language, "IT")
        resp = self.call("get", "scan_api_session", session.uuid)
        self.assertEqual(resp.json()["session"]["default_language"], "IT")

    def test_language_defaults_to_en(self):
        self.assertEqual(self.start().default_language, "EN")

    def test_unknown_language_is_400(self):
        self.assertInvalid(self.call("post", "scan_api_sessions", data={"language": "KL"}), "language")

    def test_capture_line_uses_session_language_not_foil(self):
        session = self.start(language="IT")
        line = self.capture(session).json()["line"]
        self.assertEqual((line["language"], line["foil"]), ("IT", False))

    def test_unsure_capture_keeps_candidates_and_frame(self):
        session = self.start()
        body = self.uncertain_capture(session)
        self.assertEqual(body["result"], "confirm")
        line = ScanLine.objects.get(uuid=body["line"]["uuid"])
        self.assertTrue(line.needs_confirmation)
        self.assertTrue(line.candidates)
        self.assertTrue(bytes(line.frame))

    def uncertain_capture(self, session):
        self.same_art()
        return self.capture(session).json()

    def test_start_preloads_catalog(self):
        resp = self.call("post", "scan_api_sessions", data={"expansion_id": self.dmr.pk, "condition": "EX"})
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        session = ScanSession.objects.get(uuid=body["session"]["uuid"])
        self.assertEqual((session.expansion, session.default_condition), (self.dmr, "EX"))
        self.assertEqual(body["session"]["expansion"]["code"], "dmr")
        self.assertEqual((body["session"]["status"], body["session"]["count"], body["warnings"]), ("open", 0, []))
        self.assertNotIn("sale_threshold", body["session"])

    def test_start_all_expansions(self):
        resp = self.call("post", "scan_api_sessions", data={"expansion_id": None, "condition": "NM"})
        self.assertEqual(resp.status_code, 201)
        self.assertIsNone(resp.json()["session"]["expansion"])

    def test_start_unknown_expansion_is_invalid(self):
        self.assertInvalid(self.call("post", "scan_api_sessions", data={"expansion_id": 999999}), "expansion_id")

    def test_start_unknown_condition_is_invalid(self):
        self.assertInvalid(self.call("post", "scan_api_sessions", data={"condition": "XX"}), "condition")

    def test_start_defaults_to_all_expansions_near_mint(self):
        resp = self.call("post", "scan_api_sessions", data={})
        self.assertEqual(resp.status_code, 201)
        session = ScanSession.objects.get(uuid=resp.json()["session"]["uuid"])
        self.assertEqual((session.expansion, session.default_condition), (None, "NM"))

    def test_session_detail_shows_last_added_line(self):
        session = self.start()
        self.assertIsNone(self.call("get", "scan_api_session", session.uuid).json()["session"]["last_line"])
        self.capture(session)
        body = self.call("get", "scan_api_session", session.uuid).json()["session"]
        self.assertEqual(body["count"], 1)
        line = body["last_line"]
        self.assertEqual(line["blueprint"]["id"], cm_id(1201))
        self.assertEqual(line["blueprint"]["expansion"]["code"], "dmr")
        self.assertEqual((line["language"], line["condition"]), ("EN", "NM"))
        for gone in ("price", "for_sale", "listed"):
            self.assertNotIn(gone, line)

    def test_saved_session_is_still_readable(self):
        session = self.start()
        session.status = ScanSession.Status.SAVED
        session.save()
        resp = self.call("get", "scan_api_session", session.uuid)
        self.assertEqual(resp.json()["session"]["status"], "saved")

    def test_unknown_session_404(self):
        self.assertError(self.call("get", "scan_api_session", "00000000-0000-0000-0000-000000000000"),
                         404, "not_found")


class SessionListTest(ScanApiBase):
    def listed(self):
        resp = self.call("get", "scan_api_sessions")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["results"]

    def touch(self, session, when):
        ScanSession.objects.filter(pk=session.pk).update(updated_at=when)

    def test_empty(self):
        self.assertEqual(self.listed(), [])

    def test_lists_open_sessions_with_their_details(self):
        session = self.start()
        self.capture(session)
        [item] = self.listed()
        self.assertEqual(item["uuid"], str(session.uuid))
        self.assertEqual(item["status"], "open")
        self.assertEqual(item["expansion"]["code"], "dmr")
        self.assertEqual((item["default_condition"], item["count"]), ("NM", 1))
        self.assertEqual(item["last_line"]["blueprint"]["id"], cm_id(1201))
        self.assertIn("phone_url", item)
        self.assertTrue(item["created_at"] and item["updated_at"])

    def test_saved_sessions_are_excluded(self):
        kept, saved = self.start(), self.start()
        saved.status = ScanSession.Status.SAVED
        saved.save()
        self.assertEqual([i["uuid"] for i in self.listed()], [str(kept.uuid)])

    def test_other_users_sessions_are_excluded(self):
        mine = self.start()
        ScanSession.objects.create(owner=self.other, game=game(), expansion=self.dmr)
        self.assertEqual([i["uuid"] for i in self.listed()], [str(mine.uuid)])

    def test_newest_activity_first(self):
        older, newer = self.start(), self.start()
        self.touch(older, timezone.now() - timedelta(hours=2))
        self.touch(newer, timezone.now() - timedelta(hours=1))
        self.assertEqual([i["uuid"] for i in self.listed()], [str(newer.uuid), str(older.uuid)])

    def test_scanning_a_card_counts_as_activity(self):
        scanned, idle = self.start(), self.start()
        self.touch(scanned, timezone.now() - timedelta(hours=2))
        self.touch(idle, timezone.now() - timedelta(hours=1))
        self.capture(scanned)
        listed = self.listed()
        self.assertEqual([i["uuid"] for i in listed], [str(scanned.uuid), str(idle.uuid)])
        line = ScanLine.objects.get(session=scanned)
        self.assertEqual(parse_datetime(listed[0]["updated_at"]), line.updated_at)

    def test_capped(self):
        for _ in range(SESSION_LIST_LIMIT + 2):
            ScanSession.objects.create(owner=self.user, game=game(), expansion=self.dmr)
        self.assertEqual(len(self.listed()), SESSION_LIST_LIMIT)


class SessionDeleteTest(ScanApiBase):
    def test_discard_open_session_with_its_cards(self):
        session = self.start()
        self.capture(session)
        resp = self.call("delete", "scan_api_session", session.uuid)
        self.assertEqual(resp.status_code, 204, resp.content)
        self.assertFalse(ScanSession.objects.filter(pk=session.pk).exists())
        self.assertFalse(ScanLine.objects.filter(session_id=session.pk).exists())
        self.assertFalse(ScanCapture.objects.filter(session_id=session.pk).exists())
        self.assertEqual(self.call("get", "scan_api_sessions").json()["results"], [])

    def test_saved_session_cannot_be_discarded(self):
        session = self.start()
        session.status = ScanSession.Status.SAVED
        session.save()
        self.assertError(self.call("delete", "scan_api_session", session.uuid), 409, "closed")
        self.assertTrue(ScanSession.objects.filter(pk=session.pk).exists())

    def test_saved_while_discarding_is_409(self):
        session = self.start()
        ScanSession.objects.filter(pk=session.pk).update(status=ScanSession.Status.SAVED)
        with mock.patch("ledger.api.scan._get_session", return_value=session):
            self.assertError(self.call("delete", "scan_api_session", session.uuid), 409, "closed")
        self.assertTrue(ScanSession.objects.filter(pk=session.pk).exists())

    def test_saving_a_session_discarded_meanwhile_is_404(self):
        session = self.start()
        ScanSession.objects.filter(pk=session.pk).delete()
        with mock.patch("ledger.api.scan._get_session", return_value=session):
            self.assertError(self.call("post", "scan_api_save", session.uuid), 404, "not_found")

    def test_other_users_session_is_404(self):
        theirs = ScanSession.objects.create(owner=self.other, game=game(), expansion=self.dmr)
        self.assertError(self.call("delete", "scan_api_session", theirs.uuid), 404, "not_found")
        self.assertTrue(ScanSession.objects.filter(pk=theirs.pk).exists())


class CaptureTest(ScanApiBase):
    def test_capture_recognizes_card(self):
        session = self.start()
        resp = self.capture(session)
        body = resp.json()
        self.assertEqual((resp.status_code, body["result"], body["message"]), (200, "added", "Added · 1 copy"))
        self.assertEqual((body["line"]["blueprint"]["id"], body["line"]["needs_confirmation"]), (cm_id(1201), False))
        line = ScanLine.objects.get(session=session)
        self.assertEqual((line.blueprint, line.language, line.foil), (self.counterspell, "EN", False))
        self.assertEqual(self.recognize.call_args.args[2:], (session.game, self.dmr))

    def test_capture_unsure_asks_confirmation(self):
        twin = self.same_art()
        session = self.start(expansion_id=None)
        session.expansion = None
        session.save()
        body = self.capture(session).json()
        self.assertEqual((body["result"], body["message"]), ("confirm", "Which one is it?"))
        line = ScanLine.objects.get(uuid=body["line"]["uuid"])
        self.assertTrue(line.needs_confirmation)
        self.assertIsNone(line.blueprint)
        self.assertEqual(set(line.candidates), {cm_id(1201), twin.cm_product_id})
        self.assertEqual(line.read_name, "Counterspell")
        self.assertTrue(bytes(line.frame))

    def test_capture_without_index_is_service_missing(self):
        session = self.start()
        GameVisionIndex.objects.all().delete()
        body = self.assertError(self.capture(session, "noindex"), 503, "service_missing")
        self.assertEqual(body["detail"], "Card recognition isn't available for Magic: The Gathering yet")
        self.assertNotIn("help_url", body)
        self.assertEqual((self.calls, ScanLine.objects.count()), (0, 0))
        self.assertFalse(ScanCapture.objects.filter(capture_id="noindex").exists())
        # manual search still works without an index
        resp = self.call("post", "scan_api_manual", session.uuid,
                         data={"blueprint_id": cm_id(1201), "language": "EN", "foil": False})
        self.assertEqual(resp.json()["count"], 1)

    def test_capture_without_card_is_no_card_and_retryable(self):
        session = self.start()
        body = self.capture(session, "retry", photo=blank_photo()).json()
        self.assertEqual((body["result"], body["message"], body["line"]),
                         ("no_card", "No card in the picture — frame a card", None))
        self.assertFalse(ScanCapture.objects.filter(capture_id="retry").exists())
        body = self.capture(session, "retry").json()
        self.assertEqual((body["result"], body["count"]), ("added", 1))

    def test_repeated_capture_id_adds_once(self):
        session = self.start()
        self.capture(session, "twice")
        body = self.capture(session, "twice").json()
        self.assertEqual((body["result"], body["count"]), ("added", 1))
        self.assertEqual(ScanLine.objects.get(session=session).quantity, 1)
        self.assertEqual(self.calls, 1)

    def test_unexpected_error_releases_the_capture(self):
        session = self.start()
        with mock.patch.object(recognition, "match_photo", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.capture(session, "boom")
        self.assertFalse(ScanCapture.objects.filter(capture_id="boom").exists())
        self.assertEqual(self.capture(session, "boom").json()["result"], "added")

    def test_capture_adds_line(self):
        resp = self.capture(self.start())
        body = resp.json()
        self.assertEqual((body["result"], body["message"], body["count"]), ("added", "Added · 1 copy", 1))
        self.assertEqual(body["line"]["blueprint"]["name"], "Counterspell")

    def test_capture_retry_same_id(self):
        session = self.start()
        self.capture(session, "same")
        body = self.capture(session, "same").json()
        self.assertEqual((body["result"], body["count"]), ("added", 1))
        self.assertEqual(self.calls, 1)

    def test_retry_after_merge_adds_once(self):
        session = self.start()
        self.capture(session, "a")
        self.capture(session, "b")
        body = self.capture(session, "b").json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["message"], "Added · 2 copies")
        self.assertEqual(ScanLine.objects.get(session=session).quantity, 2)
        self.assertEqual(self.calls, 2)

    def test_capture_in_progress_is_busy(self):
        session = self.start()
        ScanCapture.objects.create(session=session, capture_id="busy")
        resp = self.capture(session, "busy")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json()["result"], "busy")
        self.assertIn("Still recognising", resp.json()["message"])
        self.assertEqual(self.calls, 0)

    def test_undone_capture_is_handled(self):
        session = self.start()
        line_uuid = self.capture(session, "u").json()["line"]["uuid"]
        self.call("post", "scan_api_undo", line_uuid)
        body = self.capture(session, "u").json()
        self.assertEqual((body["result"], body["line"], body["count"]), ("handled", None, 0))

    def test_photo_without_a_card_adds_nothing(self):
        session = self.start()
        body = self.capture(session, "empty", photo=blank_photo()).json()
        self.assertEqual((body["result"], body["line"], body["count"]), ("no_card", None, 0))
        self.assertIn("No card in the picture", body["message"])
        self.assertEqual(ScanLine.objects.count(), 0)
        self.assertFalse(ScanCapture.objects.filter(capture_id="empty").exists())

    def test_missing_photo(self):
        session = self.start()
        resp = self.client.post(reverse("ledger:scan_api_captures", args=[session.uuid]), {"capture_id": "x"})
        self.assertInvalid(resp, "frame")

    def test_oversized_photo(self):
        session = self.start()
        frame = SimpleUploadedFile("f.jpg", b"\xff" * (MAX_FRAME_BYTES + 1), content_type="image/jpeg")
        resp = self.client.post(reverse("ledger:scan_api_captures", args=[session.uuid]), {"frame": frame})
        self.assertInvalid(resp, "frame")
        self.assertEqual(self.calls, 0)

    def test_capture_needs_multipart(self):
        session = self.start()
        self.assertError(self.call("post", "scan_api_captures", session.uuid, data={}), 415, "unsupported_media_type")

    def test_capture_on_closed_session_409(self):
        session = self.start()
        session.status = ScanSession.Status.SAVED
        session.save()
        self.assertError(self.capture(session), 409, "closed")
        self.assertEqual(self.calls, 0)


class MissingServicesTest(ScanApiBase):
    def test_scan_works_for_any_user(self):
        self.client.force_login(self.other)  # no market connected
        self.assertEqual(self.call("get", "scan_api_setup").json()["missing"], [])
        session = self.start()
        self.assertEqual(self.capture(session).json()["result"], "added")

    def test_capture_with_empty_catalog_says_so(self):
        session = self.start()
        Expansion.objects.all().delete()
        body = self.assertError(self.capture(session), 503, "service_missing")
        self.assertIn("The card catalog is empty", body["detail"])
        self.assertNotIn("help_url", body)
        self.assertEqual(self.calls, 0)

    def test_setup_lists_missing_services(self):
        Expansion.objects.all().delete()
        body = self.call("get", "scan_api_setup").json()
        self.assertEqual(body["warnings"], [])
        self.assertEqual(len(body["missing"]), 1)
        self.assertIn("The card catalog is empty", body["missing"][0]["message"])
        self.assertEqual(body["missing"][0]["url"], "")


class ConfirmTest(ScanApiBase):
    def uncertain(self, session):
        self.same_art()
        return self.capture(session).json()

    def test_uncertain_capture_then_confirm(self):
        session = self.start()
        body = self.uncertain(session)
        self.assertEqual(body["result"], "confirm")
        line = body["line"]
        self.assertTrue(line["needs_confirmation"])
        self.assertEqual({c["id"] for c in line["candidates"]}, {cm_id(1201), cm_id(1299)})
        frame = self.client.get(line["frame_url"])
        self.assertEqual(frame["Content-Type"], "image/jpeg")
        resp = self.call("post", "scan_api_confirm", line["uuid"],
                         data={"blueprint_id": cm_id(1201), "language": "IT", "condition": "EX", "foil": True})
        confirmed = resp.json()["line"]
        self.assertFalse(confirmed["needs_confirmation"])
        self.assertEqual((confirmed["language"], confirmed["condition"], confirmed["foil"]), ("IT", "EX", True))
        self.assertIsNone(confirmed["frame_url"])
        self.assertEqual(resp.json()["count"], 1)

    def test_candidates_say_which_match_the_best_candidate_name(self):
        session = self.start()
        self.same_art(name="Ponder", number="45")
        line = self.capture(session).json()["line"]
        # Two names with the same art: the name isn't certain, so the nearest other names are offered too.
        self.assertLessEqual({cm_id(1201), cm_id(1299)}, {c["id"] for c in line["candidates"]})
        self.assertEqual(line["read_name"], line["candidates"][0]["name"])
        self.assertEqual([c["matches_name"] for c in line["candidates"]],
                         [c["name"] == line["read_name"] for c in line["candidates"]])
        self.assertEqual(sum(c["matches_name"] for c in line["candidates"]), 1)

    def test_confirm_needs_card_and_language(self):
        session = self.start()
        line = self.uncertain(session)["line"]
        self.assertInvalid(self.call("post", "scan_api_confirm", line["uuid"], data={"blueprint_id": cm_id(1201)}),
                           "language")
        self.assertInvalid(self.call("post", "scan_api_confirm", line["uuid"], data={"blueprint_id": 999999,
                                                                                     "language": "IT"}),
                           "blueprint_id")

    def test_confirmed_line_is_returned_unchanged(self):
        session = self.start()
        line = self.capture(session).json()["line"]
        resp = self.call("post", "scan_api_confirm", line["uuid"], data={"blueprint_id": cm_id(1301), "language": "EN"})
        self.assertEqual(resp.json()["line"], line)

    def test_save_blocked_while_cards_need_confirmation(self):
        session = self.start()
        self.uncertain(session)
        body = self.assertError(self.call("post", "scan_api_save", session.uuid), 409, "blocked")
        self.assertEqual(body["detail"], "Some cards still need confirmation.")
        review = self.call("get", "scan_api_review", session.uuid).json()
        self.assertEqual((len(review["pending"]), len(review["lines"])), (1, 0))


class SearchTest(ScanApiBase):
    def test_manual_search_adds_line(self):
        session = self.start()
        body = self.client.get(reverse("ledger:scan_api_search", args=[session.uuid]), {"q": "counter"}).json()
        self.assertEqual([b["name"] for b in body["results"]], ["Counterspell"])
        self.assertEqual(body["where"], "dmr")
        resp = self.call("post", "scan_api_manual", session.uuid,
                         data={"blueprint_id": cm_id(1201), "language": "EN", "foil": False})
        self.assertEqual(resp.json()["count"], 1)
        self.assertEqual(ScanLine.objects.get(session=session).quantity, 1)

    def test_manual_needs_card_and_language(self):
        session = self.start()
        self.assertInvalid(self.call("post", "scan_api_manual", session.uuid, data={"blueprint_id": cm_id(1201)}),
                           "language")

    def test_search_other_expansion(self):
        session = self.start()  # DMR locked
        resp = self.client.get(reverse("ledger:scan_api_search", args=[session.uuid]), {"q": "sheoldred", "set": "dmu"})
        body = resp.json()
        self.assertEqual([b["name"] for b in body["results"]], ["Sheoldred, the Apocalypse"])
        self.assertEqual(body["where"], "dmu")

    def test_search_all_expansions(self):
        session = self.start()
        resp = self.client.get(reverse("ledger:scan_api_search", args=[session.uuid]), {"q": "counter", "set": "all"})
        self.assertIsNone(resp.json()["where"])

class PhoneTest(ScanApiBase):
    def test_desktop_gets_the_phone_url_of_this_session(self):
        session = self.start()
        body = self.call("get", "scan_api_session", session.uuid, HTTP_USER_AGENT=DESKTOP, secure=True).json()
        data = body["session"]
        self.assertFalse(data["is_phone"])
        self.assertEqual(data["phone_url"], f"https://testserver/mtg/scan/{session.uuid}/")
        self.assertNotIn("phone_qr_svg", data)
        self.assertTrue(data["phone_secure"])

    def test_desktop_qr_warns_when_not_https(self):
        session = self.start()
        data = self.call("get", "scan_api_session", session.uuid, HTTP_USER_AGENT=DESKTOP).json()["session"]
        self.assertFalse(data["phone_secure"])

    def test_android_is_a_phone(self):
        session = self.start()
        android = "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 Chrome/140.0 Mobile Safari/537.36"
        data = self.call("get", "scan_api_session", session.uuid, HTTP_USER_AGENT=android).json()["session"]
        self.assertTrue(data["is_phone"])


class ReviewTest(ScanApiBase):
    def test_save(self):
        session = self.start()
        self.capture(session)
        body = self.call("get", "scan_api_review", session.uuid).json()
        self.assertEqual((body["copies"], body["unique"]), (1, 1))
        self.assertNotIn("listed", body)
        resp = self.call("post", "scan_api_save", session.uuid)
        self.assertEqual(resp.json(), {"saved": 1, "redirect": "/mtg/", "messages": [
            {"level": "success", "text": "Saved 1 cards."}]})
        self.assertTrue(CollectionItem.objects.filter(card__cm_product_id=cm_id(1201)).exists())

    def test_threshold_endpoint_is_gone(self):
        with self.assertRaises(NoReverseMatch):
            reverse("ledger:scan_api_threshold", args=["00000000-0000-0000-0000-000000000000"])

    def test_closed_session_rejects_changes(self):
        session = self.start()
        line = self.capture(session).json()["line"]
        self.call("post", "scan_api_save", session.uuid)
        self.assertError(self.call("post", "scan_api_save", session.uuid), 409, "closed")
        self.assertError(self.call("post", "scan_api_manual", session.uuid,
                                   data={"blueprint_id": cm_id(1201), "language": "EN"}), 409, "closed")
        self.assertError(self.call("patch", "scan_api_line", line["uuid"], data={"foil": True}), 409, "closed")
        self.assertError(self.call("post", "scan_api_undo", line["uuid"]), 409, "closed")
        self.assertError(self.call("post", "scan_api_confirm", line["uuid"], data={}), 409, "closed")
        self.assertEqual(self.call("get", "scan_api_review", session.uuid).status_code, 200)


class LineTest(ScanApiBase):
    def setUp(self):
        super().setUp()
        self.session = self.start()
        self.line = self.capture(self.session).json()["line"]

    def patch(self, data):
        return self.call("patch", "scan_api_line", self.line["uuid"], data=data)

    def test_patch_fields(self):
        body = self.patch({"foil": True, "condition": "EX", "language": "en", "quantity": 3}).json()
        line = body["line"]
        self.assertEqual((line["foil"], line["condition"], line["language"], line["quantity"]),
                         (True, "EX", "EN", 3))
        self.assertEqual(body["count"], 3)

    def test_patch_quantity_zero_deletes(self):
        self.assertEqual(self.patch({"quantity": 0}).json(), {"line": None, "count": 0})

    def test_patch_validation(self):
        for data, fields in (({"foil": "maybe"}, ["foil"]), ({"condition": "XX"}, ["condition"]),
                             ({"language": "KL"}, ["language"]), ({"quantity": -1}, ["quantity"]),
                             ({"quantity": "two"}, ["quantity"]), ({"quantity": True}, ["quantity"]),
                             ({"for_sale": False}, ["for_sale"]), ({"price": "1"}, ["price"]),
                             ({"foil": True, "price": "1"}, ["price"]), ({}, ["non_field_errors"]),
                             (["foil"], ["non_field_errors"])):
            with self.subTest(data=data):
                self.assertInvalid(self.patch(data), *fields)
        self.assertError(self.client.patch(reverse("ledger:scan_api_line", args=[self.line["uuid"]]), "{",
                                           content_type="application/json"), 400, "parse_error")
        line = ScanLine.objects.get(uuid=self.line["uuid"])
        self.assertEqual((line.foil, line.condition, line.quantity), (False, "NM", 1))

    def test_undo(self):
        self.capture(self.session, "c2")
        body = self.call("post", "scan_api_undo", self.line["uuid"]).json()
        self.assertEqual((body["line"]["quantity"], body["count"]), (1, 1))
        self.assertEqual(self.call("post", "scan_api_undo", self.line["uuid"]).json(), {"line": None, "count": 0})
        self.assertError(self.call("post", "scan_api_undo", self.line["uuid"]), 404, "not_found")


class ProtocolTest(ScanApiBase):
    def test_403_without_login(self):
        self.client.logout()
        session = ScanSession.objects.create(owner=self.user, game=game())
        for resp in (self.call("get", "scan_api_setup"),
                     self.call("post", "scan_api_sessions", data={}),
                     self.call("get", "scan_api_review", session.uuid),
                     self.client.get(reverse("ledger:scan_api_frame", args=[session.uuid]))):
            self.assertError(resp, 403, "not_authenticated")

    def test_403_without_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        resp = client.post(reverse("ledger:scan_api_sessions"), "{}", content_type="application/json")
        self.assertError(resp, 403, "permission_denied")
        client.get(reverse("accounts:api_csrf"))
        token = client.cookies["csrftoken"].value
        resp = client.post(reverse("ledger:scan_api_sessions"), "{}", content_type="application/json",
                           HTTP_X_CSRFTOKEN=token)
        self.assertEqual(resp.status_code, 201)

    def test_405_on_wrong_method(self):
        session = self.start()
        resp = self.call("delete", "scan_api_sessions")
        self.assertError(resp, 405, "method_not_allowed")
        self.assertEqual(set(resp["Allow"].split(", ")), {"GET", "POST", "OPTIONS"})
        self.assertError(self.call("post", "scan_api_review", session.uuid), 405, "method_not_allowed")


class ScanGameTest(ScanApiBase):
    """A scan session belongs to a game: recognition, search and picks stay inside it."""

    def setUp(self):
        super().setUp()
        self.sv1, self.pikachu, _ = pokemon_setup(self)
        self.pikachu_card, self.pikachu_photo = photo_of_card(99)

    def start_in(self, slug, **data):
        resp = self.call("post", "scan_api_sessions", data={"game": slug, **data})
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()["session"], ScanSession.objects.get(uuid=resp.json()["session"]["uuid"])

    def test_start_session_in_game(self):
        body, session = self.start_in("pokemon")
        self.assertEqual((body["game"], session.game.slug), ("pokemon", "pokemon"))
        resp = self.call("post", "scan_api_sessions", data={})
        self.assertEqual(resp.json()["session"]["game"], "mtg")

    def test_unknown_game_is_invalid(self):
        self.assertInvalid(self.call("post", "scan_api_sessions", data={"game": "chess"}), "game")

    def test_expansion_must_belong_to_game(self):
        self.assertInvalid(self.call("post", "scan_api_sessions", data={"game": "pokemon", "expansion_id": self.dmr.pk}),
                           "expansion_id")
        self.assertInvalid(self.call("post", "scan_api_sessions", data={"expansion_id": self.sv1.pk}), "expansion_id")
        body, session = self.start_in("pokemon", expansion_id=self.sv1.pk)
        self.assertEqual((body["expansion"]["code"], session.expansion), ("sv1", self.sv1))

    def test_setup_per_game(self):
        body = self.client.get(reverse("ledger:scan_api_setup"), {"game": "pokemon"}).json()
        self.assertEqual([e["code"] for e in body["expansions"]], ["sv1"])
        self.assertEqual(body["missing"], [])
        self.assertNotIn("sv1", [e["code"] for e in self.call("get", "scan_api_setup").json()["expansions"]])
        self.assertNotEqual(self.client.get(reverse("ledger:scan_api_setup"), {"game": "lorcana"}).json()["missing"], [])

    def test_setup_defaults_follow_the_last_session_of_the_game(self):
        self.start(condition="EX")
        self.start_in("pokemon", expansion_id=self.sv1.pk, condition="GD")
        self.assertEqual(self.call("get", "scan_api_setup").json()["defaults"],
                         {"expansion_id": self.dmr.pk, "condition": "EX", "language": "EN"})
        self.assertEqual(self.client.get(reverse("ledger:scan_api_setup"), {"game": "pokemon"}).json()["defaults"],
                         {"expansion_id": self.sv1.pk, "condition": "GD", "language": "EN"})

    def test_sessions_list_per_game(self):
        magic = self.start()
        _, pokemon = self.start_in("pokemon")
        listed = self.client.get(reverse("ledger:scan_api_sessions"), {"game": "pokemon"}).json()["results"]
        self.assertEqual([i["uuid"] for i in listed], [str(pokemon.uuid)])
        everything = self.call("get", "scan_api_sessions").json()["results"]
        self.assertEqual({i["uuid"] for i in everything}, {str(magic.uuid), str(pokemon.uuid)})

    def test_capture_matches_in_session_game(self):
        _, session = self.start_in("pokemon")
        self.give_index(game("pokemon"), [(self.pikachu, self.pikachu_card)])
        resp = self.capture(session, photo=self.pikachu_photo)
        self.assertEqual(resp.json()["result"], "added", resp.content)
        self.assertEqual(ScanLine.objects.get(session=session).blueprint.cm_product_id, 700001)
        self.assertEqual(self.recognize.call_args.args[2], game("pokemon"))

    def test_capture_without_the_games_index_is_service_missing(self):
        _, session = self.start_in("pokemon")  # Magic has an index, Pokémon has none
        body = self.assertError(self.capture(session, photo=self.pikachu_photo), 503, "service_missing")
        self.assertEqual(body["detail"], "Card recognition isn't available for Pokémon yet")

    def test_capture_in_magic_session_ignores_other_games(self):
        _, session = self.start_in("mtg")
        self.give_index(game("pokemon"), [(self.pikachu, self.pikachu_card)])
        self.assertEqual(self.capture(session, photo=self.pikachu_photo).json()["result"], "confirm")
        self.assertNotIn(700001, ScanLine.objects.get(session=session).candidates)

    def test_manual_rejects_other_game(self):
        _, session = self.start_in("pokemon")
        self.assertInvalid(self.call("post", "scan_api_manual", session.uuid,
                                     data={"blueprint_id": cm_id(1201), "language": "EN"}), "blueprint_id")
        resp = self.call("post", "scan_api_manual", session.uuid, data={"blueprint_id": 700001, "language": "EN"})
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_confirm_stays_in_game(self):
        _, session = self.start_in("pokemon")
        self.give_index(game("pokemon"), [(self.pikachu, self.pikachu_card)])
        line = self.capture(session).json()["line"]  # a photo of Counterspell: nothing close in Pokémon's index
        self.assertInvalid(self.call("post", "scan_api_confirm", line["uuid"],
                                     data={"blueprint_id": cm_id(1201), "language": "EN"}), "blueprint_id")
        resp = self.call("post", "scan_api_confirm", line["uuid"], data={"blueprint_id": 700001, "language": "EN"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(ScanLine.objects.get(session=session).blueprint, self.pikachu)

    def test_search_stays_in_game(self):
        _, session = self.start_in("pokemon")
        url = reverse("ledger:scan_api_search", args=[session.uuid])
        self.assertEqual(self.client.get(url, {"q": "Counterspell", "set": "all"}).json()["results"], [])
        self.assertEqual(self.client.get(url, {"q": "Counterspell", "set": "dmr"}).json()["where"], None)
        body = self.client.get(url, {"q": "pika", "set": "sv1"}).json()
        self.assertEqual(([b["id"] for b in body["results"]], body["where"]), ([700001], "sv1"))

    def test_phone_url_and_redirect_have_game(self):
        body, session = self.start_in("pokemon")
        self.assertTrue(body["phone_url"].endswith(f"/pokemon/scan/{session.uuid}/"), body["phone_url"])
        self.call("post", "scan_api_manual", session.uuid, data={"blueprint_id": 700001, "language": "EN"})
        self.assertEqual(self.call("post", "scan_api_save", session.uuid).json()["redirect"], "/pokemon/")


class CandidateMatchesNameTest(LedgerTestCase):
    """The serializer compares read_name with the candidate's game: Magic suffixes are never stripped."""

    def matches(self, bp, name):
        from .api.serializers import CandidateSerializer
        return CandidateSerializer(bp, context={"read_name": name}).data["matches_name"]

    def test_magic_candidate_keeps_its_suffix(self):
        load_catalog()
        neo = Expansion.objects.get(code="neo")
        bp = Blueprint.objects.create(cm_product_id=900010, expansion=neo, collector_number="T2",
                                      name="Spirit Token (White 1/1)")
        self.assertFalse(self.matches(bp, "Spirit Token"))
        self.assertTrue(self.matches(bp, "Spirit Token (White 1/1)"))

    def test_pokemon_candidate_matches_without_attacks(self):
        pal = Expansion.objects.create(game=game("pokemon"), code="pal", name="Paldea Evolved")
        bp = Blueprint.objects.create(cm_product_id=900011, expansion=pal, collector_number="21",
                                      name="Arboliva [Enriching Oil | Solar Beam]")
        self.assertTrue(self.matches(bp, "Arboliva"))


class SchemaTest(LedgerTestCase):
    def test_schema_describes_the_scan_api(self):
        resp = self.client.get(reverse("api_schema"), HTTP_ACCEPT="application/vnd.oai.openapi+json")
        self.assertEqual(resp.status_code, 200)
        schema = json.loads(resp.content)
        self.assertIn("/api/scan/sessions/{uid}/captures/", schema["paths"])
        self.assertIn("Line", schema["components"]["schemas"])

    def test_page_api_bodies_and_filters_are_documented(self):
        resp = self.client.get(reverse("api_schema"), HTTP_ACCEPT="application/vnd.oai.openapi+json")
        schema = json.loads(resp.content)
        components = schema["components"]["schemas"]

        def body(path, method):
            content = schema["paths"][path][method].get("requestBody", {}).get("content", {})
            return {kind: set(components[c["schema"]["$ref"].rsplit("/", 1)[1]]["properties"])
                    for kind, c in content.items()}

        listing = set(ListingForm.base_fields) | {"market"}
        self.assertEqual(body("/api/collection/", "post")["application/json"], set(CatalogItemForm.base_fields))
        self.assertEqual(body("/api/collection/{id}/", "put")["application/json"], set(ItemForm.base_fields))
        self.assertIn("blueprint", CatalogItemForm.base_fields)
        self.assertNotIn("name", ItemForm.base_fields)
        self.assertEqual(body("/api/collection/import/", "post"), {"multipart/form-data": {"file"}})
        self.assertEqual(body("/api/listings/{item_pk}/", "put")["application/json"], listing)
        self.assertEqual(body("/api/sync/conflicts/{id}/resolve/", "post")["application/json"], {"keep"})
        self.assertEqual(body("/api/account/profile/", "put")["application/json"], {"first_name", "last_name", "email",
                                                                         "shop_name", "vat_number"})
        self.assertEqual(body("/api/collection/bulk/", "post")["application/json"], {"ids", "action", "location", "pct",
                                                                                     "markets"})
        self.assertEqual(body("/api/account/password/", "post")["application/json"],
                         {"old_password", "new_password1", "new_password2"})
        self.assertEqual(body("/api/auth/logout/", "post"), {})
        for path in ("/api/account/cardtrader-token/", "/api/extension/cardtrader-token/", "/api/listings/publish/",
                     "/api/orders/import/", "/api/sync/run/", "/api/orders/{id}/ship/"):
            self.assertNotIn(path, schema["paths"])

        def query(path):
            return {p["name"] for p in schema["paths"][path]["get"].get("parameters", []) if p["in"] == "query"}

        self.assertEqual(query("/api/collection/"), {"q", "foil", "set", "cond", "category", "location",
                                                       "view", "game", "page"})
        self.assertEqual(query("/api/overview/"), {"game"})
        self.assertEqual(query("/api/listings/"), {"status", "game", "market"})
        self.assertEqual(query("/api/orders/"), {"tab", "market"})
        self.assertEqual(query("/api/orders/pick-list/"), {"ids"})
        self.assertEqual(query("/api/shipments/"), {"tab", "market", "page"})
        self.assertEqual(body("/api/shipments/{id}/", "patch")["application/json"],
                         {"carrier", "tracking_code", "postage_cost", "note", "delivered", "returned"})
        self.assertEqual(body("/api/orders/{id}/items/{item_pk}/", "patch")["application/json"], {"picked"})
        self.assertEqual(query("/api/catalog/"), {"q", "set", "number", "game", "category", "owned", "rarity", "finish",
                                                  "ordering", "page"})
        self.assertEqual(query("/api/catalog/categories/"), {"game"})
        self.assertNotIn("parameters", schema["paths"]["/api/collection/"]["post"])

    def test_schema_version_is_the_project_version(self):
        resp = self.client.get(reverse("api_schema"), HTTP_ACCEPT="application/vnd.oai.openapi+json")
        pyproject = tomllib.loads((settings.BASE_DIR / "pyproject.toml").read_text())
        self.assertEqual(json.loads(resp.content)["info"]["version"], pyproject["project"]["version"])

    def test_schema_generates_without_warnings(self):
        out, err = StringIO(), StringIO()
        call_command("spectacular", "--validate", "--fail-on-warn", stdout=out, stderr=err)
        self.assertEqual(err.getvalue(), "")

    def test_docs_are_public(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("api_schema")).status_code, 200)
        self.assertContains(self.client.get(reverse("api_docs")), "swagger")


class ScanIsolationTest(ScanApiBase):
    """Another user's sessions and lines answer 404, as if they did not exist."""

    def test_other_users_session_and_lines_are_404(self):
        session = self.start()
        self.same_art()
        line = ScanLine.objects.get(uuid=self.capture(session).json()["line"]["uuid"])
        self.client.force_login(self.other)
        frame = SimpleUploadedFile("f.jpg", b"\xff\xd8jpeg", content_type="image/jpeg")
        session_calls = [
            ("get", "scan_api_session", None), ("get", "scan_api_review", None),
            ("get", "scan_api_search", None), ("post", "scan_api_manual", {}),
            ("post", "scan_api_save", None),
        ]
        for method, name, data in session_calls:
            with self.subTest(name=name):
                self.assertError(self.call(method, name, session.uuid, data=data), 404, "not_found")
        with self.subTest(name="scan_api_captures"):
            resp = self.client.post(reverse("ledger:scan_api_captures", args=[session.uuid]),
                                    {"frame": frame, "capture_id": "x"})
            self.assertError(resp, 404, "not_found")
        line_calls = [
            ("patch", "scan_api_line", {"quantity": 3}), ("post", "scan_api_undo", None),
            ("post", "scan_api_confirm", {}),
        ]
        for method, name, data in line_calls:
            with self.subTest(name=name):
                self.assertError(self.call(method, name, line.uuid, data=data), 404, "not_found")
        with self.subTest(name="scan_api_frame"):
            self.assertEqual(self.client.get(reverse("ledger:scan_api_frame", args=[line.uuid])).status_code, 404)
        line.refresh_from_db()
        self.assertEqual(line.quantity, 1)
        self.assertEqual(self.calls, 1)

    def test_setup_defaults_come_from_own_last_session(self):
        self.start(condition="LP")
        self.client.force_login(self.other)
        self.assertEqual(self.call("get", "scan_api_setup").json()["defaults"]["condition"], "NM")

    def test_new_session_belongs_to_the_user(self):
        self.client.force_login(self.other)
        self.assertEqual(self.start().owner, self.other)
