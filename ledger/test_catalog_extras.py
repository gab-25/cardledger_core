"""Catalog extras (rarity, year, sold_30d, owned_locations, filters, ordering, rarities, suggestion, report missing)
and the seller settings in the bulk actions and the add form."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from accounts.models import SellerSettings

from . import services
from .cardtrader import CardTraderError
from .models import Blueprint, CollectionItem, Listing, MissingCardReport, Order, OrderItem
from .test_catalog_api import catalog
from .test_dealer_api import COUNTERSPELL, SHEOLDRED, DealerTestCase
from .tests import LedgerTestCase, add


class CatalogExtrasTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.ugin = catalog()
        Blueprint.objects.filter(cm_product_id=9001).update(rarity="Mythic")
        Blueprint.objects.filter(cm_product_id__in=[9007, 9045]).update(rarity="Uncommon")
        self.client.force_login(self.other)

    def search(self, **params):
        resp = self.client.get(reverse("ledger:api_catalog"), params)
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def row(self, product, **params):
        return next(r for r in self.search(**params)["results"] if r["ct_id"] == product)

    def test_rarity_year_and_rarities(self):
        data = self.search()
        self.assertEqual(data["rarities"], ["Mythic", "Uncommon"])
        ugin = self.row(9001)
        self.assertEqual((ugin["rarity"], ugin["year"]), ("Mythic", 2020))
        self.assertEqual((ugin["sold_30d"], ugin["owned_locations"]), (0, []))

    def test_rarity_filter(self):
        data = self.search(rarity="Uncommon")
        self.assertTrue(data["searched"])
        self.assertEqual(sorted(r["name"] for r in data["results"]), ["Basri Ket", "Counterspell"])
        self.assertEqual(data["filters"]["rarity"], "Uncommon")

    def test_foil_filter(self):
        self.assertEqual([r["ct_id"] for r in self.search(finish="foil")["results"]], [9001])

    def test_ordering(self):
        names = lambda **p: [r["name"] for r in self.search(category=1, **p)["results"]]  # noqa: E731
        self.assertEqual(names(ordering="-trend"), ["Ugin, the Spirit Dragon", "Basri Ket", "Counterspell"])
        self.assertEqual(names(ordering="trend"), ["Basri Ket", "Ugin, the Spirit Dragon", "Counterspell"])
        self.assertEqual(names(ordering="name"), ["Basri Ket", "Counterspell", "Ugin, the Spirit Dragon"])
        self.assertEqual(names(ordering="year"), ["Counterspell", "Basri Ket", "Ugin, the Spirit Dragon"])
        self.assertEqual(self.search(ordering="bogus")["filters"]["ordering"], "")

    def test_owned_locations_are_the_users(self):
        for location, foil in (("B1", False), ("A3", True), ("", False)):
            item = CollectionItem.objects.get(pk=add(self, 9001, foil=foil, condition="EX" if location == "" else "NM")
                                              .json()["id"])
            CollectionItem.objects.filter(pk=item.pk).update(location=location)
        self.assertEqual(self.row(9001)["owned_locations"], ["A3", "B1"])
        self.client.force_login(self.user)
        self.assertEqual(self.row(9001)["owned_locations"], [])

    def test_sold_30d(self):
        item = CollectionItem.objects.get(pk=add(self, 9001, quantity=5).json()["id"])
        listing = Listing.objects.create(item=item, price="10.00", quantity=5, status=Listing.Status.PUBLISHED)

        def sold(ct_id, days_ago, qty, state="paid", owner=None):
            order = Order.objects.create(owner=owner or self.other, ct_id=ct_id, code=f"o{ct_id}", state=state,
                                         paid_at=timezone.now() - timedelta(days=days_ago))
            OrderItem.objects.create(order=order, ct_id=ct_id, listing=listing, name="Ugin", quantity=qty)

        sold(1, 2, 2)
        sold(2, 10, 1)
        sold(3, 40, 4)  # too old
        sold(4, 1, 3, state="canceled")
        self.assertEqual(self.row(9001)["sold_30d"], 3)
        self.assertEqual(self.row(9007)["sold_30d"], 0)
        self.client.force_login(self.user)  # another user's orders are theirs
        self.assertEqual(self.row(9001)["sold_30d"], 0)

    def test_suggestion(self):
        data = self.search(q="Ugin Spirit Dragon qqqq")  # a word matching nothing
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["suggestion"], "Ugin, the Spirit Dragon")
        self.assertIsNone(self.search(q="zzzzqqq")["suggestion"])
        self.assertIsNone(self.search(q="ugin")["suggestion"])  # results: no suggestion
        self.assertIsNone(self.search(q="ugin", game="pokemon")["suggestion"])  # only the game's names

    def test_report_missing(self):
        url = reverse("ledger:api_catalog_report_missing")
        resp = self.client.post(f"{url}?game=pokemon", {"q": "Pikachu VMAX", "note": "Japanese promo"},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["messages"][0]["level"], "success")
        report = MissingCardReport.objects.get()
        self.assertEqual((report.user, report.game.slug, report.q, report.note),
                         (self.other, "pokemon", "Pikachu VMAX", "Japanese promo"))
        self.assertIn("q", self.client.post(url, {"note": "x"}, content_type="application/json").json())
        self.assertEqual(self.client.post(f"{url}?game=chess", {"q": "x"}, content_type="application/json")
                         .status_code, 400)

    def test_report_missing_is_in_the_admin(self):
        self.client.force_login(self.user)  # the superuser
        self.assertEqual(self.client.get(reverse("admin:ledger_missingcardreport_changelist")).status_code, 200)


class SellerSettingsInUseTest(DealerTestCase):
    def setUp(self):
        super().setUp()
        self.guide(SHEOLDRED, trend=Decimal("10.00"))
        self.guide(COUNTERSPELL, trend=Decimal("3.33"))
        self.sheoldred = self.item(SHEOLDRED, cost=Decimal("9.80"))
        self.counterspell = self.item(COUNTERSPELL, cost=Decimal("1.00"))

    def bulk(self, action="list", **fields):
        return self.client.post(reverse("ledger:api_collection_bulk"),
                                {"ids": [self.sheoldred.pk, self.counterspell.pk], "action": action, **fields},
                                content_type="application/json")

    def test_hold_below_cost(self):
        SellerSettings.objects.create(user=self.other, hold_below_cost=True)
        resp = self.bulk(pct="-5")  # sheoldred 9.50 < cost 9.80
        self.assertEqual(resp.json()["messages"], [
            {"level": "success", "text": "Listed 1 item at trend -5%."},
            {"level": "warning", "text": "Held 1 item whose price would be below cost."},
        ])
        self.assertFalse(Listing.objects.filter(item=self.sheoldred).exists())
        self.assertEqual(Listing.objects.get(item=self.counterspell).price, Decimal("3.15"))

    def test_without_hold_below_cost_lists_everything(self):
        self.assertEqual(self.bulk(pct="-5").json()["count"], 2)

    def test_price_rounding(self):
        SellerSettings.objects.create(user=self.other, price_rounding="0.50")
        self.bulk()
        self.assertEqual(Listing.objects.get(item=self.counterspell).price, Decimal("3.50"))  # 3.33 → 3.50
        self.assertEqual(Listing.objects.get(item=self.sheoldred).price, Decimal("10.00"))

    def test_default_rounding_is_005(self):
        self.bulk()
        self.assertEqual(Listing.objects.get(item=self.counterspell).price, Decimal("3.35"))

    def test_default_condition_when_adding(self):
        SellerSettings.objects.create(user=self.other, default_condition="EX")
        resp = self.client.post(reverse("ledger:api_collection"), {"blueprint": SHEOLDRED, "quantity": 1},
                                content_type="application/json")
        self.assertEqual(CollectionItem.objects.get(pk=resp.json()["id"]).condition, "EX")
        resp = add(self, SHEOLDRED, condition="GD")  # an explicit condition wins
        self.assertEqual(CollectionItem.objects.get(pk=resp.json()["id"]).condition, "GD")


class PublishFailureLogTest(LedgerTestCase):
    def test_failed_publish_is_logged_as_error(self):
        with mock.patch.object(services, "resolve_blueprints", side_effect=CardTraderError("down")):
            self.assertEqual(self.publish().status_code, 502)
        entry = self.user.sync_logs.first()
        self.assertEqual((entry.level, entry.message), ("error", "CardTrader: publish failed (down)"))

