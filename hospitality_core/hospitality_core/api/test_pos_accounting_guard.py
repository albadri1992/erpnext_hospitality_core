"""
Regression tests for the POS direct-GL guards in accounting.py.

Bug: the original reclassify_pos_taxes / redirect_pos_income_to_suspense posted direct GL
Entries on an individual POS Invoice (ERPNext v15 defers POS GL to shift consolidation).
When the tax/suspense accounts were unconfigured this posted to empty accounts and, once
configured, made POS Invoices impossible to cancel (LinkExistsError on a GL Entry the POS
cancel flow could not reverse).

These tests verify the guard: when the relevant accounts are NOT configured, the hooks are
a no-op and post no GL, so POS stays on standard ERPNext accounting and remains cancellable.

Run:
    bench --site <site> run-tests --module hospitality_core.hospitality_core.api.test_pos_accounting_guard
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from hospitality_core.hospitality_core.api.accounting import (
    reclassify_pos_taxes,
    redirect_pos_income_to_suspense,
)

COMPANY = "Grand Hotel"


class TestPosAccountingGuards(FrappeTestCase):
    def setUp(self):
        # Force the "no tax configured" state (the desired default: no fabricated tax).
        s = frappe.get_single("Hospitality Accounting Settings")
        for k in ["consumption_tax_account", "vat_account", "service_charge_account",
                  "income_suspense_account"]:
            setattr(s, k, None)
        s.flags.ignore_mandatory = True
        s.save(ignore_permissions=True)

    def _fake_pos(self, voucher, payments):
        return frappe._dict({
            "name": voucher, "company": COMPANY, "grand_total": 100.0,
            "posting_date": frappe.utils.nowdate(), "docstatus": 1,
            "payments": [frappe._dict(p) for p in payments],
        })

    def _gl_count(self, voucher):
        return frappe.db.count("GL Entry", {"voucher_no": voucher})

    def test_reclassify_pos_taxes_is_noop_when_tax_accounts_unconfigured(self):
        voucher = "TEST-POS-GUARD-RECLASSIFY"
        reclassify_pos_taxes(self._fake_pos(voucher, [{"mode_of_payment": "Cash", "amount": 100}]),
                             method="on_submit")
        self.assertEqual(self._gl_count(voucher), 0)  # no fabricated tax GL

    def test_redirect_income_to_suspense_is_noop_when_suspense_unconfigured(self):
        voucher = "TEST-POS-GUARD-REDIRECT"
        # A Guest Account payment would normally trigger the income->suspense direct GL.
        redirect_pos_income_to_suspense(
            self._fake_pos(voucher, [{"mode_of_payment": "Guest Account", "amount": 100}]),
            method="on_submit")
        self.assertEqual(self._gl_count(voucher), 0)  # no instant in/out GL
