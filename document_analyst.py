"""Evidence-first document analysis primitives used by the app and evaluator."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any


FIELD_LABELS = {
    "vendor": ("vendor", "supplier", "from"),
    "invoice_number": ("invoice number", "invoice no", "invoice #", "inv no"),
    "purchase_order_number": (
        "purchase order number",
        "purchase order no",
        "po number",
        "po no",
        "po #",
        "purchase order",
    ),
    "document_date": ("invoice date", "order date", "document date", "dated", "date"),
    "due_date": ("due date", "payment due"),
    "tax_amount": (
        "total gst amount",
        "gst amount",
        "total gst",
        "total tax",
        "tax amount",
        "cgst amount",
        "sgst amount",
        "igst amount",
    ),
    "total_amount": (
        "amount due",
        "grand total",
        "total amount",
        "order total",
        "total",
    ),
}

REQUIRED_FIELDS = {
    "invoice": ("vendor", "invoice_number", "document_date", "total_amount"),
    "purchase_order": ("vendor", "purchase_order_number", "document_date", "total_amount"),
    "delivery_note": ("vendor", "purchase_order_number", "document_date"),
    "email": ("vendor",),
    "policy": ("document_date",),
    "unknown": (),
}

FIELD_TITLES = {
    "vendor": "vendor or supplier",
    "invoice_number": "invoice number",
    "purchase_order_number": "purchase order number",
    "document_date": "document date",
    "due_date": "due date",
    "tax_amount": "GST or tax amount",
    "total_amount": "total amount",
}


def classify_document(text: str) -> str:
    normalized = text.lower()
    lines = [line.strip().lower() for line in text.splitlines() if line.strip()]
    first_line = lines[0] if lines else ""
    headings = lines[:3]
    has_invoice_heading = any(re.fullmatch(r"(?:tax\s+)?invoice", line) for line in headings)
    has_purchase_order_heading = any(re.fullmatch(r"purchase order", line) for line in headings)
    conflicting_headings = has_invoice_heading and has_purchase_order_heading

    if "delivery note" in first_line or "dispatch note" in first_line:
        return "delivery_note"
    if "purchase order" in first_line:
        return "unknown" if conflicting_headings else "purchase_order"
    if "invoice" in first_line or "tax invoice" in first_line:
        return "unknown" if conflicting_headings else "invoice"
    if "policy" in first_line:
        return "policy"
    if "from:" in first_line or "to:" in first_line or "subject:" in first_line or "dear " in first_line:
        return "email"

    signals = {
        "invoice": ("invoice", "amount due", "tax invoice"),
        "purchase_order": ("purchase order", "po number", "po no", "order total"),
        "delivery_note": ("delivery note", "dispatch note", "goods delivered"),
        "policy": ("policy", "terms and conditions", "coverage", "effective date"),
        "email": ("subject:", "from:", "to:", "dear "),
    }
    scores = {kind: sum(normalized.count(signal) for signal in words) for kind, words in signals.items()}
    if not any(scores.values()):
        return "unknown"
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "unknown"
    second_best = sorted(scores.values(), reverse=True)[1]
    if second_best == scores[best] and best in {"invoice", "purchase_order"}:
        return "unknown"
    return best


def _label_value(line: str, labels: tuple[str, ...]) -> str | None:
    for label in sorted(labels, key=len, reverse=True):
        boundary_label = re.escape(label)
        match = re.search(
            rf"(?<![A-Za-z0-9]){boundary_label}(?![A-Za-z0-9])\s*(?:number|no\.?|#)?\s*[:#-]?\s*(.+)$",
            line,
            re.I,
        )
        if match:
            value = match.group(1).strip().strip(" .;,#:")
            if re.search(r"[A-Za-z0-9]", value):
                return value
    return None


def _amount_from_line(line: str) -> tuple[str, str] | None:
    match = re.search(r"(?:₹|\bINR\b|\bRs\.?\s*)?\s*([\d][\d,]*(?:\.\d{1,2})?)", line, re.I)
    if not match:
        return None
    try:
        amount = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    currency = "INR" if re.search(r"₹|\bINR\b|\bRs\.?\s*", line, re.I) else ""
    return f"{amount:,.2f}", currency


def _tax_amount_from_table(lines: list[str]) -> dict[str, str] | None:
    tax_labels = FIELD_LABELS["tax_amount"]
    for header_index, line in enumerate(lines):
        if not any(re.search(rf"\b{re.escape(label)}\b", line, re.I) for label in tax_labels):
            continue
        lookahead_end = min(header_index + 32, len(lines))
        for total_index in range(header_index + 1, lookahead_end):
            if not re.match(r"^total\b", lines[total_index], re.I):
                continue
            amount_end = min(total_index + 5, lookahead_end)
            for amount_index in range(total_index, amount_end):
                parsed_amount = _amount_from_line(lines[amount_index])
                if not parsed_amount:
                    continue
                amount, currency = parsed_amount
                evidence = "\n".join(lines[header_index : amount_index + 1])
                if not currency and re.search(r"₹|\bINR\b|\bRs\.?\s*", evidence, re.I):
                    currency = "INR"
                result = {"value": amount, "evidence": evidence}
                if currency:
                    result["currency"] = currency
                return result
    return None


def extract_fields(text: str) -> dict[str, dict[str, str]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    fields: dict[str, dict[str, str]] = {}
    for field_name, labels in FIELD_LABELS.items():
        for line in lines:
            value = _label_value(line, labels)
            if field_name in {"total_amount", "tax_amount"} and value:
                parsed_amount = _amount_from_line(value)
                if parsed_amount:
                    amount, currency = parsed_amount
                    fields[field_name] = {"value": amount, "evidence": line}
                    if currency:
                        fields["currency"] = {"value": currency, "evidence": line}
                    break
            elif value:
                fields[field_name] = {"value": value, "evidence": line}
                break

    if "tax_amount" not in fields:
        table_tax_amount = _tax_amount_from_table(lines)
        if table_tax_amount:
            currency = table_tax_amount.pop("currency", None)
            fields["tax_amount"] = table_tax_amount
            if currency:
                fields["currency"] = {"value": currency, "evidence": table_tax_amount["evidence"]}

    if "currency" not in fields:
        for line in lines:
            if re.search(r"₹|\bINR\b|\bRs\.?\s*", line, re.I):
                fields["currency"] = {"value": "INR", "evidence": line}
                break
            if re.search(r"\bUSD\b|\$", line, re.I):
                fields["currency"] = {"value": "USD", "evidence": line}
                break
    return fields


def analyze_document(name: str, text: str) -> dict[str, Any]:
    document_type = classify_document(text)
    fields = extract_fields(text)
    missing = [
        f"What is the {FIELD_TITLES[field]}?"
        for field in REQUIRED_FIELDS[document_type]
        if field not in fields
    ]
    summary_details = [f"{FIELD_TITLES[field]}: {fields[field]['value']}" for field in REQUIRED_FIELDS[document_type] if field in fields]
    summary = f"Classified as {document_type.replace('_', ' ')}."
    if summary_details:
        summary += " " + "; ".join(summary_details) + "."
    return {
        "name": name,
        "document_type": document_type,
        "fields": fields,
        "summary": summary,
        "missing_questions": missing,
    }


CURRENCY_SYMBOLS = {"INR": "₹", "USD": "$"}


def to_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


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


def comparison_citations(comparison: dict[str, Any]) -> list[dict[str, str]]:
    citations = []
    for check in comparison.get("checks", []):
        for side, document in (("invoice", comparison.get("invoice_document")), ("purchase_order", comparison.get("purchase_order_document"))):
            detail = check.get(side)
            if detail and detail.get("evidence"):
                citations.append({"document": document, "quote": detail["evidence"]})
    return citations


def is_comparison_question(question: str) -> bool:
    normalized = question.lower()
    return bool(
        re.search(r"\b(?:match|matches|matching|mismatch|discrepanc\w*|conflict\w*|consistent|reconcile)\b", normalized)
        or ("invoice" in normalized and re.search(r"purchase order|\bpo\b", normalized))
    )


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


def answer_from_documents(
    question: str,
    analyses: list[dict[str, Any]],
    documents: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Answer from extracted fields or relevant source passages, retaining citations."""
    normalized = question.lower()
    if is_comparison_question(question):
        comparison = compare_documents(analyses)
        return {
            "answer": format_comparison_answer(comparison),
            "citations": comparison_citations(comparison),
            "comparison": comparison,
        }

    summary_intent = any(
        phrase in normalized
        for phrase in (
            "summarize",
            "summary",
            "what is this document",
            "what is this passage",
            "what is the document about",
            "what does this document say",
            "describe this document",
        )
    )
    if summary_intent and documents:
        citations = []
        for document in documents:
            lines = [line.strip() for line in document.get("text", "").splitlines() if line.strip()]
            if lines:
                quote = " ".join(lines[:4])
                citations.append({"document": document.get("name", "document.txt"), "quote": quote})
        if citations:
            return {
                "answer": " ".join(citation["quote"] for citation in citations),
                "citations": citations,
            }

    tax_cost_question = (
        re.search(r"\b(?:gst|tax|cgst|sgst|igst)\b", normalized)
        and re.search(r"\b(?:total|amount|cost|price|charge)\b", normalized)
    )
    if tax_cost_question:
        evidence = [
            {"document": analysis["name"], **analysis["fields"]["tax_amount"]}
            for analysis in analyses
            if "tax_amount" in analysis["fields"]
        ]
        if evidence:
            values = "; ".join(f"{item['value']} ({item['document']})" for item in evidence)
            return {
                "answer": values,
                "citations": [
                    {"document": item["document"], "quote": item["evidence"]}
                    for item in evidence
                ],
            }
        return {
            "answer": "I could not find a stated GST or tax amount in the supplied documents.",
            "citations": [],
        }

    field_terms = {
        "due_date": ("due date", "when is it due"),
        "invoice_number": ("invoice number", "invoice no", "invoice #"),
        "purchase_order_number": ("purchase order number", "po number", "po no"),
        "total_amount": ("total", "amount", "cost", "price"),
        "vendor": ("vendor", "supplier", "who sent", "who is the"),
        "document_date": (
            "document date", "invoice date", "order date", "delivery date",
            "effective date", "date", "when was it issued",
        ),
    }
    for field, terms in field_terms.items():
        if any(term in normalized for term in terms):
            evidence = [
                {"document": analysis["name"], "quote": detail["evidence"], "value": detail["value"]}
                for analysis in analyses
                if (detail := analysis["fields"].get(field))
            ]
            if evidence:
                values = "; ".join(f"{item['value']} ({item['document']})" for item in evidence)
                return {"answer": values, "citations": evidence}
            if field == "document_date":
                return {
                    "answer": "I could not find a document date in the supplied documents.",
                    "citations": [],
                }

    if documents:
        stop_words = {
            "about", "after", "before", "does", "from", "give", "have", "into",
            "is", "list", "me", "please", "show", "tell", "that", "the", "their",
            "there", "this", "those", "what", "when", "where", "which", "who",
            "with", "would", "you", "your",
        }
        query_terms = {
            token for token in re.findall(r"[a-z0-9]+", normalized)
            if len(token) > 2 and token not in stop_words
        }
        matches = []
        for document in documents:
            for line in document.get("text", "").splitlines():
                quote = line.strip()
                if not quote:
                    continue
                line_terms = set(re.findall(r"[a-z0-9]+", quote.lower()))
                overlap = query_terms & line_terms
                if overlap:
                    matches.append((len(overlap), quote, document.get("name", "document.txt")))
        matches.sort(key=lambda item: item[0], reverse=True)
        if matches:
            best_score = matches[0][0]
            selected = [item for item in matches if item[0] == best_score][:3]
            return {
                "answer": " ".join(item[1] for item in selected),
                "citations": [
                    {"document": item[2], "quote": item[1]} for item in selected
                ],
            }
    return {
        "answer": "I could not find a supported answer in the supplied documents.",
        "citations": [],
    }