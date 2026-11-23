"""The repricing API: rules, previews, runs, quotes, ad-hoc reprices, the plan hook and listing new copies."""

import json
from datetime import timedelta
from decimal import Decimal as D

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import SellerSettings
from .models import Blueprint, CollectionItem, Listing, RepricingChange, RepricingRule, RepricingRun
from .test_dealer_api import COUNTERSPELL, RAGAVAN, SHEOLDRED, SOLITUDE, DealerTestCase
from .tests import cm_id

BOX = cm_id(1390)


class RepricingTestCase(DealerTestCase):
    """self.other (logged in) with an empty collection over the test catalog and its price guide."""

    def setUp(self):
        super().setUp()
        self.guide(SHEOLDRED, trend=D("10.00"), low=D("8.00"), avg7=D("9.00"), avg30=D("9.50"),
                   trend_foil=D("30.00"), low_foil=D("25.00"))
        self.guide(COUNTERSPELL, trend=D("3.33"), low=D("2.00"))
        self.guide(SOLITUDE, trend=D("20.00"))
        self.guide(BOX, trend=D("100.00"), trend_foil=D("0.00"))
        Blueprint.objects.filter(cm_product_id=SHEOLDRED).update(rarity="Mythic")

    def listing(self, item, market="ct", price="12.00", status="published", **fields):
        return Listing.objects.create(item=item, market=market, price=D(price), status=status,
                                      quantity=fields.pop("quantity", 1), **fields)

    def make_rule(self, name="Trend -10", user=None, **fields):
        fields = {"kind": "price", "reference": "trend", "adjust_pct": D("-10"), **fields}
        position = (RepricingRule.objects.owned_by(user or self.other).count() + 1)
        return RepricingRule.objects.create(user=user or self.other, name=name, position=position, **fields)

    def post(self, name, data=None, **kwargs):
        return self.client.post(reverse(f"ledger:{name}", kwargs=kwargs), data or {}, content_type="application/json")

    def get(self, name, params=None, **kwargs):
        resp = self.client.get(reverse(f"ledger:{name}", kwargs=kwargs), params or {})
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def settings(self, **fields):
        SellerSettings.objects.update_or_create(user=self.other, defaults=fields)


class RulesApiTest(RepricingTestCase):
    def rules(self):
        return self.get("api_repricing_rules")["results"]

    def test_create_list_and_stats(self):
        item = self.item(SHEOLDRED)
        self.listing(item)
        resp = self.post("api_repricing_rules", {
            "name": "Mythics", "kind": "price", "markets": "all", "reference": "trend", "adjust_pct": "-5",
            "adjust_abs": "0.10", "round_to": "0.10",
            "conditions": [{"field": "rarity", "op": "in", "value": ["Mythic"]},
                           {"field": "trend", "op": "between", "value": ["1", "50"]}],
        })
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["messages"], [{"level": "success", "text": "Rule Mythics created."}])
        [rule] = self.rules()
        self.assertEqual({k: rule[k] for k in ("id", "name", "position", "kind", "markets", "reference", "adjust_pct",
                                               "adjust_abs", "round_to", "interval_days", "enabled", "draft")},
                         {"id": resp.json()["id"], "name": "Mythics", "position": 1, "kind": "price", "markets": "all",
                          "reference": "trend", "adjust_pct": "-5.00", "adjust_abs": "0.10", "round_to": "0.10",
                          "interval_days": None, "enabled": True, "draft": False})
        self.assertEqual(rule["conditions"], [{"field": "rarity", "op": "in", "value": ["Mythic"]},
                                              {"field": "trend", "op": "between", "value": ["1", "50"]}])
        self.assertEqual(rule["stats"], {"matched": 1, "last_moved": None, "last_blocked": None})
        self.assertIn("created_at", rule)
        self.assertIn("updated_at", rule)

    def test_position(self):
        a, b = self.make_rule("a"), self.make_rule("b")
        resp = self.post("api_repricing_rules", {"name": "first", "position": 1})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.post("api_repricing_rules", {"name": "last"})
        self.assertEqual([(r["name"], r["position"]) for r in self.rules()],
                         [("first", 1), ("a", 2), ("b", 3), ("last", 4)])

    def test_patch_keeps_absent_fields_and_moves(self):
        a, b = self.make_rule("a", round_to="0.50"), self.make_rule("b")
        url = reverse("ledger:api_repricing_rule", args=[a.pk])
        resp = self.client.patch(url, {"enabled": False, "position": 2}, content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        a.refresh_from_db()
        self.assertEqual((a.enabled, a.round_to, a.adjust_pct, a.position), (False, "0.50", D("-10"), 2))
        self.assertEqual([r["name"] for r in self.rules()], ["b", "a"])

    def test_delete_renumbers(self):
        a, b, c = self.make_rule("a"), self.make_rule("b"), self.make_rule("c")
        resp = self.client.delete(reverse("ledger:api_repricing_rule", args=[b.pk]))
        self.assertEqual(resp.json()["messages"][0]["text"], "Rule b deleted.")
        self.assertEqual([(r["name"], r["position"]) for r in self.rules()], [("a", 1), ("c", 2)])

    def test_reorder(self):
        a, b, c = self.make_rule("a"), self.make_rule("b"), self.make_rule("c")
        self.assertEqual(self.post("api_repricing_reorder", {"ids": [c.pk, a.pk]}).status_code, 200)
        self.assertEqual([r["name"] for r in self.rules()], ["c", "a", "b"])
        mine = self.make_rule("gab's", user=self.user)
        self.assertEqual(self.post("api_repricing_reorder", {"ids": [mine.pk]}).status_code, 400)
        self.assertEqual(self.post("api_repricing_reorder", {"ids": [a.pk, a.pk]}).status_code, 400)
        self.assertEqual(self.post("api_repricing_reorder", {"ids": "x"}).status_code, 400)

    def test_over_time_needs_interval_and_uses_current(self):
        resp = self.post("api_repricing_rules", {"name": "drop", "kind": "over_time", "adjust_pct": "-5"})
        self.assertEqual(resp.json(), {"interval_days": ["An over-time rule needs its interval in days."]})
        resp = self.post("api_repricing_rules", {"name": "drop", "kind": "over_time", "adjust_pct": "-5",
                                                 "interval_days": 7, "reference": "trend"})
        rule = RepricingRule.objects.get(pk=resp.json()["id"])
        self.assertEqual((rule.reference, rule.interval_days), ("current", 7))
        resp = self.post("api_repricing_rules", {"name": "p", "interval_days": 7})
        self.assertIsNone(RepricingRule.objects.get(pk=resp.json()["id"]).interval_days)

    def test_condition_validation(self):
        bad = [
            "x", [{"field": "color", "op": "in", "value": ["R"]}], [{"field": "foil", "op": "in", "value": ["1"]}],
            [{"field": "foil", "op": "eq", "value": "yes"}], [{"field": "set", "op": "in", "value": []}],
            [{"field": "trend", "op": "gte", "value": "abc"}], [{"field": "trend", "op": "between", "value": ["5", "1"]}],
            [{"field": "listed_days", "op": "between", "value": ["1", "5"]}],
            [{"field": "game", "op": "in", "value": ["mtg"], "extra": 1}],
        ]
        for conditions in bad:
            resp = self.post("api_repricing_rules", {"name": "x", "conditions": conditions})
            self.assertEqual(resp.status_code, 400, conditions)
            self.assertIn("conditions", resp.json())
        self.assertIn("round_to", self.post("api_repricing_rules", {"name": "x", "round_to": "0.07"}).json())
        self.assertIn("name", self.post("api_repricing_rules", {"kind": "price"}).json())
        resp = self.post("api_repricing_rules", {"name": "ok", "conditions": [
            {"field": "listed_days", "op": "gte", "value": 30}, {"field": "foil", "op": "eq", "value": True}]})
        self.assertEqual(RepricingRule.objects.get(pk=resp.json()["id"]).conditions,
                         [{"field": "listed_days", "op": "gte", "value": "30"},
                          {"field": "foil", "op": "eq", "value": True}])

    def test_other_users_rules(self):
        mine = self.make_rule("gab's", user=self.user)
        url = reverse("ledger:api_repricing_rule", args=[mine.pk])
        self.assertEqual(self.client.patch(url, {"name": "x"}, content_type="application/json").status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)
        self.assertEqual(self.rules(), [])
        self.assertEqual(self.post("api_repricing_rule_preview", {"id": mine.pk, "name": "x"}).status_code, 404)

    def test_stats_after_a_run(self):
        a = self.make_rule("cheap", adjust_pct=D("-50"))
        floor = self.make_rule("floor", kind="floor", reference="low", adjust_pct=D("0"))
        self.listing(self.item(SHEOLDRED))  # 12.00 → 5.00, held at low 8.00
        self.listing(self.item(SOLITUDE), price="10.00")  # → 10.00: unchanged
        self.post("api_repricing_run")
        stats = {r["name"]: r["stats"] for r in self.rules()}
        self.assertEqual(stats["cheap"], {"matched": 2, "last_moved": 1, "last_blocked": 0})
        self.assertEqual(stats["floor"], {"matched": 2, "last_moved": 1, "last_blocked": 1})


class RulePreviewTest(RepricingTestCase):
    def test_counts(self):
        self.listing(self.item(SHEOLDRED), price="12.00")  # trend 10 → 9.00: down
        self.listing(self.item(COUNTERSPELL), price="1.00")  # trend 3.33 → 3.00: up
        self.listing(self.item(SOLITUDE), price="18.00")  # trend 20 → 18.00: no move
        self.listing(self.item(RAGAVAN), price="5.00")  # no trend: not priced
        body = {"name": "", "kind": "price", "reference": "trend", "adjust_pct": "-10", "enabled": False}
        self.assertEqual(self.post("api_repricing_rule_preview", body).json(),
                         {"matched": 4, "moves": 2, "up": 1, "down": 1})
        self.assertFalse(RepricingRule.objects.exists())

    def test_at_its_position(self):
        self.listing(self.item(SHEOLDRED), price="12.00")
        first = self.make_rule("first", adjust_pct=D("20"))  # 12.00: no move
        body = {"name": "new", "reference": "trend", "adjust_pct": "-10"}
        self.assertEqual(self.post("api_repricing_rule_preview", body).json()["moves"], 0)  # last: first wins
        self.assertEqual(self.post("api_repricing_rule_preview", {**body, "position": 1}).json()["moves"], 1)
        # replacing the saved rule (at its position)
        self.assertEqual(self.post("api_repricing_rule_preview", {**body, "id": first.pk}).json()["moves"], 1)

    def test_invalid(self):
        self.assertEqual(self.post("api_repricing_rule_preview", {"kind": "boom"}).status_code, 400)


class PreviewAndRunTest(RepricingTestCase):
    def setUp(self):
        super().setUp()
        self.sheoldred = self.item(SHEOLDRED, quantity=2)
        self.cm = self.listing(self.sheoldred, "cm", "12.00", "published", quantity=2, market_listing_id="77")
        self.ct = self.listing(self.sheoldred, "ct", "12.00", "to_publish")
        self.counterspell = self.item(COUNTERSPELL)
        self.paused = self.listing(self.counterspell, "ct", "1.00", "paused")
        solitude = self.item(SOLITUDE, quantity=2)
        self.error = self.listing(solitude, "ct", "5.00", "error")
        self.sold = self.listing(solitude, "cm", "5.00", "sold")
        self.rule = self.make_rule()

    def test_preview(self):
        data = self.get("api_repricing_preview")
        self.assertEqual(data["totals"], {"changes": 3, "raised": 1, "lowered": 2, "value_delta": "-7.00",
                                          "blocked": 0, "by_market": {"cm": 1, "ct": 2}})
        # cm: (9 - 12) × 2, ct: -3 × 1, counterspell: +2
        self.assertEqual(data["biggest"][0], {"listing_id": self.cm.pk, "name": "Sheoldred, the Apocalypse",
                                              "set_code": "DMU", "condition": "NM", "foil": False, "market": "cm",
                                              "from": "12.00", "to": "9.00"})
        self.assertEqual(len(data["biggest"]), 3)
        self.assertEqual((data["count"], data["page"], data["num_pages"]), (3, 1, 1))
        row = next(r for r in data["results"] if r["listing_id"] == self.paused.pk)
        self.assertEqual(row, {"listing_id": self.paused.pk, "item_id": self.counterspell.pk, "name": "Counterspell",
                               "set_code": "DMR", "condition": "NM", "language": "EN", "foil": False, "market": "ct",
                               "from": "1.00", "to": "3.00", "rule": "Trend -10", "floor_rule": None,
                               "blocked_by_floor": False})
        cm_only = self.get("api_repricing_preview", {"market": "cm"})
        self.assertEqual([r["listing_id"] for r in cm_only["results"]], [self.cm.pk])
        self.assertEqual(cm_only["totals"]["changes"], 3)
        self.assertEqual(self.client.get(reverse("ledger:api_repricing_preview"), {"market": "xx"}).status_code, 400)
        self.assertEqual(Listing.objects.get(pk=self.cm.pk).price, D("12.00"))  # nothing saved
        self.assertFalse(RepricingRun.objects.exists())

    def test_preview_blocked_by_floor(self):
        self.make_rule("floor", kind="floor", reference="low", adjust_pct=D("0"), adjust_abs=D("0.50"))
        data = self.get("api_repricing_preview")
        row = next(r for r in data["results"] if r["listing_id"] == self.cm.pk)
        self.assertEqual((row["to"], row["floor_rule"], row["blocked_by_floor"]), ("9.00", None, False))
        paused = next(r for r in data["results"] if r["listing_id"] == self.paused.pk)
        self.assertEqual((paused["to"], paused["floor_rule"], paused["blocked_by_floor"]), ("3.00", None, False))
        RepricingRule.objects.filter(pk=self.rule.pk).update(adjust_pct=D("-50"))
        data = self.get("api_repricing_preview")
        row = next(r for r in data["results"] if r["listing_id"] == self.cm.pk)
        self.assertEqual((row["to"], row["floor_rule"], row["blocked_by_floor"]), ("8.50", "floor", True))
        self.assertEqual(data["totals"]["blocked"], 3)  # sheoldred cm and ct, counterspell (1.67 < low + 0.50)

    def test_run_applies(self):
        resp = self.post("api_repricing_run")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual({k: data[k] for k in ("changes", "raised", "lowered")},
                         {"changes": 3, "raised": 1, "lowered": 2})
        self.assertEqual(data["messages"], [{"level": "success", "text": "Repriced 3 listings: 1 up, 2 down. "
                                                                         "The next sync pushes them."}])
        cm, ct, paused, error, sold = (Listing.objects.get(pk=l.pk)
                                       for l in (self.cm, self.ct, self.paused, self.error, self.sold))
        self.assertEqual((cm.price, cm.status, cm.market_listing_id), (D("9.00"), "to_publish", "77"))
        self.assertEqual((ct.price, ct.status), (D("9.00"), "to_publish"))
        self.assertEqual((paused.price, paused.status), (D("3.00"), "paused"))
        self.assertEqual((error.price, sold.price), (D("5.00"), D("5.00")))  # not repriced
        run = RepricingRun.objects.get(pk=data["run_id"])
        self.assertEqual((run.user, run.trigger, run.changes, run.value_delta, run.dry_run),
                         (self.other, "manual", 3, D("-7.00"), False))
        self.assertIsNotNone(run.finished_at)
        change = RepricingChange.objects.get(run=run, listing=self.cm)
        self.assertEqual((change.old_price, change.new_price, change.rule, change.market), (D("12.00"), D("9.00"),
                                                                                           self.rule, "cm"))
        again = self.post("api_repricing_run").json()
        self.assertEqual((again["changes"], again["messages"][0]["text"]), (0, "No price to change."))

    def test_hold_below_cost_and_seller_rounding(self):
        self.settings(hold_below_cost=True, price_rounding="0.50")
        CollectionItem.objects.filter(pk=self.sheoldred.pk).update(cost=D("9.80"))
        self.post("api_repricing_run")
        self.assertEqual(Listing.objects.get(pk=self.cm.pk).price, D("10.00"))  # 9.00 held at cost 9.80, up to 0.50
        self.assertEqual(Listing.objects.get(pk=self.paused.pk).price, D("3.00"))

    def test_runs_and_detail(self):
        run_id = self.post("api_repricing_run").json()["run_id"]
        [run] = self.get("api_repricing_runs")["results"]
        self.assertEqual({k: v for k, v in run.items() if k not in ("started_at", "finished_at")},
                         {"id": run_id, "trigger": "manual", "dry_run": False, "changes": 3, "raised": 1,
                          "lowered": 2, "value_delta": "-7.00", "blocked": 0})
        detail = self.get("api_repricing_run_detail", pk=run_id)
        self.assertEqual((detail["id"], detail["count"], detail["page"], detail["num_pages"]), (run_id, 3, 1, 1))
        self.assertEqual(detail["results"][0], {
            "listing_id": self.paused.pk, "item_id": self.counterspell.pk, "name": "Counterspell", "set_code": "DMR",
            "condition": "NM", "language": "EN", "foil": False, "market": "ct", "from": "1.00", "to": "3.00",
            "rule": "Trend -10", "floor_rule": None, "blocked_by_floor": False})
        # the change stays readable after its rule and listing are gone
        self.rule.delete()
        Listing.objects.filter(pk=self.paused.pk).delete()
        row = self.get("api_repricing_run_detail", pk=run_id)["results"][0]
        self.assertEqual((row["listing_id"], row["name"], row["rule"]), (None, "Counterspell", "Trend -10"))
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("ledger:api_repricing_run_detail", args=[run_id])).status_code, 404)
        self.assertEqual(self.get("api_repricing_runs")["results"], [])

    def test_keeps_the_last_50_runs(self):
        for _ in range(51):
            RepricingRun.objects.create(user=self.other)
        RepricingRun.objects.create(user=self.user)
        newest = self.post("api_repricing_run").json()["run_id"]
        mine = RepricingRun.objects.owned_by(self.other)
        self.assertEqual(mine.count(), 50)
        self.assertTrue(mine.filter(pk=newest).exists())
        self.assertEqual(RepricingRun.objects.owned_by(self.user).count(), 1)

    def test_drafts_and_disabled_rules_do_nothing(self):
        RepricingRule.objects.filter(pk=self.rule.pk).update(draft=True)
        self.assertEqual(self.get("api_repricing_preview")["totals"]["changes"], 0)
        RepricingRule.objects.filter(pk=self.rule.pk).update(draft=False, enabled=False)
        self.assertEqual(self.get("api_repricing_preview")["totals"]["changes"], 0)

    def test_other_users_listings_are_untouched(self):
        before = dict(Listing.objects.owned_by(self.user).values_list("pk", "price"))
        self.post("api_repricing_run")
        self.assertEqual(dict(Listing.objects.owned_by(self.user).values_list("pk", "price")), before)

    def test_market_rules_and_foil_references(self):
        RepricingRule.objects.filter(pk=self.rule.pk).update(markets="cm")
        foil = self.item(SHEOLDRED, foil=True)
        foil_listing = self.listing(foil, "cm", "10.00", "to_publish")
        self.post("api_repricing_run")
        self.assertEqual(Listing.objects.get(pk=self.ct.pk).price, D("12.00"))  # a CardTrader listing
        self.assertEqual(Listing.objects.get(pk=foil_listing.pk).price, D("27.00"))  # trend_foil 30 - 10%

    def test_over_time_steps(self):
        RepricingRule.objects.filter(pk=self.rule.pk).delete()
        self.make_rule("drop", kind="over_time", reference="current", adjust_pct=D("-10"), interval_days=7)
        Listing.objects.filter(pk=self.cm.pk).update(listed_at=timezone.now() - timedelta(days=8))
        self.post("api_repricing_run")
        cm = Listing.objects.get(pk=self.cm.pk)
        self.assertEqual(cm.price, D("10.80"))
        self.assertIsNotNone(cm.last_step_at)
        self.assertEqual(Listing.objects.get(pk=self.ct.pk).price, D("12.00"))  # never published: no step
        self.post("api_repricing_run")
        self.assertEqual(Listing.objects.get(pk=self.cm.pk).price, D("10.80"))  # next step in 7 days
        Listing.objects.filter(pk=self.cm.pk).update(last_step_at=timezone.now() - timedelta(days=7))
        self.post("api_repricing_run")
        self.assertEqual(Listing.objects.get(pk=self.cm.pk).price, D("9.70"))  # 9.72

    def test_conditions_on_listing_facts(self):
        RepricingRule.objects.filter(pk=self.rule.pk).update(conditions=[
            {"field": "both_markets", "op": "eq", "value": True}])
        data = self.get("api_repricing_preview")
        self.assertEqual({r["listing_id"] for r in data["results"]}, {self.cm.pk, self.ct.pk})
        RepricingRule.objects.filter(pk=self.rule.pk).update(conditions=[
            {"field": "rarity", "op": "in", "value": ["mythic"]}, {"field": "game", "op": "in", "value": ["mtg"]},
            {"field": "set", "op": "in", "value": ["dmu"]}, {"field": "cost_known", "op": "eq", "value": False}])
        data = self.get("api_repricing_preview")
        self.assertEqual({r["listing_id"] for r in data["results"]}, {self.cm.pk, self.ct.pk})
        RepricingRule.objects.filter(pk=self.rule.pk).update(reference="other_market", adjust_pct=D("0"))
        Listing.objects.filter(pk=self.ct.pk).update(price=D("11.00"))
        moved = {r["listing_id"]: r["to"] for r in self.get("api_repricing_preview")["results"]}
        self.assertEqual(moved, {self.cm.pk: "11.00", self.ct.pk: "12.00"})


class QuoteTest(RepricingTestCase):
    def quote(self, **params):
        return {m["market"]: m for m in self.get("api_repricing_quote", {"blueprint": SHEOLDRED, **params})["markets"]}

    def test_without_rules_the_trend(self):
        q = self.quote()
        self.assertEqual(q["cm"], {"market": "cm", "price": "10.00", "rule": None, "floor_rule": None,
                                   "note": "No rule sets it: Cardmarket trend."})
        self.assertEqual(q["ct"]["price"], "10.00")
        self.assertEqual(self.quote(foil="1")["cm"]["price"], "30.00")

    def test_with_rules(self):
        self.make_rule("cm only", markets="cm", adjust_pct=D("10"))
        self.make_rule("all", adjust_pct=D("-10"))
        self.make_rule("floor", kind="floor", reference="cost", adjust_pct=D("0"), adjust_abs=D("1"))
        q = self.quote(cost="9.50")
        self.assertEqual((q["cm"]["price"], q["cm"]["rule"], q["cm"]["floor_rule"]), ("11.00", "cm only", None))
        self.assertEqual((q["ct"]["price"], q["ct"]["rule"], q["ct"]["floor_rule"]), ("10.50", "all", "floor"))
        self.assertEqual(q["ct"]["note"], "Raised to the floor of floor.")

    def test_no_price(self):
        q = {m["market"]: m for m in self.get("api_repricing_quote", {"blueprint": RAGAVAN})["markets"]}
        self.assertEqual((q["cm"]["price"], q["cm"]["rule"]), (None, None))
        self.assertTrue(q["cm"]["note"])

    def test_validation(self):
        url = reverse("ledger:api_repricing_quote")
        self.assertEqual(self.client.get(url).status_code, 400)
        self.assertEqual(self.client.get(url, {"blueprint": 1}).status_code, 400)
        self.assertEqual(self.client.get(url, {"blueprint": SHEOLDRED, "condition": "XX"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"blueprint": SHEOLDRED, "cost": "abc"}).status_code, 400)


class AdhocTest(RepricingTestCase):
    def setUp(self):
        super().setUp()
        self.sheoldred = self.item(SHEOLDRED, quantity=2)
        self.cm = self.listing(self.sheoldred, "cm", "12.00", "published")
        self.counterspell = self.item(COUNTERSPELL, quantity=3)  # not listed
        self.ragavan = self.item(RAGAVAN)  # no trend

    def adhoc(self, **data):
        body = {"ids": [self.sheoldred.pk, self.counterspell.pk, self.ragavan.pk], "mode": "trend_pct",
                "value": "-10", "basis": "trend", "markets": ["cm", "ct"], "dry_run": True, **data}
        return self.post("api_repricing_adhoc", body)

    def test_dry_run(self):
        data = self.adhoc().json()
        rows = {r["item_id"]: r for r in data["results"]}
        self.assertEqual(rows[self.sheoldred.pk], {"item_id": self.sheoldred.pk, "name": "Sheoldred, the Apocalypse",
                                                   "now": {"cm": "12.00", "ct": None},
                                                   "new": {"cm": "9.00", "ct": "9.00"}, "note": ""})
        self.assertEqual(rows[self.counterspell.pk]["new"], {"cm": "3.00", "ct": "3.00"})
        self.assertEqual(rows[self.ragavan.pk]["new"], {"cm": None, "ct": None})
        self.assertEqual(rows[self.ragavan.pk]["note"], "No Cardmarket trend.")
        self.assertEqual(Listing.objects.get(pk=self.cm.pk).price, D("12.00"))
        self.assertFalse(RepricingRun.objects.exists())

    def test_modes(self):
        def new(**data):
            return {r["item_id"]: r["new"] for r in self.adhoc(**data).json()["results"]}[self.sheoldred.pk]
        self.assertEqual(new(mode="set", value="7.77"), {"cm": "7.75", "ct": "7.75"})
        self.assertEqual(new(mode="change", value="-1", unit="eur", basis="current"), {"cm": "11.00", "ct": None})
        self.assertEqual(new(mode="change", value="10", unit="pct", basis="low"), {"cm": "8.80", "ct": "8.80"})
        self.assertEqual(new(mode="trend_pct", value="0", basis="avg30"), {"cm": "9.50", "ct": "9.50"})
        note = {r["item_id"]: r["note"] for r in self.adhoc(mode="change", value="1", basis="current").json()["results"]}
        self.assertEqual(note[self.sheoldred.pk], "No current price on CardTrader.")

    def test_floors_and_cost(self):
        self.settings(hold_below_cost=True)
        CollectionItem.objects.filter(pk=self.counterspell.pk).update(cost=D("4.00"))
        self.make_rule("floor", kind="floor", reference="low", adjust_pct=D("0"))
        rows = {r["item_id"]: r for r in self.adhoc(mode="set", value="1.00").json()["results"]}
        self.assertEqual(rows[self.sheoldred.pk]["new"], {"cm": "8.00", "ct": "8.00"})
        self.assertIn("raised to the floor of floor", rows[self.sheoldred.pk]["note"])
        self.assertEqual(rows[self.counterspell.pk]["new"], {"cm": "4.00", "ct": "4.00"})
        self.assertIn("held at cost", rows[self.counterspell.pk]["note"])

    def test_apply_one_market(self):
        resp = self.adhoc(markets=["ct"], dry_run=False)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json(), {"count": 2, "messages": [
            {"level": "success", "text": "Repriced 2 items. The next sync pushes the prices."},
            {"level": "warning", "text": "Skipped 1 item without a price to set."}]})
        self.assertEqual(Listing.objects.get(pk=self.cm.pk).price, D("12.00"))  # Cardmarket not chosen
        ct = Listing.objects.get(item=self.sheoldred, market="ct")
        self.assertEqual((ct.price, ct.quantity, ct.status), (D("9.00"), 2, "to_publish"))
        self.assertEqual(Listing.objects.get(item=self.counterspell, market="ct").quantity, 3)
        run = RepricingRun.objects.get()
        self.assertEqual((run.trigger, run.changes), ("adhoc", 2))
        self.assertEqual(set(run.price_changes.values_list("old_price", flat=True)), {None})

    def test_apply_updates_a_published_listing(self):
        self.adhoc(markets=["cm"], dry_run=False, ids=[self.sheoldred.pk])
        cm = Listing.objects.get(pk=self.cm.pk)
        self.assertEqual((cm.price, cm.status), (D("9.00"), "to_publish"))
        self.assertFalse(Listing.objects.filter(item=self.sheoldred, market="ct").exists())

    def test_validation_and_ownership(self):
        self.assertEqual(self.adhoc(ids=[]).status_code, 400)
        self.assertIn("mode", self.adhoc(mode="x").json())
        self.assertIn("markets", self.adhoc(markets=[]).json())
        self.assertIn("value", self.adhoc(mode="set", value="0").json())
        mine = CollectionItem.objects.owned_by(self.user).first()
        self.assertEqual(self.adhoc(ids=[mine.pk]).json(), {"results": []})
        resp = self.adhoc(ids=[mine.pk], dry_run=False)
        self.assertEqual(resp.json()["count"], 0)
        self.assertEqual(resp.json()["messages"][0]["level"], "warning")


class PlanHookTest(RepricingTestCase):
    def setUp(self):
        super().setUp()
        code = self.client.post(reverse("ledger:api_extension_pair")).json()["code"]
        token = Client().post(reverse("ledger:api_extension_claim"), {"code": code, "name": "Chrome"},
                              content_type="application/json").json()["token"]
        self.ext = Client(HTTP_AUTHORIZATION=f"Token {token}")
        item = self.item(SHEOLDRED)
        self.cm = self.listing(item, "cm", "12.00", "published", market_listing_id="9")
        self.ct = self.listing(item, "ct", "12.00", "published", market_listing_id="8")
        self.make_rule()

    def plan(self, market):
        resp = self.ext.get(reverse("ledger:api_extension_plan"), {"market": market})
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def test_off_by_default(self):
        self.assertEqual(self.plan("cm")["changes"], [])
        self.assertFalse(RepricingRun.objects.exists())

    def test_reprice_before_sync(self):
        self.settings(reprice_before_sync=True)
        [change] = self.plan("cm")["changes"]
        self.assertEqual((change["listing_id"], change["action"], change["price"]), (self.cm.pk, "update", "9.00"))
        self.assertEqual(Listing.objects.get(pk=self.ct.pk).price, D("12.00"))  # only the planned market
        run = RepricingRun.objects.get()
        self.assertEqual((run.trigger, run.changes), ("sync", 1))

    def test_setting_in_the_account_api(self):
        resp = self.client.put(reverse("accounts:api_settings"), {"reprice_before_sync": True},
                               content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        data = self.client.get(reverse("accounts:api_settings")).json()
        self.assertIs(data["reprice_before_sync"], True)
        self.assertEqual(data["hold_below_cost"], False)


class AddAndListTest(RepricingTestCase):
    def add(self, product=SHEOLDRED, **fields):
        body = {"blueprint": product, "condition": "NM", "language": "EN", "quantity": 1, **fields}
        return self.client.post(reverse("ledger:api_collection"), body, content_type="application/json")

    def test_cost_location_and_list_on(self):
        self.make_rule("ct up", markets="ct", adjust_pct=D("10"))
        resp = self.add(quantity=3, cost="4.50", location="A1", list_on=["cm", "ct"])
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["messages"], [{"level": "success", "text": (
            "Added 3× Sheoldred, the Apocalypse. Listed on Cardmarket at €10.00 and CardTrader at €11.00.")}])
        item = CollectionItem.objects.get(pk=resp.json()["id"])
        self.assertEqual((item.cost, item.location, item.quantity), (D("4.50"), "A1", 3))
        listings = {l.market: (l.price, l.quantity, l.status) for l in item.listings.all()}
        self.assertEqual(listings, {"cm": (D("10.00"), 3, "to_publish"), "ct": (D("11.00"), 3, "to_publish")})

    def test_merged_copies_add_to_the_listing(self):
        item_id = self.add(quantity=1, list_on=["cm"]).json()["id"]
        Listing.objects.filter(item_id=item_id).update(status="published", price=D("15.00"))
        resp = self.add(quantity=2, list_on=["cm"])
        self.assertEqual(resp.status_code, 200)
        listing = Listing.objects.get(item_id=item_id)
        self.assertEqual((listing.price, listing.quantity, listing.status), (D("15.00"), 3, "to_publish"))
        self.assertEqual(CollectionItem.objects.get(pk=item_id).cost, None)

    def test_no_price_no_listing(self):
        resp = self.add(RAGAVAN, list_on=["ct"])
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["messages"][1]["level"], "warning")
        self.assertFalse(Listing.objects.filter(item_id=resp.json()["id"]).exists())

    def test_without_list_on_nothing_is_listed(self):
        item_id = self.add(cost="1.00").json()["id"]
        self.assertFalse(Listing.objects.filter(item_id=item_id).exists())
        self.assertIn("list_on", self.add(list_on=["xx"]).json())
        self.assertIn("cost", self.add(cost="-1").json())


class MeCountsTest(RepricingTestCase):
    def test_rules_counts(self):
        self.make_rule("on")
        self.make_rule("off", enabled=False)
        self.make_rule("draft", draft=True)
        self.make_rule("gab's", user=self.user)
        counts = self.client.get(reverse("accounts:api_me")).json()["counts"]
        self.assertEqual((counts["rules_on"], counts["rules_total"]), (1, 3))


class RepricingSchemaTest(RepricingTestCase):
    def test_documented(self):
        resp = self.client.get(reverse("api_schema"), HTTP_ACCEPT="application/vnd.oai.openapi+json")
        schema = json.loads(resp.content)
        for path in ("/api/repricing/rules/", "/api/repricing/rules/{id}/", "/api/repricing/rules/reorder/",
                     "/api/repricing/rules/preview/", "/api/repricing/preview/", "/api/repricing/run/",
                     "/api/repricing/runs/", "/api/repricing/runs/{id}/", "/api/repricing/quote/",
                     "/api/repricing/adhoc/"):
            self.assertIn(path, schema["paths"])
        components = schema["components"]["schemas"]

        def body(path, method):
            ref = schema["paths"][path][method]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
            return set(components[ref.rsplit("/", 1)[1]]["properties"])

        rule = {"name", "kind", "markets", "conditions", "reference", "adjust_pct", "adjust_abs", "round_to",
                "interval_days", "enabled", "draft", "position"}
        self.assertEqual(body("/api/repricing/rules/", "post"), rule)
        self.assertEqual(body("/api/repricing/rules/preview/", "post"), rule | {"id"})
        self.assertEqual(body("/api/repricing/adhoc/", "post"),
                         {"ids", "mode", "value", "unit", "basis", "markets", "dry_run"})
        self.assertEqual(body("/api/collection/", "post"),
                         {"blueprint", "condition", "language", "foil", "quantity", "cost", "location", "list_on"})
        self.assertIn("reprice_before_sync", body("/api/account/settings/", "put"))
        query = {p["name"] for p in schema["paths"]["/api/repricing/quote/"]["get"]["parameters"]}
        self.assertEqual(query, {"blueprint", "condition", "language", "foil", "cost"})
