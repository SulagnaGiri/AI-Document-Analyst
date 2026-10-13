"""Deterministic checks that must be exact, so they are done by code rather than the LLM.

Inputs are fields the LLM already extracted *and* that grounding.py verified against the
document lines they cite. Nothing here depends on document wording or layout.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from grounding import to_decimal

CURRENCY_SYMBOLS = {"INR": "₹", "USD": "$"}


def format_money(value: Any, currency: str | None = None) -> str:
    amount = to_decimal(value)
    if amount is None:
        return str(value)
    return f"{CURRENCY_SYMBOLS.get(currency or '', (currency + ' ') if currency else '')}{amount:,.2f}"


def _normalize_reference(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _normalize_name(value: str) -> str:
    words = re.findall(r"[a-z0-9]+", value.lower())
    return " ".join(word for word in words if word not in {"ltd", "limited", "pvt", "private", "inc", "llc", "co"})


def _pick_pair(invoices: list[dict[str, Any]], purchase_orders: list[dict[str, Any]]):
    """Prefer the invoice/PO pair where the invoice cites the PO's number."""
    for invoice in invoices:
        reference = invoice["fields"].get("purchase_order_number", {}).get("value")
        for purchase_order in purchase_orders:
            number = purchase_order["fields"].get("purchase_order_number", {}).get("value")
            if reference and number and _normalize_reference(reference) == _normalize_reference(number):
                return invoice, purchase_order
    return invoices[0], purchase_orders[0]


def _check(field: str, label: str, invoice: dict | None, purchase_order: dict | None, status: str) -> dict[str, Any]:
    return {
        "field": field,
        "label": label,
        "invoice": invoice,
        "purchase_order": purchase_order,
        "status": status,
    }


def compare_documents(analyses: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic invoice vs purchase-order check over already-extracted, evidenced fields.

    The status is decided here in code, never by the LLM, so a conflicting pair can't be
    talked into a MATCH.
    """
    invoices = [item for item in analyses if item["document_type"] == "invoice"]
    purchase_orders = [item for item in analyses if item["document_type"] == "purchase_order"]
    if not invoices or not purchase_orders:
        found = ", ".join(f"{item['name']} ({item['document_type'].replace('_', ' ')})" for item in analyses)
        return {
            "status": "INSUFFICIENT_DATA",
            "invoice_amount": None,
            "purchase_order_amount": None,
            "checks": [],
            "observations": [
                "A comparable invoice and purchase order were not both supplied."
                + (f" Documents provided: {found}." if found else "")
            ],
        }

    invoice, purchase_order = _pick_pair(invoices, purchase_orders)
    inv_fields, po_fields = invoice["fields"], purchase_order["fields"]
    inv_currency = inv_fields.get("currency", {}).get("value")
    po_currency = po_fields.get("currency", {}).get("value")
    checks = []
    observations = []

    # Total amount: the headline check.
    inv_total = inv_fields.get("total_amount")
    po_total = po_fields.get("total_amount")
    inv_amount = to_decimal(inv_total["value"]) if inv_total else None
    po_amount = to_decimal(po_total["value"]) if po_total else None
    if inv_amount is None or po_amount is None:
        missing = [name for name, amount in ((invoice["name"], inv_amount), (purchase_order["name"], po_amount)) if amount is None]
        observations.append(f"No total amount could be verified in: {', '.join(missing)}.")
        checks.append(_check("total_amount", "Total amount", inv_total, po_total, "MISSING"))
    elif inv_amount == po_amount:
        checks.append(_check("total_amount", "Total amount", inv_total, po_total, "MATCH"))
        observations.append("The invoice total equals the purchase order total.")
    else:
        checks.append(_check("total_amount", "Total amount", inv_total, po_total, "MISMATCH"))
        difference = abs(inv_amount - po_amount)
        direction = "higher" if inv_amount > po_amount else "lower"
        observations.append(
            f"The invoice total ({format_money(inv_amount, inv_currency)}) is "
            f"{format_money(difference, inv_currency or po_currency)} {direction} than the purchase order total "
            f"({format_money(po_amount, po_currency)}), a {difference / po_amount * 100:.1f}% difference."
            if po_amount else "The invoice and purchase order totals differ."
        )

    # Currency.
    if inv_currency and po_currency and inv_currency != po_currency:
        checks.append(_check("currency", "Currency", inv_fields.get("currency"), po_fields.get("currency"), "MISMATCH"))
        observations.append(f"The documents use different currencies ({inv_currency} vs {po_currency}).")

    # Vendor / supplier.
    inv_vendor, po_vendor = inv_fields.get("vendor"), po_fields.get("vendor")
    if inv_vendor and po_vendor:
        same = _normalize_name(inv_vendor["value"]) == _normalize_name(po_vendor["value"])
        checks.append(_check("vendor", "Vendor", inv_vendor, po_vendor, "MATCH" if same else "MISMATCH"))
        if not same:
            observations.append(f"The vendor differs: '{inv_vendor['value']}' on the invoice vs '{po_vendor['value']}' on the purchase order.")

    # The invoice's PO reference should point at this purchase order.
    inv_ref, po_number = inv_fields.get("purchase_order_number"), po_fields.get("purchase_order_number")
    if inv_ref and po_number:
        same = _normalize_reference(inv_ref["value"]) == _normalize_reference(po_number["value"])
        checks.append(_check("purchase_order_number", "PO reference", inv_ref, po_number, "MATCH" if same else "MISMATCH"))
        if not same:
            observations.append(f"The invoice references {inv_ref['value']}, but the purchase order number is {po_number['value']}.")
    elif po_number and not inv_ref:
        observations.append("The invoice does not reference a purchase order number, so the pairing could not be confirmed.")

    # Other supplied documents that mention the same PO (delivery notes, emails...).
    if po_number:
        target = _normalize_reference(po_number["value"])
        for item in analyses:
            reference = item["fields"].get("purchase_order_number", {}).get("value")
            if item is not invoice and item is not purchase_order and reference and _normalize_reference(reference) == target:
                observations.append(f"{item['name']} ({item['document_type'].replace('_', ' ')}) also refers to {po_number['value']}.")

    statuses = {check["status"] for check in checks}
    if checks and checks[0]["status"] == "MISSING":
        status = "INSUFFICIENT_DATA"
    elif "MISMATCH" in statuses:
        status = "MISMATCH"
    else:
        status = "MATCH"

    return {
        "status": status,
        "invoice_amount": inv_total["value"] if inv_total else None,
        "purchase_order_amount": po_total["value"] if po_total else None,
        "currency": inv_currency or po_currency,
        "invoice_document": invoice["name"],
        "purchase_order_document": purchase_order["name"],
        "checks": checks,
        "observations": observations,
    }


def check_internal_consistency(fields: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Arithmetic checks inside one document, run by code on verified fields."""
    issues = []
    parts = {name: to_decimal(fields[name]["value"]) for name in ("subtotal", "tax_amount", "total_amount") if name in fields}
    if len(parts) == 3 and None not in parts.values():
        expected = parts["subtotal"] + parts["tax_amount"]
        if abs(expected - parts["total_amount"]) > Decimal("1"):
            currency = fields.get("currency", {}).get("value")
            issues.append({
                "issue": (
                    f"Subtotal {format_money(parts['subtotal'], currency)} + tax {format_money(parts['tax_amount'], currency)} "
                    f"= {format_money(expected, currency)}, but the stated total is {format_money(parts['total_amount'], currency)}."
                ),
                "evidence": [fields[name]["evidence"] for name in ("subtotal", "tax_amount", "total_amount")],
                "source": "code",
            })
    return issues


def comparison_citations(comparison: dict[str, Any]) -> list[dict[str, str]]:
    citations = []
    for check in comparison.get("checks", []):
        for side, document in (("invoice", comparison.get("invoice_document")), ("purchase_order", comparison.get("purchase_order_document"))):
            detail = check.get(side)
            if detail and detail.get("evidence"):
                citations.append({"document": document, "quote": detail["evidence"], "lines": detail.get("lines", [])})
    return citations


def format_comparison_answer(comparison: dict[str, Any]) -> str:
    currency = comparison.get("currency")

    def amount(value):
        return format_money(value, currency) if value is not None else "Not found"

    lines = [
        "Invoice:", amount(comparison.get("invoice_amount")), "",
        "Purchase Order:", amount(comparison.get("purchase_order_amount")), "",
        "Status:", comparison["status"], "",
        "Observations:",
    ]
    lines += [f"- {item}" for item in comparison["observations"]] or ["- None."]
    return "\n".join(lines)
