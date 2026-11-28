"""The repricing engine (ledger/repricing.py), without the database."""

from decimal import Decimal as D

from django.test import SimpleTestCase

from .repricing import Facts, Rule, condition_matches, evaluate, insert_rule, round_price, rule_matches, step_due


def facts(**fields):
    refs = {"trend": D("10.00"), "low": D("8.00"), "avg7": D("9.00"), "avg30": D("9.50"), "cost": None,
            "other_market": None, "current": D("10.00")}
    refs.update(fields.pop("refs", {}))
    base = {"market": "cm", "game": "mtg", "rarity": "Rare", "condition": "NM", "language": "EN", "foil": False,
            "is_product": False, "set_code": "MH2", "listed_days": 3, "last_step_days": None, "both_markets": False}
    return Facts(refs=refs, **{**base, **fields})


def rule(name="r", kind="price", **fields):
    for key in ("adjust_pct", "adjust_abs"):
        if key in fields:
            fields[key] = D(fields[key])
    if "round_to" in fields and fields["round_to"] is not None:
        fields["round_to"] = D(fields["round_to"])
    if "conditions" in fields:
        fields["conditions"] = tuple(fields["conditions"])
    return Rule(id=name, name=name, kind=kind, **fields)


def cond(field, op, value):
    return {"field": field, "op": op, "value": value}


class ConditionTest(SimpleTestCase):
    def test_in_fields(self):
        f = facts(game="pokemon", rarity="Mythic", condition="LP", language="IT", set_code="MH2")
        self.assertTrue(condition_matches(cond("game", "in", ["mtg", "pokemon"]), f))
        self.assertFalse(condition_matches(cond("game", "in", ["mtg"]), f))
        self.assertTrue(condition_matches(cond("rarity", "in", ["mythic"]), f))  # case-insensitive
        self.assertTrue(condition_matches(cond("condition", "in", ["LP", "PL"]), f))
        self.assertFalse(condition_matches(cond("condition", "in", ["NM"]), f))
        self.assertTrue(condition_matches(cond("language", "in", ["IT"]), f))
        self.assertTrue(condition_matches(cond("set", "in", ["mh2"]), f))
        self.assertFalse(condition_matches(cond("set", "in", ["neo"]), f))

    def test_eq_fields(self):
        f = facts(foil=True, is_product=False, both_markets=True, refs={"cost": D("1.00")})
        self.assertTrue(condition_matches(cond("foil", "eq", True), f))
        self.assertFalse(condition_matches(cond("is_product", "eq", True), f))
        self.assertTrue(condition_matches(cond("both_markets", "eq", True), f))
        self.assertTrue(condition_matches(cond("cost_known", "eq", True), f))
        self.assertTrue(condition_matches(cond("cost_known", "eq", False), facts()))

    def test_trend(self):
        f = facts(refs={"trend": D("5.00")})
        self.assertTrue(condition_matches(cond("trend", "gte", "5"), f))
        self.assertFalse(condition_matches(cond("trend", "gte", "5.01"), f))
        self.assertTrue(condition_matches(cond("trend", "lte", "5.00"), f))
        self.assertTrue(condition_matches(cond("trend", "between", ["1", "5"]), f))
        self.assertFalse(condition_matches(cond("trend", "between", ["6", "9"]), f))
        self.assertFalse(condition_matches(cond("trend", "lte", "100"), facts(refs={"trend": None})))

    def test_listed_days(self):
        self.assertTrue(condition_matches(cond("listed_days", "gte", "30"), facts(listed_days=30)))
        self.assertFalse(condition_matches(cond("listed_days", "gte", "30"), facts(listed_days=29)))
        self.assertTrue(condition_matches(cond("listed_days", "lte", "0"), facts(listed_days=None)))  # never listed

    def test_markets_and_all_conditions(self):
        r = rule(markets="ct", conditions=[cond("foil", "eq", False)])
        self.assertFalse(rule_matches(r, facts(market="cm")))
        self.assertTrue(rule_matches(r, facts(market="ct")))
        r = rule(conditions=[cond("foil", "eq", False), cond("set", "in", ["NEO"])])
        self.assertFalse(rule_matches(r, facts()))
        self.assertTrue(rule_matches(rule(), facts()))  # no conditions: any listing


class EvaluateTest(SimpleTestCase):
    def test_price_rule(self):
        out = evaluate(facts(), [rule(reference="trend", adjust_pct="-10", adjust_abs="0.20")])
        self.assertEqual(out.price, D("9.20"))  # 10 × 0.9 + 0.2
        self.assertEqual(out.rule.name, "r")

    def test_references(self):
        f = facts(refs={"cost": D("2.00"), "other_market": D("7.00"), "current": D("6.00")})
        for ref, expected in [("trend", "10.00"), ("low", "8.00"), ("avg7", "9.00"), ("avg30", "9.50"),
                              ("cost", "2.00"), ("other_market", "7.00"), ("current", "6.00")]:
            self.assertEqual(evaluate(f, [rule(reference=ref)]).price, D(expected), ref)

    def test_first_price_rule_wins(self):
        rules = [rule("a", conditions=[cond("set", "in", ["NEO"])], adjust_pct="50"),
                 rule("b", adjust_pct="-20"), rule("c", adjust_pct="+20")]
        out = evaluate(facts(), rules)
        self.assertEqual((out.rule.name, out.price), ("b", D("8.00")))

    def test_rule_without_its_reference_is_skipped(self):
        rules = [rule("cost", reference="cost"), rule("trend")]
        self.assertEqual(evaluate(facts(), rules).rule.name, "trend")

    def test_inactive_rules_are_skipped(self):
        rules = [Rule(id=1, name="off", kind="price", adjust_pct=D("50"), active=False), rule("on")]
        self.assertEqual(evaluate(facts(), rules).rule.name, "on")

    def test_floors_after_the_price_rule(self):
        rules = [rule("floor", kind="floor", reference="low", adjust_pct="0"),  # before it: floors apply after
                 rule("cheap", adjust_pct="-50"),
                 rule("floor2", kind="floor", reference="avg7", adjust_pct="-10")]  # avg7 × 0.9 = 8.10 over low 8.00
        out = evaluate(facts(), rules)
        self.assertEqual((out.price, out.rule.name, out.floor_rule.name, out.blocked_by_floor),
                         (D("8.10"), "cheap", "floor2", True))

    def test_floor_without_price_rule(self):
        out = evaluate(facts(refs={"current": D("3.00")}), [rule("f", kind="floor", reference="low")])
        self.assertEqual((out.price, out.rule, out.floor_rule.name), (D("8.00"), None, "f"))

    def test_no_rule_leaves_the_price_exactly(self):
        out = evaluate(facts(refs={"current": D("3.33")}), [])
        self.assertEqual(out.price, D("3.33"))  # not re-rounded to 3.35
        self.assertIsNone(out.moved(D("3.33")))

    def test_hold_below_cost(self):
        f = facts(refs={"cost": D("9.03")})
        out = evaluate(f, [rule(adjust_pct="-50")], hold_below_cost=True)
        self.assertEqual(out.price, D("9.05"))  # cost 9.03 rounded up, never under it
        self.assertTrue(out.held_at_cost and out.blocked_by_floor)
        self.assertEqual(evaluate(f, [rule(adjust_pct="-50")]).price, D("5.00"))  # setting off

    def test_market_minimum(self):
        f = facts(refs={"trend": D("0.01"), "current": D("1.00")})
        self.assertEqual(evaluate(f, [rule(round_to="0.01")]).price, D("0.02"))  # Cardmarket
        self.assertEqual(evaluate(facts(market="ct", refs=f.refs), [rule(round_to="0.01")]).price, D("0.01"))
        self.assertFalse(evaluate(f, [rule(round_to="0.01")]).blocked_by_floor)

    def test_rounding(self):
        f = facts(refs={"trend": D("1.00")})
        self.assertEqual(evaluate(f, [rule(adjust_pct="12")]).price, D("1.10"))  # default step 0.05
        self.assertEqual(evaluate(f, [rule(adjust_pct="13")]).price, D("1.15"))
        self.assertEqual(evaluate(f, [rule(adjust_pct="13")], default_step=D("0.50")).price, D("1.00"))
        self.assertEqual(evaluate(f, [rule(adjust_pct="13", round_to="0.01")]).price, D("1.13"))
        self.assertEqual(evaluate(f, [rule(adjust_pct="13", round_to="1.00")]).price, D("1.00"))

    def test_round_price(self):
        self.assertEqual(round_price(D("2.997"), "0.05"), D("3.00"))
        self.assertEqual(round_price(D("2.01"), "0.05", minimum=D("2.01")), D("2.05"))
        self.assertEqual(round_price(D("0.02"), "0.05", minimum=D("0.02")), D("0.05"))

    def test_candidate_skips_price_rules(self):
        rules = [rule(adjust_pct="50"), rule("f", kind="floor", reference="low")]
        self.assertEqual(evaluate(facts(), rules, candidate=D("12.00")).price, D("12.00"))
        self.assertEqual(evaluate(facts(), rules, candidate=D("5.00")).price, D("8.00"))  # floor

    def test_fallback_for_a_new_listing(self):
        f = facts(refs={"current": None})
        self.assertEqual(evaluate(f, [], fallback=D("4.02")).price, D("4.00"))
        self.assertIsNone(evaluate(f, []).price)
        self.assertEqual(evaluate(f, [rule("cur", reference="current"), rule("t", adjust_pct="10")]).rule.name, "t")

    def test_moved_ignores_less_than_a_cent(self):
        out = evaluate(facts(), [rule(adjust_pct="0")])
        self.assertIsNone(out.moved(D("10.00")))
        self.assertEqual(out.moved(D("9.99")), D("10.00"))


class OverTimeTest(SimpleTestCase):
    def over(self, **fields):
        return rule("drop", kind="over_time", adjust_pct="-10", interval_days=7, **fields)

    def test_step_when_due(self):
        out = evaluate(facts(listed_days=7, refs={"current": D("10.00")}), [self.over()])
        self.assertEqual((out.price, out.stepped, out.rule.name), (D("9.00"), True, "drop"))

    def test_no_step_before_the_interval(self):
        out = evaluate(facts(listed_days=6, refs={"current": D("10.00")}), [self.over(), rule("later")])
        self.assertEqual((out.price, out.stepped, out.rule.name), (D("10.00"), False, "drop"))  # it still wins

    def test_next_step_after_the_last(self):
        r = self.over()
        self.assertFalse(step_due(r, facts(listed_days=30, last_step_days=3)))
        self.assertTrue(step_due(r, facts(listed_days=30, last_step_days=7)))
        self.assertTrue(step_due(r, facts(listed_days=30, last_step_days=None)))
        self.assertFalse(step_due(r, facts(listed_days=None)))  # never published

    def test_steps_stop_at_a_floor(self):
        rules = [self.over(), rule("f", kind="floor", reference="low")]
        out = evaluate(facts(listed_days=10, refs={"current": D("8.50")}), rules)
        self.assertEqual((out.price, out.floor_rule.name), (D("8.00"), "f"))


class InsertRuleTest(SimpleTestCase):
    def test_insert_and_replace(self):
        a, b, c = rule("a"), rule("b"), rule("c")
        new = rule("n")
        self.assertEqual([r.name for r in insert_rule([a, b, c], new)], ["a", "b", "c", "n"])
        self.assertEqual([r.name for r in insert_rule([a, b, c], new, position=1)], ["n", "a", "b", "c"])
        self.assertEqual([r.name for r in insert_rule([a, b, c], new, position=2, replace_id="c")], ["a", "n", "b"])
