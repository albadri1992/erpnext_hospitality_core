"""
SI-based billing & collection for Hospitality Core.

This is the operational layer on top of the coverage decision engine
(``coverage.resolve_liability``). It turns resolved liability shares into real
ERPNext documents:

- one ``Sales Invoice`` per liable ``Customer`` (revenue, receivable, tax and — when
  applicable — stock come from ERPNext, NOT from direct GL);
- collection via standard ``Payment Entry`` with references and allocation;
- full traceability: each ``Sales Invoice Item`` carries the source charge id and the
  coverage decision, and each invoice/payment links back to the reservation and guest.

Single-path guarantee (no double accounting): charges billed here are NEVER also posted
as ``Folio Transaction`` rows, so the legacy direct-GL hook
(``accounting.make_gl_entries_for_folio_transaction``) does not fire for them. The
Sales Invoice + Payment Entry are the single source of GL/AR for these charges.
"""

import json

import frappe
from frappe import _
from frappe.utils import flt, nowdate

from hospitality_core.hospitality_core.api.coverage import resolve_liability


# --------------------------------------------------------------------------------------
# Reservation -> coverage agreement (maps the existing Reservation Routing to the engine)
# --------------------------------------------------------------------------------------
def build_agreement_from_reservation(reservation):
    """
    Build a coverage agreement from an existing Hotel Reservation.

    The sponsor is ``reservation.company`` (a Customer, set for company guests).
    Each ``Reservation Routing`` row with ``bill_to == 'Company'`` becomes a 100%
    coverage rule for that Item Group; everything else stays on the guest.
    """
    if isinstance(reservation, str):
        reservation = frappe.get_doc("Hotel Reservation", reservation)

    if not reservation.get("is_company_guest") or not reservation.get("company"):
        return None

    rules = []
    for row in (reservation.get("routing_instructions") or []):
        if (row.get("bill_to") or "").strip() == "Company":
            rules.append({
                "item_group": row.get("item_group"),
                "coverage_percent": 100,
                "priority": 10,
            })

    return {"sponsor_customer": reservation.company, "rules": rules}


# --------------------------------------------------------------------------------------
# Idempotency helpers
# --------------------------------------------------------------------------------------
def _already_invoiced_sources(sources):
    """Return the subset of source ids already billed by a non-cancelled Sales Invoice."""
    if not sources:
        return set()
    rows = frappe.get_all(
        "Sales Invoice Item",
        filters={"hotel_charge_source": ["in", list(sources)], "docstatus": ["<", 2]},
        pluck="hotel_charge_source",
    )
    return set(rows)


def _company_defaults(company):
    return {
        "income_account": frappe.db.get_value("Company", company, "default_income_account"),
        "cost_center": frappe.db.get_value("Company", company, "cost_center"),
        "cash_account": frappe.db.get_value("Company", company, "default_cash_account"),
        "currency": frappe.db.get_value("Company", company, "default_currency"),
    }


# --------------------------------------------------------------------------------------
# Core: create one Sales Invoice per liable customer from unbilled charges
# --------------------------------------------------------------------------------------
def create_invoices_for_charges(company, charges, agreement=None, guest_customer=None,
                                reservation=None, guest=None):
    """
    Resolve liability for ``charges`` and create one submitted Sales Invoice per liable
    Customer. Idempotent: shares whose source id was already billed are skipped.

    Returns ``{"invoices": [names...], "unassigned": [shares...], "skipped": [sources...]}``.
    Guest shares with no personal Customer are returned under ``unassigned`` and are NOT
    invoiced (never routed to a default customer).
    """
    defaults = _company_defaults(company)
    if not defaults["income_account"]:
        frappe.throw(_("Company {0} has no Default Income Account.").format(company))

    results = resolve_liability(charges, agreement=agreement, guest_customer=guest_customer)
    charge_by_id = {c.get("id"): c for c in charges}

    # Bucket billable shares per customer; collect unassigned + already-billed.
    buckets = {}          # customer -> list of (source_id, share, charge)
    unassigned = []
    all_sources = []
    for row in results:
        charge = charge_by_id.get(row["charge_id"], {})
        for share in row["shares"]:
            if share["amount"] <= 0:
                continue
            source_id = f"{row['charge_id']}:{share['kind']}"
            all_sources.append(source_id)
            if share.get("requires_customer") or not share.get("customer"):
                unassigned.append({"source": source_id, "amount": share["amount"],
                                   "charge_id": row["charge_id"], "category": row["category"]})
                continue
            buckets.setdefault(share["customer"], []).append((source_id, share, charge))

    already = _already_invoiced_sources(all_sources)

    invoices = []
    skipped = []
    for customer, items in buckets.items():
        lines = []
        party_type = None
        for source_id, share, charge in items:
            if source_id in already:
                skipped.append(source_id)
                continue
            party_type = "Sponsor" if share["kind"] == "sponsor" else "Guest"
            rule = share.get("coverage_rule")
            note = (f"Covered by rule {rule}" if rule else
                    ("Sponsor share" if share["kind"] == "sponsor" else "Guest share"))
            lines.append({
                "item_code": charge.get("item_code"),
                "item_name": charge.get("description") or charge.get("category"),
                "description": f"{charge.get('category')} - {charge.get('description') or ''}".strip(" -"),
                "qty": 1,
                "rate": share["amount"],
                "income_account": defaults["income_account"],
                "cost_center": defaults["cost_center"],
                "hotel_charge_source": source_id,
                "hotel_coverage_note": note,
            })
        if not lines:
            continue

        si = frappe.new_doc("Sales Invoice")
        si.company = company
        si.customer = customer
        si.currency = defaults["currency"]
        si.set_posting_time = 1
        si.posting_date = nowdate()
        si.due_date = nowdate()
        si.hotel_reservation = reservation
        si.hotel_guest = guest
        si.hotel_liable_party_type = party_type
        for line in lines:
            si.append("items", line)
        si.flags.ignore_permissions = True
        si.insert()
        si.submit()
        invoices.append(si.name)

    return {"invoices": invoices, "unassigned": unassigned, "skipped": skipped}


# --------------------------------------------------------------------------------------
# Collection (standard ERPNext Payment Entry)
# --------------------------------------------------------------------------------------
def collect_invoice_payment(invoice, amount=None, paid_to=None, mode_of_payment=None,
                            actual_payer_name=None, received_from_guest=None,
                            reference_no=None):
    """
    Create & submit a Receive Payment Entry against a Sales Invoice, allocating either the
    full outstanding or a partial ``amount``. The accounting party stays the invoice's
    Customer; ``actual_payer_name`` / ``received_from_guest`` are documentation only.
    """
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    si = frappe.get_doc("Sales Invoice", invoice)
    pe = get_payment_entry("Sales Invoice", invoice)
    pe.paid_to = paid_to or _company_defaults(si.company)["cash_account"]
    if mode_of_payment:
        pe.mode_of_payment = mode_of_payment
    pe.reference_no = reference_no or invoice
    pe.reference_date = nowdate()

    if amount is not None:
        amount = flt(amount)
        pe.paid_amount = amount
        pe.received_amount = amount
        # Allocate the (partial) amount to the invoice reference.
        for ref in pe.references:
            ref.allocated_amount = min(flt(ref.outstanding_amount), amount)
        # For overpayment, leave the surplus unallocated (becomes advance on the party).

    if hasattr(pe, "actual_payer_name"):
        pe.actual_payer_name = actual_payer_name
    if hasattr(pe, "received_from_guest"):
        pe.received_from_guest = received_from_guest
    if hasattr(pe, "hotel_reservation"):
        pe.hotel_reservation = si.get("hotel_reservation")

    pe.flags.ignore_permissions = True
    pe.insert()
    pe.submit()
    return pe.name


def create_advance_payment(customer, company, amount, paid_to=None, mode_of_payment=None,
                           actual_payer_name=None, received_from_guest=None):
    """Record a deposit/advance as an unallocated Receive Payment Entry on the Customer."""
    defaults = _company_defaults(company)
    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = "Receive"
    pe.company = company
    pe.posting_date = nowdate()
    pe.party_type = "Customer"
    pe.party = customer
    pe.paid_to = paid_to or defaults["cash_account"]
    pe.paid_amount = flt(amount)
    pe.received_amount = flt(amount)
    if mode_of_payment:
        pe.mode_of_payment = mode_of_payment
    pe.reference_no = f"ADV-{customer}"
    pe.reference_date = nowdate()
    if hasattr(pe, "actual_payer_name"):
        pe.actual_payer_name = actual_payer_name
    if hasattr(pe, "received_from_guest"):
        pe.received_from_guest = received_from_guest
    pe.flags.ignore_permissions = True
    pe.insert()
    pe.submit()
    return pe.name


# --------------------------------------------------------------------------------------
# Whitelisted operational entry point (wires the engine into the reservation flow)
# --------------------------------------------------------------------------------------
@frappe.whitelist()
def generate_reservation_invoices(reservation, charges, agreement=None):
    """
    Generate per-liable-customer Sales Invoices for a reservation's unbilled charges.

    ``charges`` is a JSON list of charge dicts (id, category, item_code, amount, ...).
    ``agreement`` is optional; when omitted it is derived from the reservation's routing.
    """
    res = frappe.get_doc("Hotel Reservation", reservation)
    if isinstance(charges, str):
        charges = json.loads(charges)
    if isinstance(agreement, str) and agreement:
        agreement = json.loads(agreement)
    if agreement is None:
        agreement = build_agreement_from_reservation(res)

    guest_customer = frappe.db.get_value("Guest", res.guest, "customer") if res.guest else None
    return create_invoices_for_charges(
        company=frappe.defaults.get_user_default("Company")
        or frappe.db.get_single_value("Global Defaults", "default_company"),
        charges=charges,
        agreement=agreement,
        guest_customer=guest_customer,
        reservation=res.name,
        guest=res.guest,
    )
