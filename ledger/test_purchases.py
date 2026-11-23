"""Purchase lots: CRUD and the per-user code, computed figures, tabs and KPIs, attachments, listing, cost allocation,
lots on collection items and scan sessions, me's lots_to_process."""

import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Blueprint, CollectionItem, Listing, PurchaseLot, ScanLine, ScanSession
from .test_dealer_api import COUNTERSPELL, RAGAVAN, SHEOLDRED, SOLITUDE, DealerTestCase
from .tests import add, game

PDF = b"%PDF-1.4 receipt"


class PurchaseTestCase(DealerTestCase):
    def create(self, **fields):
        body = {"type": "collection", "seller": "Mario", "price_paid": "100.00", **fields}
        resp = self.client.post(reverse("ledger:api_purchases"), body, content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return PurchaseLot.objects.get(pk=resp.json()["id"])

    def lots(self, **params):
        resp = self.client.get(reverse("ledger:api_purchases"), params)
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def detail(self, lot):
        resp = self.client.get(reverse("ledger:api_purchase", args=[lot.pk]))
        self.assertEqual(resp.status_code, 200)
        return resp.json()


class PurchaseCrudTest(PurchaseTestCase):
    def test_create_with_defaults_and_sequence(self):
        resp = self.client.get(reverse("ledger:api_purchases_next_code"))
        self.assertEqual(resp.json(), {"code": "PL-0001"})
        resp = self.client.post(reverse("ledger:api_purchases"), {"seller": "Mario"}, content_type="application/json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["code"], "PL-0001")
        self.assertEqual(resp.json()["messages"][0]["text"], "Purchase PL-0001 recorded.")
        lot = PurchaseLot.objects.get(pk=resp.json()["id"])
        self.assertEqual((lot.type, lot.status, lot.paid_by, lot.price_paid, lot.bought_on),
                         ("collection", "in_post", "cash", Decimal("0"), timezone.localdate()))
        self.assertEqual(self.create().code, "PL-0002")
        self.assertEqual(self.client.get(reverse("ledger:api_purchases_next_code")).json()["code"], "PL-0003")
        # per user
        self.client.force_login(self.user)
        self.assertEqual(self.create().code, "PL-0001")

    def test_validation(self):
        resp = self.client.post(reverse("ledger:api_purchases"), {"type": "gift", "price_paid": "-1",
                                                                  "bought_on": "yesterday"},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(set(resp.json()), {"type", "price_paid", "bought_on"})

    def test_patch_keeps_absent_fields(self):
        lot = self.create(where="Lucca", cards_estimate=500, notes="old binder")
        resp = self.client.patch(reverse("ledger:api_purchase", args=[lot.pk]), {"status": "grading"},
                                 content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        lot.refresh_from_db()
        self.assertEqual((lot.status, lot.where, lot.cards_estimate, lot.notes, lot.seller),
                         ("grading", "Lucca", 500, "old binder", "Mario"))
        data = self.detail(lot)
        self.assertEqual((data["notes"], data["attachments"], data["code"]), ("old binder", [], lot.code))

    def test_delete_keeps_items(self):
        lot = self.create()
        item = self.item(SHEOLDRED, lot=lot)
        resp = self.client.delete(reverse("ledger:api_purchase", args=[lot.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(PurchaseLot.objects.filter(pk=lot.pk).exists())
        item.refresh_from_db()
        self.assertIsNone(item.lot)

    def test_me_counts_lots_to_process(self):
        for status in ("in_post", "received", "grading", "listing", "done"):
            self.create(status=status)
        self.assertEqual(self.client.get(reverse("accounts:api_me")).json()["counts"]["lots_to_process"], 3)


class PurchaseFiguresTest(PurchaseTestCase):
    def setUp(self):
        super().setUp()
        self.guide(SHEOLDRED, trend=Decimal("60.00"))
        self.guide(COUNTERSPELL, trend=Decimal("1.00"))
        self.lot = self.create(price_paid="100.00", cards_estimate=10)
        self.sheoldred = self.item(SHEOLDRED, quantity=2, lot=self.lot)  # 120.00, published
        Listing.objects.create(item=self.sheoldred, price=Decimal("59.00"), quantity=2, status="published")
        self.counterspell = self.item(COUNTERSPELL, quantity=3, lot=self.lot, cost=Decimal("0.20"))  # 3.00, unlisted
        self.ragavan = self.item(RAGAVAN, lot=self.lot)  # no trend, unlisted, no cost

    def test_row(self):
        row = self.lots()["results"][0]
        self.assertEqual((row["code"], row["cards"], row["cards_estimate"], row["trend_value"], row["return_pct"],
                          row["price_paid"]), ("PL-0001", 6, 10, "123.00", "23.0", "100.00"))
        self.assertEqual(row["progress"], {"sorted": 6, "graded": 6, "priced": 5, "listed": 2, "total": 10})
        self.assertEqual(row["top_card"], {"name": "Sheoldred, the Apocalypse", "trend": "60.00"})
        self.assertEqual(row["unlisted"], 4)

    def test_lot_without_items(self):
        empty = self.create(cards_estimate=40, price_paid="0")
        row = self.detail(empty)
        self.assertEqual((row["cards"], row["trend_value"], row["return_pct"], row["top_card"], row["unlisted"]),
                         (40, None, None, None, 0))
        self.assertEqual(row["progress"]["total"], 40)

    def test_tabs_and_kpis(self):
        self.create(status="listing", price_paid="50.00", cards_estimate=20)
        old = self.create(status="done", price_paid="30.00")
        PurchaseLot.objects.filter(pk=old.pk).update(bought_on=timezone.localdate() - timedelta(days=40))
        data = self.lots()
        self.assertEqual(data["tabs"], [{"key": "all", "label": "All", "count": 3},
                                        {"key": "to_grade", "label": "To grade", "count": 1},
                                        {"key": "listing", "label": "Listing", "count": 1},
                                        {"key": "done", "label": "Done", "count": 1}])
        self.assertEqual(data["kpis"], {"spent_30d": "150.00", "lots_30d": 2, "cards_30d": 26,
                                        "trend_bought_30d": "123.00", "avg_paid_pct": "81.3", "waiting": 1,
                                        "waiting_cards": 6})
        listing = self.lots(tab="listing")
        self.assertEqual((listing["tab"], listing["count"], listing["results"][0]["status"]), ("listing", 1, "listing"))
        self.assertEqual(self.lots(tab="nope")["tab"], "all")

    def test_allocate_cost(self):
        resp = self.client.post(reverse("ledger:api_purchase_allocate_cost", args=[self.lot.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([m["level"] for m in resp.json()["messages"]], ["success", "warning"])
        self.assertEqual(resp.json()["count"], 2)
        for item in (self.sheoldred, self.counterspell, self.ragavan):
            item.refresh_from_db()
        # 100 × 60 / 123 per copy of Sheoldred, 100 × 1 / 123 per Counterspell; Ragavan has no trend
        self.assertEqual((self.sheoldred.cost, self.counterspell.cost, self.ragavan.cost),
                         (Decimal("48.78"), Decimal("0.81"), None))

    def test_allocate_without_trend(self):
        lot = self.create()
        self.item(RAGAVAN, lot=lot)
        resp = self.client.post(reverse("ledger:api_purchase_allocate_cost", args=[lot.pk]))
        self.assertEqual((resp.json()["count"], resp.json()["messages"][0]["level"]), (0, "warning"))

    def test_list_unlisted(self):
        resp = self.client.post(reverse("ledger:api_purchase_list", args=[self.lot.pk]), {"markets": ["cm", "ct"]},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["count"], 1)  # Counterspell; Ragavan has no price
        self.assertEqual([m["level"] for m in resp.json()["messages"]], ["success", "warning"])
        listings = Listing.objects.filter(item=self.counterspell).order_by("market")
        self.assertEqual([(l.market, l.price, l.quantity, l.status) for l in listings],
                         [("cm", Decimal("1.00"), 3, "to_publish"), ("ct", Decimal("1.00"), 3, "to_publish")])
        self.assertEqual(Listing.objects.filter(item=self.sheoldred).count(), 1)  # already listed: untouched
        self.assertEqual(self.client.post(reverse("ledger:api_purchase_list", args=[self.lot.pk]), {},
                                          content_type="application/json").status_code, 400)


class PurchaseAttachmentTest(PurchaseTestCase):
    def setUp(self):
        super().setUp()
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        self.lot = self.create()

    def upload(self, content, name="receipt.pdf"):
        return self.client.post(reverse("ledger:api_purchase_attachments", args=[self.lot.pk]),
                                {"file": SimpleUploadedFile(name, content)})

    def test_upload_list_and_delete(self):
        resp = self.upload(PDF)
        self.assertEqual(resp.status_code, 201, resp.content)
        attachment = resp.json()["attachment"]
        self.assertEqual(attachment["name"], "receipt.pdf")
        self.assertTrue(attachment["url"].startswith("/media/purchases/"))
        self.assertNotIn("receipt", attachment["url"])
        stored = self.lot.attachments.get().file.name
        self.assertTrue(default_storage.exists(stored))
        self.assertEqual(self.client.get(attachment["url"]).status_code, 200)
        self.assertEqual([a["id"] for a in self.detail(self.lot)["attachments"]], [attachment["id"]])
        resp = self.client.delete(reverse("ledger:api_purchase_attachment", args=[self.lot.pk, attachment["id"]]))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(default_storage.exists(stored))

    def test_rejects_other_files(self):
        self.assertEqual(self.upload(b"MZ executable", "x.exe").status_code, 400)
        self.assertEqual(self.upload(b"%PDF" + b"0" * (10 * 1024 * 1024)).status_code, 400)
        self.assertEqual(self.upload(b"\x89PNG\r\n\x1a\n...", "photo.png").status_code, 201)

    def test_deleting_the_lot_deletes_files(self):
        self.upload(PDF)
        stored = self.lot.attachments.get().file.name
        self.client.delete(reverse("ledger:api_purchase", args=[self.lot.pk]))
        self.assertFalse(default_storage.exists(stored))


class LotOnItemsTest(PurchaseTestCase):
    def setUp(self):
        super().setUp()
        self.lot = self.create()

    def test_add_put_and_filter(self):
        resp = add(self, SHEOLDRED, lot="pl-0001")
        self.assertEqual(resp.status_code, 201, resp.content)
        item = CollectionItem.objects.get(pk=resp.json()["id"])
        self.assertEqual(item.lot, self.lot)
        other = self.item(SOLITUDE)
        self.assertEqual(add(self, SOLITUDE, lot="PL-9999").status_code, 400)
        self.assertEqual([r["name"] for r in self.collection(lot="PL-0001")["results"]], ["Sheoldred, the Apocalypse"])
        self.assertTrue(self.collection(lot="PL-0001")["filtered"])
        rows = {r["name"]: r["lot"] for r in self.collection()["results"]}
        self.assertEqual(rows, {"Sheoldred, the Apocalypse": "PL-0001", "Solitude": None})
        url = reverse("ledger:api_item", args=[other.pk])
        base = {"condition": "NM", "language": "EN", "quantity": 1}
        self.assertEqual(self.client.put(url, {**base, "lot": "PL-0001"}, content_type="application/json")
                         .status_code, 200)
        other.refresh_from_db()
        self.assertEqual(other.lot, self.lot)
        self.assertEqual(self.client.get(url).json()["lot"], "PL-0001")
        self.client.put(url, base, content_type="application/json")  # absent: unchanged
        other.refresh_from_db()
        self.assertEqual(other.lot, self.lot)
        self.client.put(url, {**base, "lot": ""}, content_type="application/json")  # blank: cleared
        other.refresh_from_db()
        self.assertIsNone(other.lot)

    def test_another_users_lot_is_unknown(self):
        self.client.force_login(self.user)
        theirs = self.create()
        self.client.force_login(self.other)
        resp = add(self, SHEOLDRED, lot=theirs.code)  # same code PL-0001 is self.other's own lot
        self.assertEqual(CollectionItem.objects.get(pk=resp.json()["id"]).lot, self.lot)
        PurchaseLot.objects.filter(pk=self.lot.pk).delete()
        self.assertEqual(add(self, SOLITUDE, lot=theirs.code).status_code, 400)

    def test_scan_session_lot(self):
        url = reverse("ledger:scan_api_sessions")
        resp = self.client.post(url, {"game": "mtg", "lot": "PL-0001"}, content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["session"]["lot"], "PL-0001")
        session = ScanSession.objects.get(uuid=resp.json()["session"]["uuid"])
        self.assertEqual(self.client.post(url, {"lot": "PL-0404"}, content_type="application/json").status_code, 400)
        detail = reverse("ledger:scan_api_session", args=[session.uuid])
        resp = self.client.patch(detail, {"lot": None}, content_type="application/json")
        self.assertEqual((resp.status_code, resp.json()["session"]["lot"]), (200, None))
        resp = self.client.patch(detail, {"lot": "PL-0001"}, content_type="application/json")
        self.assertEqual(resp.json()["session"]["lot"], "PL-0001")
        existing = self.item(COUNTERSPELL)  # an existing stack gets the lot too
        for product in (SHEOLDRED, COUNTERSPELL):
            ScanLine.objects.create(session=session, blueprint=Blueprint.objects.get(cm_product_id=product),
                                    condition="NM", language="EN", quantity=1)
        resp = self.client.post(reverse("ledger:scan_api_save", args=[session.uuid]))
        self.assertEqual(resp.status_code, 200, resp.content)
        lots = set(CollectionItem.objects.owned_by(self.other).filter(card__game=game())
                   .values_list("lot__code", flat=True))
        self.assertEqual(lots, {"PL-0001"})
        existing.refresh_from_db()
        self.assertEqual((existing.quantity, existing.lot), (2, self.lot))
