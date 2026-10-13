"""Tests for the deterministic checks. Inputs are already-extracted fields, so no document wording is involved."""

import unittest

from checks import check_internal_consistency, compare_documents, format_comparison_answer, format_money


def field(value, evidence="", lines=(1,)):
    return {"value": value, "evidence": evidence or str(value), "lines": list(lines)}


def doc(name, document_type, **fields):
    return {"name": name, "document_type": document_type, "fields": {key: field(value) for key, value in fields.items()}}


INVOICE = dict(vendor="Northstar Supplies", purchase_order_number="PO-778", currency="INR")
PO = dict(vendor="Northstar Supplies", purchase_order_number="PO-778", currency="INR")


class CompareDocumentsTests(unittest.TestCase):
    def test_equal_totals_match(self):
        result = compare_documents([doc("i", "invoice", total_amount="48,000.00", **INVOICE),
                                    doc("p", "purchase_order", total_amount="48,000.00", **PO)])
        self.assertEqual(result["status"], "MATCH")

    def test_conflicting_totals_mismatch_with_difference(self):
        result = compare_documents([doc("i", "invoice", total_amount="52,400.00", **INVOICE),
                                    doc("p", "purchase_order", total_amount="48,000.00", **PO)])
        self.assertEqual(result["status"], "MISMATCH")
        self.assertIn("₹4,400.00 higher", result["observations"][0])
        self.assertIn("9.2%", result["observations"][0])

    def test_same_total_but_different_vendor_is_mismatch(self):
        result = compare_documents([doc("i", "invoice", total_amount="100", vendor="Acme Ltd"),
                                    doc("p", "purchase_order", total_amount="100", vendor="Northstar Supplies")])
        self.assertEqual(result["status"], "MISMATCH")
        self.assertTrue(any("vendor differs" in item for item in result["observations"]))

    def test_vendor_suffixes_are_ignored(self):
        result = compare_documents([doc("i", "invoice", total_amount="100", vendor="Acme Pvt. Ltd."),
                                    doc("p", "purchase_order", total_amount="100", vendor="ACME")])
        self.assertEqual(result["status"], "MATCH")

    def test_invoice_citing_another_po_is_mismatch(self):
        result = compare_documents([doc("i", "invoice", total_amount="100", purchase_order_number="PO-1"),
                                    doc("p", "purchase_order", total_amount="100", purchase_order_number="PO-2")])
        self.assertEqual(result["status"], "MISMATCH")

    def test_currency_mismatch_is_flagged(self):
        result = compare_documents([doc("i", "invoice", total_amount="100", currency="USD"),
                                    doc("p", "purchase_order", total_amount="100", currency="INR")])
        self.assertEqual(result["status"], "MISMATCH")

    def test_missing_total_is_insufficient_data(self):
        result = compare_documents([doc("i", "invoice", total_amount="100"), doc("p", "purchase_order", vendor="X")])
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")

    def test_no_purchase_order_is_insufficient_data(self):
        result = compare_documents([doc("i", "invoice", total_amount="100"), doc("e", "email")])
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")
        self.assertIn("i (invoice), e (email)", result["observations"][0])

    def test_pairs_the_invoice_that_cites_the_po(self):
        result = compare_documents([
            doc("other.txt", "invoice", total_amount="1", purchase_order_number="PO-9"),
            doc("right.txt", "invoice", total_amount="100", purchase_order_number="PO-778"),
            doc("po.txt", "purchase_order", total_amount="100", purchase_order_number="PO-778"),
            doc("dn.txt", "delivery_note", purchase_order_number="po 778"),
        ])
        self.assertEqual(result["invoice_document"], "right.txt")
        self.assertEqual(result["status"], "MATCH")
        self.assertTrue(any("dn.txt" in item for item in result["observations"]))

    def test_answer_uses_the_assignment_format(self):
        result = compare_documents([doc("i", "invoice", total_amount="52400", **INVOICE),
                                    doc("p", "purchase_order", total_amount="48000", **PO)])
        text = format_comparison_answer(result)
        self.assertTrue(text.startswith("Invoice:\n₹52,400.00\n\nPurchase Order:\n₹48,000.00\n\nStatus:\nMISMATCH"))


class InternalConsistencyTests(unittest.TestCase):
    def test_totals_that_add_up_raise_nothing(self):
        fields = {"subtotal": field("408,161.08"), "tax_amount": field("73,469.00"), "total_amount": field("481,630.08")}
        self.assertEqual(check_internal_consistency(fields), [])

    def test_totals_that_do_not_add_up_are_reported(self):
        fields = {"subtotal": field("100"), "tax_amount": field("18"), "total_amount": field("120")}
        issues = check_internal_consistency(fields)
        self.assertEqual(len(issues), 1)
        self.assertIn("118.00", issues[0]["issue"])

    def test_partial_fields_are_not_checked(self):
        self.assertEqual(check_internal_consistency({"total_amount": field("120")}), [])


class FormatTests(unittest.TestCase):
    def test_money(self):
        self.assertEqual(format_money("52400", "INR"), "₹52,400.00")
        self.assertEqual(format_money("840.5", "USD"), "$840.50")
        self.assertEqual(format_money("10", "EUR"), "EUR 10.00")
        self.assertEqual(format_money("TBD"), "TBD")


if __name__ == "__main__":
    unittest.main()
