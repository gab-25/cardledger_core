"""Price snapshots (catalog import, change_30d, history) and stock value snapshots (command, value_week_ago)."""

from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from . import snapshots
from .models import PriceSnapshot, StockValueSnapshot
from .test_catalog_sync import CatalogSyncTestCase
from .test_dealer_api import COUNTERSPELL, SHEOLDRED, SOLITUDE, DealerTestCase
from .tests import cm_id, game


class PriceSnapshotTest(DealerTestCase):
    def rows(self, product=SHEOLDRED):
        return list(PriceSnapshot.objects.filter(cm_product_id=product).order_by("date")
                    .values_list("date", "trend", "trend_foil"))

    def test_one_row_per_change(self):
        d1, d2, d3 = date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)
        self.guide(SHEOLDRED, trend=Decimal("10.00"), trend_foil=Decimal("20.00"))
        snapshots.snapshot_prices(game(), d1)
        snapshots.snapshot_prices(game(), d2)  # unchanged: no row
        self.guide(SHEOLDRED, trend=Decimal("11.00"), trend_foil=Decimal("20.00"))
        snapshots.snapshot_prices(game(), d3)
        self.guide(SHEOLDRED, trend=Decimal("12.00"), trend_foil=Decimal("20.00"))
        snapshots.snapshot_prices(game(), d3)  # same day: overwritten
        self.assertEqual(self.rows(), [(d1, Decimal("10.00"), Decimal("20.00")),
                                       (d3, Decimal("12.00"), Decimal("20.00"))])
        self.assertEqual(self.rows(700001), [])

    def test_prune_keeps_the_value_at_the_cutoff(self):
        today = timezone.localdate()
        for days, trend in ((500, "1.00"), (450, "2.00"), (10, "3.00")):
            PriceSnapshot.objects.create(cm_product_id=SOLITUDE, date=today - timedelta(days=days), trend=Decimal(trend))
        snapshots.prune_prices(today)
        self.assertEqual([r[1] for r in self.rows(SOLITUDE)], [Decimal("2.00"), Decimal("3.00")])

    def test_catalog_change_30d(self):
        old = timezone.localdate() - timedelta(days=35)
        self.guide(SHEOLDRED, trend=Decimal("10.00"), trend_foil=Decimal("30.00"))
        self.guide(COUNTERSPELL, trend=Decimal("1.00"))
        PriceSnapshot.objects.create(cm_product_id=SHEOLDRED, date=old, trend=Decimal("8.00"), trend_foil=Decimal("40.00"))
        PriceSnapshot.objects.create(cm_product_id=SHEOLDRED, date=timezone.localdate() - timedelta(days=5),
                                     trend=Decimal("9.00"))  # after the 30-day mark: not the base

        def change(**params):
            rows = self.client.get(reverse("ledger:api_catalog"), params).json()["results"]
            return {r["ct_id"]: r["change_30d"] for r in rows}

        rows = change(set="dmu")
        self.assertEqual((rows[SHEOLDRED], rows[cm_id(1390)]), ("+25.0", None))
        self.assertEqual(change(set="dmu", finish="foil")[SHEOLDRED], "-25.0")

    def test_history(self):
        today = timezone.localdate()
        for days, trend in ((100, "5.00"), (50, "6.00"), (10, "7.00")):
            PriceSnapshot.objects.create(cm_product_id=SHEOLDRED, date=today - timedelta(days=days),
                                         trend=Decimal(trend), trend_foil=None)
        url = reverse("ledger:api_catalog_history", args=[SHEOLDRED])
        points = self.client.get(url).json()["points"]
        self.assertEqual(points, [
            {"date": str(today - timedelta(days=90)), "trend": "5.00", "trend_foil": None},
            {"date": str(today - timedelta(days=50)), "trend": "6.00", "trend_foil": None},
            {"date": str(today - timedelta(days=10)), "trend": "7.00", "trend_foil": None},
        ])
        self.assertEqual(len(self.client.get(url, {"days": "20"}).json()["points"]), 2)
        self.assertEqual(len(self.client.get(url, {"days": "9999"}).json()["points"]), 3)
        self.assertEqual(self.client.get(reverse("ledger:api_catalog_history", args=[123])).status_code, 404)


class CatalogSyncSnapshotTest(CatalogSyncTestCase):
    def test_sync_writes_snapshots_dated_by_the_guide(self):
        self.reports = []
        self.run_sync()
        days = set(PriceSnapshot.objects.values_list("date", flat=True))
        self.assertEqual(days, {date(2026, 11, 14)})
        n = PriceSnapshot.objects.count()
        self.assertGreater(n, 0)
        self.run_sync()  # same prices: nothing new
        self.assertEqual(PriceSnapshot.objects.count(), n)
        self.assertTrue(StockValueSnapshot.objects.filter(date=timezone.localdate()).exists())


class StockValueSnapshotTest(DealerTestCase):
    def test_command_and_value_week_ago(self):
        self.guide(SHEOLDRED, trend=Decimal("10.00"))
        self.item(SHEOLDRED, quantity=3)
        out = StringIO()
        call_command("snapshot_stock_values", stdout=out)
        self.assertIn("snapshots written", out.getvalue())
        today = timezone.localdate()
        snap = StockValueSnapshot.objects.get(user=self.other, game=game())
        self.assertEqual((snap.date, snap.value), (today, Decimal("30.00")))
        call_command("snapshot_stock_values", stdout=StringIO())  # rerun: overwritten
        self.assertEqual(StockValueSnapshot.objects.filter(user=self.other).count(), 1)
        self.assertIsNone(self.collection()["stats"]["value_week_ago"])
        StockValueSnapshot.objects.create(user=self.other, game=game(), date=today - timedelta(days=8),
                                          value=Decimal("25.00"))
        StockValueSnapshot.objects.create(user=self.other, game=game(), date=today - timedelta(days=20),
                                          value=Decimal("5.00"))
        self.assertEqual(self.collection()["stats"]["value_week_ago"], "25.00")
        self.assertIsNone(self.collection(game="pokemon")["stats"]["value_week_ago"])
