"""What each supported document type looks like.

This is configuration, not logic: adding a new document type means adding an entry here.
The LLM is shown these definitions when it analyzes a document; `required` drives the
"missing information" questions, and the canonical field names let code compare documents.
"""

from __future__ import annotations

DOCUMENT_TYPES: dict[str, dict] = {
    "invoice": {
        "description": "A bill from a supplier asking the buyer to pay for goods or services.",
        "fields": {
            "vendor": "company that issued the invoice (the seller, not the buyer)",
            "buyer": "company or person being billed",
            "invoice_number": "the invoice's own number",
            "document_date": "date the invoice was issued",
            "due_date": "payment due date",
            "purchase_order_number": "PO number the invoice refers to",
            "currency": "ISO code, e.g. INR or USD",
            "subtotal": "amount before tax",
            "tax_amount": "total tax / GST charged (all components together)",
            "total_amount": "final amount payable including tax",
            "line_items": "names of the goods/services billed, separated by semicolons",
        },
        "required": ["vendor", "invoice_number", "document_date", "total_amount"],
    },
    "purchase_order": {
        "description": "A buyer's order to a supplier, approving goods/services at a price.",
        "fields": {
            "vendor": "supplier the order is placed with",
            "buyer": "company placing the order",
            "purchase_order_number": "this purchase order's number",
            "document_date": "order date",
            "currency": "ISO code, e.g. INR or USD",
            "total_amount": "approved order total",
            "line_items": "names of the goods/services ordered, separated by semicolons",
            "approved_by": "who approved the order",
        },
        "required": ["vendor", "purchase_order_number", "document_date", "total_amount"],
    },
    "delivery_note": {
        "description": "Confirms goods were delivered, usually against a purchase order.",
        "fields": {
            "vendor": "supplier that delivered",
            "purchase_order_number": "PO the delivery is for",
            "document_date": "delivery date",
            "items_delivered": "what was delivered and how many",
            "received_by": "who received the goods",
        },
        "required": ["vendor", "purchase_order_number", "document_date", "items_delivered"],
    },
    "email": {
        "description": "A message between people or companies.",
        "fields": {
            "sender": "who sent it",
            "recipient": "who it was sent to",
            "subject": "subject line",
            "document_date": "date sent",
            "request": "what the sender is asking for or reporting",
            "purchase_order_number": "PO number mentioned, if any",
            "referenced_documents": "invoice/PO numbers or other documents mentioned",
        },
        "required": ["sender", "subject"],
    },
    "policy": {
        "description": "Rules or terms an organisation sets out (expenses, returns, insurance...).",
        "fields": {
            "policy_name": "title of the policy",
            "document_date": "effective date",
            "key_rules": "the main rules, limits and deadlines",
            "applies_to": "who the policy applies to",
        },
        "required": ["policy_name", "document_date"],
    },
    "unknown": {
        "description": "Anything that is not clearly one of the types above, or mixes several.",
        "fields": {},
        "required": [],
    },
}

FIELD_QUESTIONS = {
    "vendor": "Who is the vendor or supplier?",
    "invoice_number": "What is the invoice number?",
    "purchase_order_number": "What is the purchase order number?",
    "document_date": "What is the document date?",
    "total_amount": "What is the total amount?",
    "items_delivered": "What items were delivered?",
    "sender": "Who sent this message?",
    "subject": "What is the subject?",
    "policy_name": "What is the name of this policy?",
}


def describe_types_for_prompt() -> str:
    lines = []
    for name, spec in DOCUMENT_TYPES.items():
        lines.append(f"- {name}: {spec['description']}")
        for field, meaning in spec["fields"].items():
            required = " (required)" if field in spec["required"] else ""
            lines.append(f"    - {field}: {meaning}{required}")
    return "\n".join(lines)


def missing_field_questions(document_type: str, found_fields: set[str]) -> list[str]:
    spec = DOCUMENT_TYPES.get(document_type, DOCUMENT_TYPES["unknown"])
    return [
        FIELD_QUESTIONS.get(field, f"What is the {field.replace('_', ' ')}?")
        for field in spec["required"]
        if field not in found_fields
    ]
