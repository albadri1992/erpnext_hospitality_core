"""
End-to-end integration tests for the SI-based billing & collection cycle.

These are REAL ERPNext document tests: they create Sales Invoices, Payment Entries and
assert on GL Entries / outstanding_amount / party ledger — not standalone arithmetic.

Run:
    bench --site <site> run-tests --module hospitality_core.hospitality_core.api.test_billing_integration

Standard pre-tax example: Room=100, Main Meal=30, Minibar=10, Bar=20, Other=15 (total 175).
"""

import uuid

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from hospitality_core.hospitality_core.api import billing
from hospitality_core.hospitality_core.api.coverage import (
    BAR,
    INCLUDED_MEAL,
    MINIBAR,
    OTHER,
    ROOM_RATE,
)

COMPANY = "Grand Hotel"
GUEST_CUST = "Guest Customer TST"
SPONSOR_CUST = "Sponsor Co TST"

ITEMS = {
    ROOM_RATE: ("HC-ROOM", 100),
    INCLUDED_MEAL: ("HC-MEAL", 30),
    MINIBAR: ("HC-MINIBAR", 10),
    BAR: ("HC-BAR", 20),
    OTHER: ("HC-OTHER", 15),
}


def _non_group(doctype, preferred):
    for name in preferred:
        if frappe.db.exists(doctype, {"name": name, "is_group": 0}):
            return name
    return frappe.db.get_value(doctype, {"is_group": 0}, "name")


def _ensure_customer(name):
    if not frappe.db.exists("Customer", name):
        frappe.get_doc({
            "doctype": "Customer", "customer_name": name, "customer_type": "Company",
            "customer_group": _non_group("Customer Group", ["Commercial", "Individual", "All Customer Groups"]),
            "territory": _non_group("Territory", ["Rest Of The World", "United States", "All Territories"]),
        }).insert(ignore_permissions=True)


def _ensure_item(code):
    if not frappe.db.exists("Item", code):
        frappe.get_doc({
            "doctype": "Item", "item_code": code, "item_name": code,
            "item_group": "Services" if frappe.db.exists("Item Group", "Services") else "All Item Groups",
            "stock_uom": "Nos" if frappe.db.exists("UOM", "Nos") else frappe.db.get_value("UOM", {}, "name"),
            "is_stock_item": 0,
        }).insert(ignore_permissions=True)


class BillingTestBase(FrappeTestCase):
    def setUp(self):
        _ensure_customer(GUEST_CUST)
        _ensure_customer(SPONSOR_CUST)
        for _cat, (code, _amt) in ITEMS.items():
            _ensure_item(code)
        self.pfx = uuid.uuid4().hex[:8]

    def charges(self, subset=None):
        rows = []
        for cat, (code, amt) in ITEMS.items():
            if subset and cat not in subset:
                continue
            rows.append({
                "id": f"{self.pfx}-{code}", "category": cat, "item_code": code,
                "item_group": "Services", "amount": amt,
                "description": code, "meal_type": "Dinner" if cat == INCLUDED_MEAL else None,
            })
        return rows

    # ---- assertion helpers on real ERPNext docs ----
    def si(self, name):
        return frappe.get_doc("Sales Invoice", name)

    def gl(self, voucher):
        return frappe.get_all(
            "GL Entry",
            filters={"voucher_no": voucher, "is_cancelled": 0},
            fields=["account", "party", "party_type", "debit", "credit"],
        )

    def receivable_debit(self, voucher, customer):
        debtors = frappe.db.get_value("Company", COMPANY, "default_receivable_account")
        total = 0.0
        for r in self.gl(voucher):
            if r.account == debtors and r.party == customer:
                total += flt(r.debit) - flt(r.credit)
        return flt(total, 2)

    def income_credit(self, voucher):
        income = frappe.db.get_value("Company", COMPANY, "default_income_account")
        total = 0.0
        for r in self.gl(voucher):
            if r.account == income:
                total += flt(r.credit) - flt(r.debit)
        return flt(total, 2)

    def invoices_by_customer(self, result):
        out = {}
        for name in result["invoices"]:
            si = self.si(name)
            out[si.customer] = si
        return out


class TestSixScenarios(BillingTestBase):
    def test_scenario_1_guest_is_customer(self):
        res = billing.create_invoices_for_charges(
            COMPANY, self.charges(), agreement=None, guest_customer=GUEST_CUST)
        by = self.invoices_by_customer(res)
        self.assertEqual(set(by), {GUEST_CUST})
        self.assertEqual(flt(by[GUEST_CUST].net_total, 2), 175)
        self.assertEqual(flt(by[GUEST_CUST].outstanding_amount, 2), 175)
        # real GL: receivable debit on the guest customer equals the invoice total
        self.assertEqual(self.receivable_debit(by[GUEST_CUST].name, GUEST_CUST), 175)
        self.assertEqual(self.income_credit(by[GUEST_CUST].name), 175)

    def test_scenario_2_company_covers_all(self):
        agreement = {"sponsor_customer": SPONSOR_CUST, "rules": [{"coverage_percent": 100}]}
        res = billing.create_invoices_for_charges(
            COMPANY, self.charges(), agreement=agreement, guest_customer=GUEST_CUST)
        by = self.invoices_by_customer(res)
        self.assertEqual(set(by), {SPONSOR_CUST})
        self.assertEqual(flt(by[SPONSOR_CUST].net_total, 2), 175)
        self.assertEqual(self.receivable_debit(by[SPONSOR_CUST].name, SPONSOR_CUST), 175)
        self.assertEqual(res["unassigned"], [])

    def test_scenario_3_company_room_only_two_invoices(self):
        agreement = {"sponsor_customer": SPONSOR_CUST, "rules": [
            {"charge_category": ROOM_RATE, "coverage_percent": 100, "priority": 1}]}
        res = billing.create_invoices_for_charges(
            COMPANY, self.charges(), agreement=agreement, guest_customer=GUEST_CUST)
        by = self.invoices_by_customer(res)
        self.assertEqual(flt(by[SPONSOR_CUST].net_total, 2), 100)
        self.assertEqual(flt(by[GUEST_CUST].net_total, 2), 75)
        # paying one does not settle the other
        billing.collect_invoice_payment(by[SPONSOR_CUST].name)
        self.assertEqual(flt(self.si(by[SPONSOR_CUST].name).outstanding_amount, 2), 0)
        self.assertEqual(flt(self.si(by[GUEST_CUST].name).outstanding_amount, 2), 75)

    def test_scenario_4_company_room_and_meals(self):
        agreement = {"sponsor_customer": SPONSOR_CUST, "rules": [
            {"charge_category": ROOM_RATE, "coverage_percent": 100, "priority": 1},
            {"charge_category": INCLUDED_MEAL, "coverage_percent": 100, "priority": 1}]}
        res = billing.create_invoices_for_charges(
            COMPANY, self.charges(), agreement=agreement, guest_customer=GUEST_CUST)
        by = self.invoices_by_customer(res)
        self.assertEqual(flt(by[SPONSOR_CUST].net_total, 2), 130)
        self.assertEqual(flt(by[GUEST_CUST].net_total, 2), 45)

    def test_scenario_5_minibar_bar_excluded_overrides_general(self):
        agreement = {"sponsor_customer": SPONSOR_CUST, "rules": [
            {"coverage_percent": 100, "priority": 5},
            {"charge_category": MINIBAR, "exclude": True, "priority": 1},
            {"charge_category": BAR, "exclude": True, "priority": 1}]}
        res = billing.create_invoices_for_charges(
            COMPANY, self.charges(), agreement=agreement, guest_customer=GUEST_CUST)
        by = self.invoices_by_customer(res)
        self.assertEqual(flt(by[SPONSOR_CUST].net_total, 2), 145)  # 100+30+15
        self.assertEqual(flt(by[GUEST_CUST].net_total, 2), 30)     # 10+20

    def test_scenario_6_guest_customer_with_extras_no_rebilling(self):
        # First bill room only for the guest, then bill the rest later; no charge re-billed.
        first = billing.create_invoices_for_charges(
            COMPANY, self.charges(subset={ROOM_RATE}), agreement=None, guest_customer=GUEST_CUST)
        self.assertEqual(len(first["invoices"]), 1)
        # Now attempt to bill ALL charges (room already billed) -> room is skipped.
        second = billing.create_invoices_for_charges(
            COMPANY, self.charges(), agreement=None, guest_customer=GUEST_CUST)
        self.assertIn(f"{self.pfx}-HC-ROOM:guest", second["skipped"])
        by = self.invoices_by_customer(second)
        # second invoice bills the remaining 75 (30+10+20+15), not 175
        self.assertEqual(flt(by[GUEST_CUST].net_total, 2), 75)


class TestEdgeCases(BillingTestBase):
    def test_idempotency_no_double_invoice(self):
        first = billing.create_invoices_for_charges(
            COMPANY, self.charges(), agreement=None, guest_customer=GUEST_CUST)
        self.assertEqual(len(first["invoices"]), 1)
        second = billing.create_invoices_for_charges(
            COMPANY, self.charges(), agreement=None, guest_customer=GUEST_CUST)
        self.assertEqual(second["invoices"], [])  # nothing new
        self.assertTrue(second["skipped"])

    def test_missing_personal_customer_blocks_guest_invoice(self):
        agreement = {"sponsor_customer": SPONSOR_CUST, "rules": [
            {"charge_category": ROOM_RATE, "coverage_percent": 100, "priority": 1}]}
        res = billing.create_invoices_for_charges(
            COMPANY, self.charges(), agreement=agreement, guest_customer=None)
        by = self.invoices_by_customer(res)
        # only the sponsor is invoiced; the 75 guest share is surfaced, not billed
        self.assertEqual(set(by), {SPONSOR_CUST})
        self.assertEqual(flt(by[SPONSOR_CUST].net_total, 2), 100)
        self.assertEqual(flt(sum(u["amount"] for u in res["unassigned"]), 2), 75)

    def test_actual_payer_on_behalf_of_company(self):
        agreement = {"sponsor_customer": SPONSOR_CUST, "rules": [{"coverage_percent": 100}]}
        res = billing.create_invoices_for_charges(
            COMPANY, self.charges(), agreement=agreement, guest_customer=GUEST_CUST)
        si = self.invoices_by_customer(res)[SPONSOR_CUST]
        pe = billing.collect_invoice_payment(
            si.name, actual_payer_name="John Guest (cash)", received_from_guest=None)
        pe_doc = frappe.get_doc("Payment Entry", pe)
        self.assertEqual(pe_doc.party, SPONSOR_CUST)          # AR stays on the company
        self.assertEqual(pe_doc.actual_payer_name, "John Guest (cash)")
        self.assertEqual(flt(self.si(si.name).outstanding_amount, 2), 0)

    def test_partial_then_final_payment(self):
        res = billing.create_invoices_for_charges(
            COMPANY, self.charges(), agreement=None, guest_customer=GUEST_CUST)
        si = self.invoices_by_customer(res)[GUEST_CUST]
        billing.collect_invoice_payment(si.name, amount=100)
        self.assertEqual(flt(self.si(si.name).outstanding_amount, 2), 75)
        billing.collect_invoice_payment(si.name, amount=75)
        self.assertEqual(flt(self.si(si.name).outstanding_amount, 2), 0)

    def test_cancel_invoice_reverses_gl_and_frees_source(self):
        res = billing.create_invoices_for_charges(
            COMPANY, self.charges(subset={ROOM_RATE}), agreement=None, guest_customer=GUEST_CUST)
        si_name = res["invoices"][0]
        self.assertEqual(self.receivable_debit(si_name, GUEST_CUST), 100)
        frappe.get_doc("Sales Invoice", si_name).cancel()
        # after cancel there are no active GL entries for the voucher
        self.assertEqual(self.gl(si_name), [])
        # the source charge is billable again (re-invoicing creates a new SI)
        again = billing.create_invoices_for_charges(
            COMPANY, self.charges(subset={ROOM_RATE}), agreement=None, guest_customer=GUEST_CUST)
        self.assertEqual(len(again["invoices"]), 1)

    def test_credit_note_partial_correction(self):
        res = billing.create_invoices_for_charges(
            COMPANY, self.charges(subset={ROOM_RATE, INCLUDED_MEAL}),
            agreement=None, guest_customer=GUEST_CUST)
        si = self.invoices_by_customer(res)[GUEST_CUST]  # 130 (room 100 + meal 30)
        from erpnext.controllers.sales_and_purchase_return import make_return_doc
        credit = make_return_doc("Sales Invoice", si.name)
        # credit only the meal line (rate 30); make_return_doc sets negative qty
        credit.items = [it for it in credit.items if flt(it.rate) == 30]
        credit.flags.ignore_permissions = True
        credit.insert()
        credit.submit()
        # The credit note is a standalone document reducing the party's receivable by 30.
        self.assertEqual(flt(credit.net_total, 2), -30)
        # Net receivable for the guest across invoice + credit note = 130 - 30 = 100.
        net_ar = self.receivable_debit(si.name, GUEST_CUST) + self.receivable_debit(credit.name, GUEST_CUST)
        self.assertEqual(flt(net_ar, 2), 100)


class TestReservationWiring(BillingTestBase):
    def test_build_agreement_from_routing(self):
        res = frappe.get_doc({
            "doctype": "Hotel Reservation", "is_company_guest": 1, "company": SPONSOR_CUST,
            "routing_instructions": [
                {"item_group": "Room Charges", "bill_to": "Company"},
                {"item_group": "F&B", "bill_to": "Guest"},
            ],
        })
        agreement = billing.build_agreement_from_reservation(res)
        self.assertEqual(agreement["sponsor_customer"], SPONSOR_CUST)
        self.assertEqual(len(agreement["rules"]), 1)
        self.assertEqual(agreement["rules"][0]["item_group"], "Room Charges")

    def test_generate_reservation_invoices_links_to_reservation(self):
        # minimal real reservation
        if not frappe.db.exists("Hotel Reception", "Main Reception"):
            frappe.get_doc({"doctype": "Hotel Reception", "reception_name": "Main Reception"}).insert(ignore_permissions=True)
        if not frappe.db.exists("Hotel Room Type", "Deluxe"):
            frappe.get_doc({"doctype": "Hotel Room Type", "room_type_name": "Deluxe",
                            "default_rate": 100, "max_adults": 2, "max_children": 1}).insert(ignore_permissions=True)
        if not frappe.db.exists("Hotel Room", "TST-201"):
            frappe.get_doc({"doctype": "Hotel Room", "room_number": "TST-201", "room_type": "Deluxe",
                            "hotel_reception": "Main Reception", "status": "Available"}).insert(ignore_permissions=True)
        gname = f"WiredGuest-{self.pfx}"
        frappe.get_doc({"doctype": "Guest", "full_name": gname, "guest_type": "Corporate",
                        "customer": GUEST_CUST}).insert(ignore_permissions=True)
        from frappe.utils import add_days, nowdate
        res = frappe.new_doc("Hotel Reservation")
        res.hotel_reception = "Main Reception"
        res.guest = gname
        res.room_type = "Deluxe"
        res.room = "TST-201"
        res.status = "Reserved"
        res.arrival_date = nowdate()
        res.departure_date = add_days(nowdate(), 1)
        res.flags.ignore_permissions = True
        res.insert()

        agreement = {"sponsor_customer": SPONSOR_CUST, "rules": [
            {"charge_category": ROOM_RATE, "coverage_percent": 100, "priority": 1}]}
        result = billing.generate_reservation_invoices(res.name, self.charges(), agreement=agreement)
        by = self.invoices_by_customer(result)
        self.assertEqual(flt(by[SPONSOR_CUST].net_total, 2), 100)
        self.assertEqual(flt(by[GUEST_CUST].net_total, 2), 75)
        # every invoice is traceable back to the reservation
        for name in result["invoices"]:
            self.assertEqual(self.si(name).hotel_reservation, res.name)
