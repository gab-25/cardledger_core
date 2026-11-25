"""Orders pick/pack/ship and Shipments: stages, deadlines, tabs, picking, packing, issues, pick lists, packing slips,
shipments (statuses, KPIs, tabs, edits) and the sidebar counters."""

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TransactionTestCase
from django.urls import reverse

from accounts.models import Profile
from . import services, shipping
from .models import Card, CollectionItem, Listing, Order, OrderItem, Shipment
from .tests import LedgerTestCase, game

ROME = ZoneInfo("Europe/Rome")
# Wednesday 18 November 2026, 10:00 in Rome
NOW = datetime(2026, 11, 18, 10, 0, tzinfo=ROME)


def at(day, hour=9):
    return datetime(2026, 11, day, hour, 0, tzinfo=ROME)


@contextmanager
def frozen(moment=NOW):
    with mock.patch("django.utils.timezone.now", return_value=moment):
        yield


class FlowTestCase(LedgerTestCase):
    """self.other (no demo data) is logged in; time is frozen at NOW."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.other)
        patcher = mock.patch("django.utils.timezone.now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.n = 0

    def order(self, state="paid", paid_at=None, lines=(("Bolt", 1),), owner=None, market="ct", **fields):
        """An order of owner (default self.other); lines: (name, quantity) or (name, quantity, collection item)."""
        self.n += 1
        order = Order.objects.create(owner=owner or self.other, market=market, market_order_id=str(self.n),
                                     code=f"o{self.n}", state=state, paid_at=paid_at or at(18), **fields)
        for i, line in enumerate(lines):
            name, qty, *item = line
            listing = Listing.objects.get_or_create(item=item[0], market=market, defaults={
                "price": Decimal("1.00"), "status": "sold"})[0] if item else None
            OrderItem.objects.create(order=order, market_item_id=f"{self.n}-{i}", name=name, quantity=qty,
                                     listing=listing, expansion="M10", price=Decimal("2.00"))
        return order

    def item(self, name, location=""):
        card, _ = Card.objects.get_or_create(game=game(), name=name, set_code="M10")
        return CollectionItem.objects.create(owner=self.other, card=card, quantity=3, location=location)

    def get(self, name, *args, **params):
        resp = self.client.get(reverse(f"ledger:{name}", args=args), params)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def call(self, method, name, *args, **body):
        return getattr(self.client, method)(reverse(f"ledger:{name}", args=args), body,
                                            content_type="application/json")


class ShippingHelpersTest(LedgerTestCase):
    def test_working_days_and_due(self):
        friday = date(2026, 11, 13)
        self.assertEqual(shipping.add_working_days(friday, 1), date(2026, 11, 16))
        self.assertEqual(shipping.due_by(at(13), None), date(2026, 11, 17))  # default 2 working days
        self.assertEqual(shipping.due_by(at(13), 4), date(2026, 11, 19))
        self.assertIsNone(shipping.due_by(None, 2))
        # late evening UTC is already the next day in Rome
        self.assertEqual(shipping.due_by(datetime(2026, 11, 15, 23, 30, tzinfo=ZoneInfo("UTC")), 0),
                         date(2026, 11, 16))
        today = date(2026, 11, 18)
        self.assertEqual([shipping.due(d, today) for d in (date(2026, 11, 17), today, date(2026, 11, 19), None)],
                         ["overdue", "today", "ok", None])

    def test_expected_by_and_carrier(self):
        self.assertEqual(shipping.expected_by(at(10), None, True), date(2026, 11, 15))
        self.assertEqual(shipping.expected_by(at(10), None, False), date(2026, 11, 20))
        self.assertEqual(shipping.expected_by(at(10), 3, False), date(2026, 11, 13))
        self.assertEqual(shipping.carrier_for("DHL Express"), "DHL")
        self.assertEqual(shipping.carrier_for("Raccomandata 1"), "Poste Italiane")
        self.assertEqual(shipping.carrier_for("Priority Letter"), "")
        self.assertEqual(shipping.carrier_for("Groups shipping"), "")  # whole words only


class StageTest(FlowTestCase):
    def stage(self, order):
        return Order.objects.with_stage().get(pk=order.pk).stage

    def test_stages(self):
        to_pack = self.order()
        picking = self.order(lines=[("A", 1), ("B", 1)])
        picking.items.filter(name="A").update(picked=True)
        packed = self.order(packed_at=NOW)
        shipped = self.order("sent", sent_at=NOW, packed_at=NOW)
        arrived = self.order("arrived")
        noted = self.order(issue_note="Card damaged", packed_at=NOW)
        lost = self.order("lost")
        rfc = self.order("request_for_cancel")
        canceled_paid = self.order("canceled")
        canceled_unpaid = self.order("canceled")
        Order.objects.filter(pk=canceled_unpaid.pk).update(paid_at=None)
        closed = self.order("closed")
        hub = self.order("hub_pending")
        expected = {to_pack: "to_pack", picking: "picking", packed: "packed", shipped: "shipped",
                    arrived: "shipped", noted: "issue", lost: "issue", rfc: "issue", canceled_paid: "issue",
                    canceled_unpaid: "shipped", closed: "shipped", hub: "to_pack"}
        for order, stage in expected.items():
            with self.subTest(order=order.state, stage=stage):
                self.assertEqual(self.stage(order), stage)


class OrdersListTest(FlowTestCase):
    def test_due_in_rows(self):
        today = self.order(paid_at=at(16))  # Monday + 2 working days = Wednesday
        overdue = self.order(paid_at=at(13))  # Friday + 2 = Tuesday
        ok = self.order(paid_at=at(18))
        longer = self.order(paid_at=at(13), max_shipping_days=4)  # Thursday
        rows = {r["id"]: r for r in self.get("api_orders")["results"]}
        self.assertEqual((rows[today.pk]["due"], rows[today.pk]["due_by"]), ("today", "2026-11-18"))
        self.assertEqual((rows[overdue.pk]["due"], rows[overdue.pk]["due_by"]), ("overdue", "2026-11-17"))
        self.assertEqual(rows[ok.pk]["due"], "ok")
        self.assertEqual((rows[longer.pk]["due"], rows[longer.pk]["due_by"]), ("ok", "2026-11-19"))
        shipped = self.order("sent", sent_at=NOW)
        row = self.get("api_orders", tab="shipped")["results"][0]
        self.assertEqual((row["id"], row["due"], row["due_by"], row["stage"]), (shipped.pk, None, None, "shipped"))

    def test_tabs_counts_and_order(self):
        recent = self.order(paid_at=at(18, 8))
        old = self.order(paid_at=at(17))
        overdue = self.order(paid_at=at(12))
        picking = self.order(paid_at=at(16), lines=[("A", 2), ("B", 1)])
        picking.items.filter(name="A").update(picked=True)
        self.order(packed_at=NOW)
        first_sent = self.order("sent", sent_at=at(16))
        last_sent = self.order("sent", sent_at=at(18, 8))
        self.order("lost")
        self.order("closed")
        self.order(market="cm")
        data = self.get("api_orders")
        self.assertEqual(data["tab"], "to_pack")
        self.assertEqual({t["key"]: t["count"] for t in data["tabs"]},
                         {"to_pack": 5, "packed": 1, "shipped": 2, "issues": 1})
        self.assertEqual([t["label"] for t in data["tabs"]], ["To pack", "Packed", "Shipped", "Issues"])
        ids = [r["id"] for r in data["results"]]
        self.assertEqual(ids[:4], [overdue.pk, picking.pk, old.pk, recent.pk])
        row = next(r for r in data["results"] if r["id"] == picking.pk)
        self.assertEqual((row["stage"], row["picked_count"], row["items_count"]), ("picking", 2, 3))
        self.assertEqual(data["stats"]["shipped_today"], 1)
        shipped = self.get("api_orders", tab="shipped")["results"]
        self.assertEqual([r["id"] for r in shipped], [last_sent.pk, first_sent.pk])
        # the market filter also narrows the counts
        cm = self.get("api_orders", market="cm")
        self.assertEqual({t["key"]: t["count"] for t in cm["tabs"]},
                         {"to_pack": 1, "packed": 0, "shipped": 0, "issues": 0})
        # old keys still work
        self.assertEqual(self.get("api_orders", tab="to_ship")["tab"], "to_pack")
        self.assertEqual(self.get("api_orders", tab="completed")["tab"], "shipped")
        self.assertEqual(self.get("api_orders", tab="nope")["tab"], "to_pack")

    def test_detail_fields_and_next(self):
        first = self.order(paid_at=at(12))
        second = self.order(paid_at=at(17))
        data = self.get("api_order", first.pk)
        self.assertEqual(data["next_id"], second.pk)
        for key in ("stage", "due", "due_by", "picked_count", "items_count", "packed_at", "issue_note"):
            self.assertIn(key, data)
        self.assertFalse(data["items"][0]["picked"])
        self.assertEqual(self.get("api_order", second.pk)["next_id"], first.pk)  # last: back to the first


class PickPackTest(FlowTestCase):
    def test_pick_then_pack(self):
        order = self.order(lines=[("A", 2), ("B", 1)])
        later = self.order(paid_at=at(18, 9) + timedelta(minutes=5))
        a, b = order.items.get(name="A"), order.items.get(name="B")
        resp = self.call("patch", "api_order_item", order.pk, a.pk, picked=True)
        self.assertEqual(resp.json(), {"picked_count": 2, "items_count": 3, "stage": "picking"})
        self.assertEqual(self.call("patch", "api_order_item", order.pk, a.pk).status_code, 400)  # picked required
        # not every line picked
        resp = self.call("post", "api_order_pack", order.pk)
        self.assertEqual((resp.status_code, resp.json()["code"]), (409, "blocked"))
        self.call("patch", "api_order_item", order.pk, b.pk, picked=True)
        resp = self.call("post", "api_order_pack", order.pk)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["next_id"], later.pk)
        self.assertEqual(resp.json()["messages"][0]["level"], "success")
        order.refresh_from_db()
        self.assertIsNotNone(order.packed_at)
        self.assertEqual(Order.objects.with_stage().get(pk=order.pk).stage, "packed")
        # unpack
        self.assertEqual(self.call("post", "api_order_unpack", order.pk).status_code, 200)
        self.assertEqual(Order.objects.with_stage().get(pk=order.pk).stage, "picking")
        self.call("patch", "api_order_item", order.pk, a.pk, picked=False)

    def test_force_and_last(self):
        order = self.order()
        resp = self.call("post", "api_order_pack", order.pk, force=True)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIsNone(resp.json()["next_id"])
        sent = self.order("sent")
        self.assertEqual(self.call("post", "api_order_pack", sent.pk, force=True).status_code, 409)
        self.assertEqual(self.call("post", "api_order_unpack", sent.pk).status_code, 409)

    def test_issue(self):
        order = self.order(packed_at=NOW)
        self.assertEqual(self.call("post", "api_order_issue", order.pk, note=" Wrong card ").status_code, 200)
        order.refresh_from_db()
        self.assertEqual((order.issue_note, order.issue_at), ("Wrong card", NOW))
        self.assertEqual(self.get("api_orders", tab="issues")["results"][0]["issue_note"], "Wrong card")
        self.call("post", "api_order_issue", order.pk, note="")
        order.refresh_from_db()
        self.assertEqual((order.issue_note, order.issue_at), ("", None))
        self.assertEqual(Order.objects.with_stage().get(pk=order.pk).stage, "packed")

    def test_other_users_orders(self):
        mine = self.order(owner=self.user)
        line = mine.items.get()
        for method, name, args in (("patch", "api_order_item", (mine.pk, line.pk)), ("post", "api_order_pack", (mine.pk,)),
                                   ("post", "api_order_unpack", (mine.pk,)), ("post", "api_order_issue", (mine.pk,))):
            with self.subTest(name=name):
                self.assertEqual(self.call(method, name, *args, picked=True, note="x").status_code, 404)
        # a line of another order of mine
        theirs = self.order()
        self.assertEqual(self.call("patch", "api_order_item", theirs.pk, line.pk, picked=True).status_code, 404)
        self.assertEqual(self.get("api_orders_pick_list", ids=f"{mine.pk}")["orders"], [])
        self.assertEqual(self.get("api_orders_slips", ids=f"{mine.pk}")["orders"], [])


class PickListTest(FlowTestCase):
    def test_merged_and_sorted(self):
        bolt, ring = self.item("Bolt", "B2"), self.item("Ring", "A1")
        first = self.order(lines=[("Bolt", 1, bolt), ("Island", 2), ("Ring", 1, ring)])
        second = self.order(lines=[("Bolt", 2, bolt), ("Island", 1), ("Akroma", 1)])
        first.items.filter(name="Bolt").update(picked=True)
        data = self.get("api_orders_pick_list", ids=f"{second.pk},{first.pk}")
        self.assertEqual(data["orders"], [{"id": second.pk, "code": second.code, "market": "ct"},
                                          {"id": first.pk, "code": first.code, "market": "ct"}])
        lines = [(l["location"], l["name"], l["quantity"], l["orders"], l["picked"]) for l in data["lines"]]
        self.assertEqual(lines, [
            ("A1", "Ring", 1, [first.code], False),
            ("B2", "Bolt", 3, [second.code, first.code], False),
            ("", "Akroma", 1, [second.code], False),
            ("", "Island", 3, [second.code, first.code], False),
        ])
        second.items.filter(name="Bolt").update(picked=True)
        bolt_line = next(l for l in self.get("api_orders_pick_list", ids=f"{first.pk},{second.pk}")["lines"]
                         if l["name"] == "Bolt")
        self.assertTrue(bolt_line["picked"])
        self.assertEqual(self.client.get(reverse("ledger:api_orders_pick_list"), {"ids": "1,x"}).status_code, 400)


class SlipsTest(FlowTestCase):
    def test_sender_is_the_shop_name(self):
        Profile.objects.create(user=self.other, shop_name="Gab Cards")
        a, b = self.order(), self.order()
        data = self.get("api_orders_slips", ids=f"{b.pk},{a.pk}")
        self.assertEqual(data["business"], {"shop_name": "Gab Cards"})
        self.assertEqual([o["id"] for o in data["orders"]], [b.pk, a.pk])
        self.assertIn("items", data["orders"][0])
        self.assertNotIn("invoice_number", data["orders"][0])

    def test_without_profile(self):
        order = self.order()
        self.assertEqual(self.get("api_orders_slips", ids=str(order.pk))["business"], {"shop_name": ""})


class ShipmentCreationTest(FlowTestCase):
    def test_record_shipment(self):
        order = self.order(shipping_method="DHL Parcel", tracked=True)
        self.assertIsNone(services.record_shipment(order))  # not shipped yet
        order.state, order.sent_at, order.tracking_code = "sent", at(17), "JD1"
        order.save()
        shipment = services.record_shipment(order)
        self.assertEqual((shipment.carrier, shipment.tracking_code, shipment.shipped_at, shipment.expected_by),
                         ("DHL", "JD1", at(17), date(2026, 11, 22)))
        shipment.carrier = "UPS"
        shipment.save()
        order.state = "arrived"
        services.record_shipment(order)
        shipment.refresh_from_db()
        self.assertEqual((shipment.carrier, shipment.delivered_at), ("UPS", NOW))  # the edit stays

    def test_extension_creates_shipments(self):
        from .test_extension import ExtensionTestCase

        self.client.force_login(self.user)
        token = ExtensionTestCase.pair_and_claim(self)
        ext = Client(HTTP_AUTHORIZATION=f"Token {token}")
        paid = Order.objects.owned_by(self.user).filter(market="ct", state="paid").first()
        resp = ext.post(reverse("ledger:api_extension_shipped"),
                        {"market": "ct", "market_order_id": paid.market_order_id, "tracking_code": "RR9IT"},
                        content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        shipment = Shipment.objects.get(order=paid)
        self.assertEqual((shipment.tracking_code, shipment.shipped_at), ("RR9IT", NOW))
        # an order reported already sent
        services.save_order(self.user, "cm", "X1", {"code": "X1", "state": "sent", "sent_at": at(16),
                                                    "shipping_method": "Lettre suivie"}, [])
        shipment = Shipment.objects.get(order__market_order_id="X1")
        self.assertEqual((shipment.carrier, shipment.shipped_at, shipment.expected_by),
                         ("La Poste", at(16), date(2026, 11, 26)))
        # the demo's sent order has one too (load_demo)
        self.assertTrue(Shipment.objects.filter(order__owner=self.user, order__market_order_id="9000004").exists())


class ShipmentsApiTest(FlowTestCase):
    def ship(self, sent_day, *, tracked=True, tracking="", market="ct", total="10.00", **fields):
        order = self.order("sent", sent_at=at(sent_day), tracked=tracked, tracking_code=tracking, market=market,
                           total=Decimal(total))
        shipment = services.record_shipment(order)
        for name, value in fields.items():
            setattr(shipment, name, value)
        shipment.save()
        return shipment

    def test_statuses_tabs_and_kpis(self):
        on_way = self.ship(16, tracking="T1")  # expected 21
        late = self.ship(10, tracking="T2", total="30.00")  # expected 15
        untracked = self.ship(16, tracked=False, market="cm")  # expected 26
        untracked_late = self.ship(1, tracked=False, total="5.00")  # expected 11
        on_time = self.ship(10, tracking="T3", delivered_at=at(14), postage_cost=Decimal("2.00"))
        tardy = self.ship(5, tracking="T4", delivered_at=at(12), postage_cost=Decimal("4.50"))  # expected 10
        early = self.ship(12, tracking="T7", delivered_at=at(15))  # expected 17
        self.ship(9, tracking="T5", returned_at=at(17))
        arrived = self.ship(15, tracking="T6")
        # delivered on the market, date unknown: delivered, but not in delivered_30d / on_time_pct
        Order.objects.filter(pk=arrived.order_id).update(state="arrived")
        data = self.get("api_shipments")
        statuses = {r["id"]: r["status"] for r in data["results"]}
        self.assertEqual(data["tab"], "in_transit")
        self.assertEqual(statuses, {on_way.pk: "in_transit", late.pk: "late", untracked.pk: "no_tracking",
                                    untracked_late.pk: "no_tracking"})
        self.assertEqual([r["id"] for r in data["results"]],
                         [untracked.pk, on_way.pk, late.pk, untracked_late.pk])  # newest first (then newest id)
        self.assertEqual({t["key"]: t["count"] for t in data["tabs"]},
                         {"in_transit": 4, "delivered": 4, "problems": 2, "returned": 1})
        self.assertEqual({r["id"] for r in self.get("api_shipments", tab="problems")["results"]},
                         {late.pk, untracked_late.pk})
        self.assertEqual({r["id"]: r["status"] for r in self.get("api_shipments", tab="delivered")["results"]},
                         {on_time.pk: "delivered", tardy.pk: "delivered", early.pk: "delivered",
                          arrived.pk: "delivered"})
        self.assertEqual([r["status"] for r in self.get("api_shipments", tab="returned")["results"]], ["returned"])
        row = next(r for r in data["results"] if r["id"] == on_way.pk)
        self.assertEqual((row["order_id"], row["order_code"], row["expected_by"], row["days_since_shipped"],
                          row["tracked"], row["total"], row["postage_cost"], row["note"], row["returned_at"]),
                         (on_way.order_id, on_way.order.code, "2026-11-21", 2, True, "10.00", None, "", None))
        self.assertFalse(next(r for r in data["results"] if r["id"] == untracked.pk)["tracked"])
        self.assertEqual(data["kpis"], {
            "in_transit": 4, "in_transit_by_market": {"cm": 1, "ct": 3}, "delivered_30d": 3,
            "on_time_pct": "66.7", "problems": 2, "value_at_risk": "35.00", "postage_30d": "6.50",
            "postage_per_order": "3.25",
        })
        self.assertEqual((data["count"], data["page"], data["num_pages"]), (4, 1, 1))
        cm = self.get("api_shipments", market="cm")
        self.assertEqual((cm["market"], cm["kpis"]["in_transit"], cm["kpis"]["in_transit_by_market"]),
                         ("cm", 1, {"cm": 1, "ct": 3}))
        self.assertEqual(self.get("api_shipments", tab="nope")["tab"], "in_transit")

    def test_empty_kpis(self):
        kpis = self.get("api_shipments")["kpis"]
        self.assertEqual((kpis["on_time_pct"], kpis["value_at_risk"], kpis["postage_30d"], kpis["postage_per_order"]),
                         (None, None, None, None))

    def test_patch(self):
        shipment = self.ship(16, tracking="T1", carrier="DHL")
        resp = self.call("patch", "api_shipment", shipment.pk, postage_cost="3.20", note="Fragile")
        self.assertEqual(resp.status_code, 200, resp.content)
        shipment.refresh_from_db()
        self.assertEqual((shipment.carrier, shipment.tracking_code, shipment.postage_cost, shipment.note),
                         ("DHL", "T1", Decimal("3.20"), "Fragile"))  # left out: kept
        self.call("patch", "api_shipment", shipment.pk, delivered=True)
        shipment.refresh_from_db()
        self.assertEqual(shipment.delivered_at, NOW)
        self.call("patch", "api_shipment", shipment.pk, returned=True)
        shipment.refresh_from_db()
        self.assertEqual(Shipment.objects.with_status(NOW.date()).get(pk=shipment.pk).status, "returned")
        self.call("patch", "api_shipment", shipment.pk, delivered=True)
        shipment.refresh_from_db()
        self.assertIsNone(shipment.returned_at)
        self.call("patch", "api_shipment", shipment.pk, delivered=False, carrier="UPS", tracking_code="U9")
        shipment.refresh_from_db()
        self.assertEqual((shipment.delivered_at, shipment.carrier, shipment.tracking_code), (None, "UPS", "U9"))
        self.assertEqual(self.call("patch", "api_shipment", shipment.pk, postage_cost="-1").status_code, 400)
        # another user's shipment
        theirs = services.record_shipment(self.order("sent", owner=self.user))
        self.assertEqual(self.call("patch", "api_shipment", theirs.pk, note="x").status_code, 404)
        self.assertEqual(self.get("api_shipments")["kpis"]["in_transit"], 1)


class MeCountsTest(FlowTestCase):
    def test_counts(self):
        self.order()
        picking = self.order(market="cm")
        picking.items.update(picked=True)
        self.order(packed_at=NOW)
        late = self.order("sent", sent_at=at(1), tracking_code="T")
        services.record_shipment(late)
        services.record_shipment(self.order("sent", sent_at=at(17), tracking_code="T"))
        counts = self.client.get(reverse("accounts:api_me")).json()["counts"]
        self.assertEqual((counts["orders_to_pack"], counts["shipment_problems"]), (2, 1))


class ShipmentBackfillTest(TransactionTestCase):
    BEFORE = [("ledger", "0042_orders_packing_shipments"), ("auth", "0012_alter_user_first_name_max_length")]
    AFTER = [("ledger", "0043_backfill_shipments"), ("auth", "0012_alter_user_first_name_max_length")]

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_sent_orders_get_a_shipment(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.BEFORE)
        get = executor.loader.project_state(self.BEFORE).apps.get_model
        gab = get("auth", "User").objects.create(username="gab")
        Order = get("ledger", "Order")
        sent = Order.objects.create(owner=gab, market_order_id="1", code="1", state="sent", sent_at=at(10),
                                    tracking_code="RR1", shipping_method="Raccomandata")
        arrived = Order.objects.create(owner=gab, market_order_id="2", code="2", state="arrived", sent_at=at(5),
                                       max_shipping_days=3)
        Order.objects.create(owner=gab, market_order_id="3", code="3", state="paid")

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.AFTER)
        Shipment = executor.loader.project_state(self.AFTER).apps.get_model("ledger", "Shipment")
        self.assertEqual(Shipment.objects.count(), 2)
        s = Shipment.objects.get(order_id=sent.pk)
        self.assertEqual((s.carrier, s.tracking_code, s.shipped_at, s.expected_by, s.delivered_at),
                         ("Poste Italiane", "RR1", at(10), date(2026, 11, 15), None))
        s = Shipment.objects.get(order_id=arrived.pk)
        self.assertEqual((s.expected_by, s.delivered_at is not None), (date(2026, 11, 8), True))
