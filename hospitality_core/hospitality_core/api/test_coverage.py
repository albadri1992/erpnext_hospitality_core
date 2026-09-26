"""
Unit tests for the coverage decision engine (hospitality_core.hospitality_core.api.coverage).

Standard example (pre-tax) from the Hospitality Financial Architecture spec:
    Room = 100, Main Meals = 30, Minibar = 10, Bar = 20, Other = 15  -> total 175.

These are pure-logic tests (no DB), runnable via:
    bench --site <site> run-tests --module hospitality_core.hospitality_core.api.test_coverage
"""

import unittest

from hospitality_core.hospitality_core.api.coverage import (
    BAR,
    INCLUDED_MEAL,
    MINIBAR,
    OTHER,
    ROOM_RATE,
    aggregate_by_customer,
    assert_split_invariant,
    resolve_liability,
)

COMP = "C-COMPANY"
GUEST = "C-GUEST"


def standard_charges():
    return [
        {"id": "room", "category": ROOM_RATE, "amount": 100},
        {"id": "meal", "category": INCLUDED_MEAL, "meal_type": "Dinner", "amount": 30},
        {"id": "minibar", "category": MINIBAR, "amount": 10},
        {"id": "bar", "category": BAR, "amount": 20},
        {"id": "other", "category": OTHER, "amount": 15},
    ]


class TestCoverageScenarios(unittest.TestCase):
    def _totals(self, agreement, guest_customer):
        results = resolve_liability(standard_charges(), agreement=agreement, guest_customer=guest_customer)
        assert_split_invariant(results)
        return aggregate_by_customer(results)

    def test_scenario_1_guest_is_customer_all_personal(self):
        totals, unassigned = self._totals(agreement=None, guest_customer=GUEST)
        self.assertEqual(totals, {GUEST: 175})
        self.assertEqual(unassigned, 0)

    def test_scenario_2_company_covers_all(self):
        agreement = {"sponsor_customer": COMP, "rules": [{"coverage_percent": 100}]}
        totals, unassigned = self._totals(agreement=agreement, guest_customer=None)
        self.assertEqual(totals, {COMP: 175})
        self.assertEqual(unassigned, 0)

    def test_scenario_3_company_covers_room_only(self):
        agreement = {"sponsor_customer": COMP, "rules": [
            {"charge_category": ROOM_RATE, "coverage_percent": 100, "priority": 1},
        ]}
        totals, unassigned = self._totals(agreement=agreement, guest_customer=GUEST)
        self.assertEqual(totals, {COMP: 100, GUEST: 75})
        self.assertEqual(unassigned, 0)

    def test_scenario_4_company_covers_room_and_main_meals(self):
        agreement = {"sponsor_customer": COMP, "rules": [
            {"charge_category": ROOM_RATE, "coverage_percent": 100, "priority": 1},
            {"charge_category": INCLUDED_MEAL, "coverage_percent": 100, "priority": 1},
        ]}
        totals, unassigned = self._totals(agreement=agreement, guest_customer=GUEST)
        self.assertEqual(totals, {COMP: 130, GUEST: 45})
        self.assertEqual(unassigned, 0)

    def test_scenario_5a_room_only_with_minibar_bar_excluded(self):
        agreement = {"sponsor_customer": COMP, "rules": [
            {"charge_category": ROOM_RATE, "coverage_percent": 100, "priority": 2},
            {"charge_category": MINIBAR, "exclude": True, "priority": 1},
            {"charge_category": BAR, "exclude": True, "priority": 1},
        ]}
        totals, unassigned = self._totals(agreement=agreement, guest_customer=GUEST)
        self.assertEqual(totals, {COMP: 100, GUEST: 75})
        self.assertEqual(unassigned, 0)

    def test_scenario_5b_room_and_meals_with_minibar_bar_excluded(self):
        agreement = {"sponsor_customer": COMP, "rules": [
            {"charge_category": ROOM_RATE, "coverage_percent": 100, "priority": 2},
            {"charge_category": INCLUDED_MEAL, "coverage_percent": 100, "priority": 2},
            {"charge_category": MINIBAR, "exclude": True, "priority": 1},
            {"charge_category": BAR, "exclude": True, "priority": 1},
        ]}
        totals, unassigned = self._totals(agreement=agreement, guest_customer=GUEST)
        self.assertEqual(totals, {COMP: 130, GUEST: 45})
        self.assertEqual(unassigned, 0)

    def test_scenario_5c_exclusion_overrides_general_cover_all(self):
        # A broad "cover everything" rule, but minibar & bar are explicitly excluded.
        agreement = {"sponsor_customer": COMP, "rules": [
            {"coverage_percent": 100, "priority": 5},
            {"charge_category": MINIBAR, "exclude": True, "priority": 1},
            {"charge_category": BAR, "exclude": True, "priority": 1},
        ]}
        totals, unassigned = self._totals(agreement=agreement, guest_customer=GUEST)
        # Company: 100 + 30 + 15 = 145 ; Guest: 10 + 20 = 30
        self.assertEqual(totals, {COMP: 145, GUEST: 30})
        self.assertEqual(unassigned, 0)

    def test_scenario_6_guest_is_customer_with_extras(self):
        totals, unassigned = self._totals(agreement=None, guest_customer=GUEST)
        self.assertEqual(totals.get(GUEST), 175)
        self.assertEqual(unassigned, 0)


class TestCoverageEdgeCases(unittest.TestCase):
    def test_partial_50_percent_meal_coverage(self):
        agreement = {"sponsor_customer": COMP, "rules": [
            {"charge_category": ROOM_RATE, "coverage_percent": 100, "priority": 1},
            {"charge_category": INCLUDED_MEAL, "coverage_percent": 50, "priority": 1},
        ]}
        results = resolve_liability(standard_charges(), agreement=agreement, guest_customer=GUEST)
        assert_split_invariant(results)
        totals, _ = aggregate_by_customer(results)
        # Company: 100 + 15 (half of 30) = 115 ; Guest: 15 + 10 + 20 + 15 = 60
        self.assertEqual(totals, {COMP: 115, GUEST: 60})

    def test_amount_cap_consumed_across_two_charges(self):
        charges = [
            {"id": "n1", "category": ROOM_RATE, "amount": 60},
            {"id": "n2", "category": ROOM_RATE, "amount": 60},
        ]
        agreement = {"sponsor_customer": COMP, "rules": [
            {"charge_category": ROOM_RATE, "coverage_percent": 100, "amount_limit": 100, "priority": 1},
        ]}
        results = resolve_liability(charges, agreement=agreement, guest_customer=GUEST)
        assert_split_invariant(results)
        totals, _ = aggregate_by_customer(results)
        # Cap 100 shared: first night fully covered (60), second night covered 40, guest 20.
        self.assertEqual(totals, {COMP: 100, GUEST: 20})

    def test_guest_share_without_personal_customer_is_flagged_unassigned(self):
        agreement = {"sponsor_customer": COMP, "rules": [
            {"charge_category": ROOM_RATE, "coverage_percent": 100, "priority": 1},
        ]}
        results = resolve_liability(standard_charges(), agreement=agreement, guest_customer=None)
        assert_split_invariant(results)
        totals, unassigned = aggregate_by_customer(results)
        self.assertEqual(totals, {COMP: 100})
        self.assertEqual(unassigned, 75)  # not routed to any default customer
        # every guest share must be explicitly flagged
        guest_shares = [s for r in results for s in r["shares"] if s["kind"] == "guest"]
        self.assertTrue(all(s["requires_customer"] for s in guest_shares))

    def test_split_invariant_holds_for_all_scenarios(self):
        agreement = {"sponsor_customer": COMP, "rules": [
            {"charge_category": ROOM_RATE, "coverage_percent": 33, "priority": 1},
            {"charge_category": INCLUDED_MEAL, "coverage_percent": 50, "priority": 1},
        ]}
        results = resolve_liability(standard_charges(), agreement=agreement, guest_customer=GUEST)
        assert_split_invariant(results)
        totals, unassigned = aggregate_by_customer(results)
        grand_total = round(sum(totals.values()) + unassigned, 2)
        self.assertEqual(grand_total, 175)


if __name__ == "__main__":
    unittest.main()
