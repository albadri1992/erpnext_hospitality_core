"""
Coverage decision engine for Hospitality Core.

This is the server-side, deterministic core of the billing model described in the
Hospitality Financial Architecture: it takes the consumption charges of a stay and
a Billing Agreement (a sponsor Customer plus a prioritised set of Coverage Rules)
and resolves, for every charge, *who is liable* and *for how much*.

Design rules enforced here (independent of how invoices are later produced):

1. Three parties are separated:
   - Guest        : the consumer (not necessarily the debtor).
   - Customer     : the party legally liable for a charge (sponsor_customer or the
                    guest's personal_customer). This is what a Sales Invoice.customer
                    must eventually be.
   - Actual Payer : whoever hands over the money. Not resolved here; it never changes
                    liability.
2. Per-charge decision order: exclusions -> eligibility (category / item / meal /
   validity window) -> rule priority -> coverage percent / cap -> sponsor share,
   remainder -> guest's personal customer.
3. The split is loss-less: sponsor_share + guest_share == original charge amount
   (within currency precision). A charge is never billed twice.
4. If a guest share is due but the guest has no personal Customer, that share is
   flagged `requires_customer` and is NOT silently assigned to any default customer.

The engine operates on plain dictionaries so it is fully unit-testable without the
database. A thin DocType-backed wrapper can be layered on top later.
"""

from frappe.utils import flt

# Charge categories (the source must set these explicitly; never inferred from names).
ROOM_RATE = "Room Rate"
INCLUDED_MEAL = "Included Meal"
EXTRA_MEAL = "Extra Meal"
ROOM_SERVICE = "Room Service"
MINIBAR = "Minibar"
BAR = "Bar"
OTHER = "Other"

DEFAULT_PRECISION = 2


def _within_validity(rule, service_time):
    """A rule only applies inside its [valid_from, valid_to] window (string/date safe)."""
    if not service_time:
        return True
    vf, vt = rule.get("valid_from"), rule.get("valid_to")
    if vf and str(service_time) < str(vf):
        return False
    if vt and str(service_time) > str(vt):
        return False
    return True


def _matches(rule, charge):
    """Whether a coverage rule targets a given charge."""
    if not _within_validity(rule, charge.get("service_time") or charge.get("service_date")):
        return False
    if rule.get("charge_category") and rule["charge_category"] != charge.get("category"):
        return False
    if rule.get("item_group") and rule["item_group"] != charge.get("item_group"):
        return False
    if rule.get("item_code") and rule["item_code"] != charge.get("item_code"):
        return False
    if rule.get("meal_type") and rule["meal_type"] != charge.get("meal_type"):
        return False
    return True


def resolve_liability(charges, agreement=None, guest_customer=None, precision=DEFAULT_PRECISION):
    """
    Resolve liability shares for every charge.

    Args:
        charges: list of dicts, each with at least ``amount`` and ``category``. Optional:
            ``id``, ``item_group``, ``item_code``, ``meal_type``, ``service_time``.
        agreement: optional dict ``{"sponsor_customer", "rules": [...]}``. Each rule may
            define ``charge_category``/``item_group``/``item_code``/``meal_type`` targeting,
            ``coverage_percent`` (default 100), ``amount_limit`` (cap on covered amount,
            shared across matching charges), ``priority`` (lower wins), ``exclude`` (bool),
            and ``valid_from``/``valid_to``.
        guest_customer: the guest's personal Customer (or ``None``).
        precision: currency precision for rounding the split.

    Returns:
        list of dicts: ``{"charge_id", "category", "amount", "shares": [...]}`` where each
        share is ``{"customer", "amount", "kind": "sponsor"|"guest", "coverage_rule",
        "requires_customer"}``. The sum of a charge's share amounts always equals the
        charge amount.
    """
    sponsor = (agreement or {}).get("sponsor_customer")
    rules = sorted((agreement or {}).get("rules", []), key=lambda r: r.get("priority", 100))
    cap_consumed = {}  # id(rule) -> covered so far, to honour amount_limit across charges
    results = []

    for charge in charges:
        amount = flt(charge.get("amount"), precision)
        chosen = None

        if sponsor and amount > 0:
            matches = [r for r in rules if _matches(r, charge)]
            # Exclusions are applied first: any matching exclude rule keeps the charge
            # entirely on the guest, even if a broader coverage rule also matches.
            if not any(r.get("exclude") for r in matches):
                chosen = next((r for r in matches if not r.get("exclude")), None)

        sponsor_amt = 0.0
        if chosen is not None:
            percent = flt(chosen.get("coverage_percent", 100), precision)
            covered = flt(amount * percent / 100.0, precision)
            limit = chosen.get("amount_limit")
            if limit is not None:
                key = id(chosen)
                remaining = max(flt(limit, precision) - cap_consumed.get(key, 0.0), 0.0)
                covered = min(covered, flt(remaining, precision))
                cap_consumed[key] = flt(cap_consumed.get(key, 0.0) + covered, precision)
            sponsor_amt = flt(covered, precision)

        guest_amt = flt(amount - sponsor_amt, precision)

        shares = []
        if sponsor_amt > 0:
            shares.append({
                "customer": sponsor,
                "amount": sponsor_amt,
                "kind": "sponsor",
                "coverage_rule": chosen,
                "requires_customer": False,
            })
        if guest_amt > 0:
            shares.append({
                "customer": guest_customer,
                "amount": guest_amt,
                "kind": "guest",
                "coverage_rule": None,
                "requires_customer": guest_customer is None,
            })

        results.append({
            "charge_id": charge.get("id"),
            "category": charge.get("category"),
            "amount": amount,
            "shares": shares,
        })

    return results


def aggregate_by_customer(results, precision=DEFAULT_PRECISION):
    """
    Roll resolved shares up per liable Customer (the basis for one Sales Invoice each).

    Returns ``(totals, unassigned)`` where ``totals`` maps customer -> amount for shares
    that have a Customer, and ``unassigned`` is the total of guest shares that still lack
    a personal Customer (these must be collected immediately or get a Customer before any
    credit invoice is raised — never routed to a default customer).
    """
    totals, unassigned = {}, 0.0
    for row in results:
        for share in row["shares"]:
            if share.get("requires_customer") or not share.get("customer"):
                unassigned = flt(unassigned + share["amount"], precision)
            else:
                totals[share["customer"]] = flt(totals.get(share["customer"], 0.0) + share["amount"], precision)
    return totals, flt(unassigned, precision)


def assert_split_invariant(results, precision=DEFAULT_PRECISION):
    """Raise AssertionError if any charge's shares do not sum back to the charge amount."""
    for row in results:
        total = flt(sum(s["amount"] for s in row["shares"]), precision)
        # A fully-zero charge legitimately produces no shares.
        expected = flt(row["amount"], precision)
        if not row["shares"] and expected == 0:
            continue
        assert total == expected, (
            f"Split invariant violated for charge {row.get('charge_id')}: "
            f"shares sum {total} != amount {expected}"
        )
    return True
