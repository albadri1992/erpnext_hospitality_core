"""
Idempotent patch: add traceability custom fields that link ERPNext billing
documents back to the Hospitality domain, so every Sales Invoice / line / payment
can be traced to a reservation, guest, source charge and coverage decision.

Safe to run multiple times (create_custom_fields updates in place).
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
    "Sales Invoice": [
        {
            "fieldname": "hotel_reservation",
            "label": "Hotel Reservation",
            "fieldtype": "Link",
            "options": "Hotel Reservation",
            "insert_after": "customer",
            "read_only": 1,
        },
        {
            "fieldname": "hotel_guest",
            "label": "Hotel Guest",
            "fieldtype": "Link",
            "options": "Guest",
            "insert_after": "hotel_reservation",
            "read_only": 1,
        },
        {
            "fieldname": "hotel_liable_party_type",
            "label": "Hotel Liable Party Type",
            "fieldtype": "Select",
            "options": "\nSponsor\nGuest",
            "insert_after": "hotel_guest",
            "read_only": 1,
        },
    ],
    "Sales Invoice Item": [
        {
            "fieldname": "hotel_charge_source",
            "label": "Hotel Charge Source",
            "fieldtype": "Data",
            "insert_after": "item_code",
            "read_only": 1,
            "description": "Unique source id of the folio charge/share this line bills.",
        },
        {
            "fieldname": "hotel_coverage_note",
            "label": "Hotel Coverage Note",
            "fieldtype": "Small Text",
            "insert_after": "hotel_charge_source",
            "read_only": 1,
        },
    ],
    "Payment Entry": [
        {
            "fieldname": "hotel_reservation",
            "label": "Hotel Reservation",
            "fieldtype": "Link",
            "options": "Hotel Reservation",
            "insert_after": "party_name",
            "read_only": 1,
        },
        {
            "fieldname": "received_from_guest",
            "label": "Received From Guest",
            "fieldtype": "Link",
            "options": "Guest",
            "insert_after": "hotel_reservation",
            "description": "Operational proof-of-receipt only; does not change the accounting party.",
        },
        {
            "fieldname": "actual_payer_name",
            "label": "Actual Payer Name",
            "fieldtype": "Data",
            "insert_after": "received_from_guest",
            "description": "Who physically handed over the money; never the accounting party.",
        },
    ],
}


def execute():
    create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
    frappe.clear_cache()
