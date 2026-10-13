import io
import json
import os
import unittest
from unittest.mock import patch

import app
from document_analyst import analyze_document, answer_from_documents, compare_documents


class DocumentAnalystTests(unittest.TestCase):
    def setUp(self):
        self.invoice_text = """TAX INVOICE
Vendor: Northstar Supplies
Invoice No: INV-204
Invoice Date: 2026-04-10
Grand Total: INR 52,400
"""
        self.po_text = """PURCHASE ORDER
Supplier: Northstar Supplies
PO Number: PO-778
Order Date: 2026-04-02
Order Total: INR 48,000
"""

    def test_classifies_and_extracts_invoice(self):
        analysis = analyze_document("invoice.txt", self.invoice_text)
        self.assertEqual(analysis["document_type"], "invoice")
        self.assertEqual(analysis["fields"]["invoice_number"]["value"], "INV-204")
        self.assertEqual(analysis["fields"]["total_amount"]["value"], "52,400.00")
        self.assertIn("Grand Total: INR 52,400", analysis["fields"]["total_amount"]["evidence"])

    def test_invoice_with_purchase_order_reference_stays_invoice(self):
        analysis = analyze_document(
            "invoice.txt",
            "TAX INVOICE\nInvoice Date: 2026-04-10\nPurchase Order: PO-778",
        )
        self.assertEqual(analysis["document_type"], "invoice")
        self.assertEqual(analysis["fields"]["document_date"]["value"], "2026-04-10")

    def test_flags_conflicting_totals(self):
        result = compare_documents([
            analyze_document("invoice.txt", self.invoice_text),
            analyze_document("po.txt", self.po_text),
        ])
        self.assertEqual(result["status"], "MISMATCH")
        self.assertEqual(result["invoice_amount"], "52,400.00")
        self.assertEqual(result["purchase_order_amount"], "48,000.00")

    def test_missing_required_field_generates_question(self):
        analysis = analyze_document("partial.txt", "INVOICE\nVendor: Northstar Supplies\n")
        self.assertIn("What is the total amount?", analysis["missing_questions"])

    def test_answers_unknown_question_without_inventing(self):
        analyses = [analyze_document("invoice.txt", self.invoice_text)]
        answer = answer_from_documents("What is the customer's bank account?", analyses)
        self.assertEqual(answer["citations"], [])
        self.assertIn("could not find", answer["answer"])

    def test_answers_passage_question_with_source_quote(self):
        text = "EXPENSE POLICY\nEmployees must submit receipts within 30 days.\nManager approval is required above INR 10,000."
        analysis = analyze_document("policy.txt", text)
        answer = answer_from_documents(
            "What does the policy say about receipts?",
            [analysis],
            [{"name": "policy.txt", "text": text}],
        )
        self.assertIn("submit receipts within 30 days", answer["answer"])
        self.assertTrue(answer["citations"])

    def test_supported_nvidia_answer_is_marked_as_llm(self):
        documents = [{"name": "policy.txt", "text": "Receipts are due within 30 days."}]
        llm_result = {
            "answer": "Receipts are due within 30 days.",
            "citations": [{"document": "policy.txt", "quote": "Receipts are due within 30 days."}],
        }
        with patch.object(app, "NVIDIA_API_KEY", "test-token"), patch.object(
            app, "_llm_answer_question", return_value=llm_result
        ) as mocked_answer:
            answer = app.answer_with_fallback("When are receipts due?", documents)

        mocked_answer.assert_called_once_with("When are receipts due?", documents)
        self.assertEqual(answer["answer_source"], "nvidia")
        self.assertEqual(answer["llm_status"], "answered")

    def test_answers_gst_cost_from_explicit_tax_amount(self):
        text = "INVOICE\nGST Number: 29ABCDE1234F1Z5\nTotal GST Amount: INR 2,400"
        analysis = analyze_document("invoice.txt", text)
        answer = answer_from_documents(
            "What is the total GST cost?",
            [analysis],
            [{"name": "invoice.txt", "text": text}],
        )
        self.assertIn("2,400.00", answer["answer"])
        self.assertEqual(answer["citations"][0]["quote"], "Total GST Amount: INR 2,400")

    def test_extracts_tax_total_from_split_pdf_table_lines(self):
        text = """TAX INVOICE
Total Tax Amount
37321.02
29279.75
6868.23
Total
₹
73469.00
"""
        analysis = analyze_document("invoice.pdf", text)
        answer = answer_from_documents(
            "What is the total GST cost?",
            [analysis],
            [{"name": "invoice.pdf", "text": text}],
        )

        self.assertEqual(analysis["fields"]["tax_amount"]["value"], "73,469.00")
        self.assertIn("73,469.00", answer["answer"])
        self.assertIn("Total Tax Amount", answer["citations"][0]["quote"])

    def test_extracts_invoice_date_from_dated_label(self):
        text = "TAX INVOICE\nInvoice No. ODS01262700015\nDated: 03-06-2026"
        analysis = analyze_document("invoice.pdf", text)
        answer = answer_from_documents(
            "Invoice date",
            [analysis],
            [{"name": "invoice.pdf", "text": text}],
        )

        self.assertEqual(analysis["fields"]["document_date"]["value"], "03-06-2026")
        self.assertIn("03-06-2026", answer["answer"])
        self.assertIn("Dated: 03-06-2026", answer["citations"][0]["quote"])

    def test_missing_invoice_date_does_not_match_invoice_headings(self):
        text = "TAX INVOICE\nINVOICE FROM\nINVOICE TO\nInvoice No. ODS01262700015"
        analysis = analyze_document("invoice.pdf", text)
        answer = answer_from_documents(
            "Invoice date",
            [analysis],
            [{"name": "invoice.pdf", "text": text}],
        )

        self.assertIn("could not find a document date", answer["answer"])
        self.assertEqual(answer["citations"], [])

    def test_gst_number_citation_cannot_answer_tax_cost_question(self):
        text = "INVOICE\nGST Number: 29ABCDE1234F1Z5\nTotal GST Amount: INR 2,400"
        documents = [{"name": "invoice.txt", "text": text}]
        llm_result = {
            "answer": "GST Number: 29ABCDE1234F1Z5",
            "citations": [{"document": "invoice.txt", "quote": "GST Number: 29ABCDE1234F1Z5"}],
        }
        with patch.object(app, "NVIDIA_API_KEY", "test-token"), patch.object(
            app, "_llm_answer_question", return_value=llm_result
        ):
            answer = app.answer_with_fallback("Total GST cost", documents)

        self.assertIn("2,400.00", answer["answer"])
        self.assertEqual(answer["citations"][0]["quote"], "Total GST Amount: INR 2,400")

    def test_reasoned_gst_answer_with_stitched_table_quote_is_accepted(self):
        text = "TAX INVOICE\nSubtotal 408161.08\nCGST 36734.5\nSGST 36734.5\nTotal Tax Amount\nTotal ₹ 73469.0"
        documents = [{"name": "Invoice #26.pdf", "text": text}]
        llm_result = {
            "answer": "Total GST is ₹73,469.00 (CGST ₹36,734.50 + SGST ₹36,734.50).",
            "explanation": "Added the CGST and SGST lines.",
            "citations": [
                {"document": "Invoice", "quote": "CGST 36734.5"},
                {"document": "Invoice", "quote": "Total Tax Amount\nTotal ₹ 73469.0"},
                {"document": "Invoice", "quote": "GST total 99999"},
            ],
        }
        with patch.object(app, "NVIDIA_API_KEY", "test-token"), patch.object(
            app, "_llm_answer_question", return_value=llm_result
        ):
            answer = app.answer_with_fallback("Total GST", documents)

        self.assertEqual(answer["answer_source"], "nvidia")
        self.assertIn("73,469.00", answer["answer"])
        self.assertEqual(answer["explanation"], "Added the CGST and SGST lines.")
        self.assertEqual(
            [citation["quote"] for citation in answer["citations"]],
            ["CGST 36734.5", "Total Tax Amount\nTotal ₹ 73469.0"],
        )
        self.assertTrue(all(c["document"] == "Invoice #26.pdf" for c in answer["citations"]))

    def test_double_escaped_quote_still_verifies(self):
        source = 'Description of Goods\nSEC 12" A/P\nSEC 12" MILLANO ALL PURPOSE'
        self.assertTrue(app._quote_in_source('SEC 12\\" A/P\\nSEC 12\\" MILLANO ALL PURPOSE', source))
        self.assertFalse(app._quote_in_source('SEC 99\\" FAN', source))

    def test_reasoning_model_think_block_is_stripped(self):
        content = '<think>CGST plus SGST...</think>\n```json\n{"answer": "73469"}\n```'
        response = io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())
        with patch.object(app, "NVIDIA_API_KEY", "test-token"), patch("app.urlopen", return_value=response):
            self.assertEqual(app._call_nvidia("Return JSON"), {"answer": "73469"})

    def test_summary_question_returns_document_passage(self):
        text = "EXPENSE POLICY\nEmployees must submit receipts within 30 days."
        analysis = analyze_document("policy.txt", text)
        answer = answer_from_documents(
            "What is this document about?",
            [analysis],
            [{"name": "policy.txt", "text": text}],
        )
        self.assertIn("submit receipts within 30 days", answer["answer"])

    def test_invalid_llm_date_uses_verified_extraction(self):
        llm_result = {
            "document_type": "invoice",
            "fields": {"document_date": {"value": ":", "evidence": "Invoice Date: :"}},
            "summary": "Classified as invoice. document date: :.",
            "missing_questions": [],
        }
        with patch.object(app, "NVIDIA_API_KEY", "test-token"), patch.object(
            app, "_llm_analyze_document", return_value=llm_result
        ):
            analysis = app.analyze_with_fallback(
                "invoice.txt", "TAX INVOICE\nInvoice Date: 2026-04-10"
            )

        self.assertEqual(analysis["fields"]["document_date"]["value"], "2026-04-10")
        self.assertNotIn("document date: :", analysis["summary"])

    def test_valid_llm_analysis_is_not_mixed_with_keyword_rules(self):
        text = "From: Priya\nSubject: Refund\nDear team, the total was wrong. Date: 2026-05-02"
        llm_result = {
            "document_type": "email",
            "fields": {"sender": {"value": "Priya", "evidence": "From: Priya"}},
            "summary": "Priya reports a wrong total.",
            "missing_questions": [],
        }
        with patch.object(app, "NVIDIA_API_KEY", "test-token"), patch.object(
            app, "_llm_analyze_document", return_value=llm_result
        ):
            analysis = app.analyze_with_fallback("mail.eml", text)

        self.assertEqual(list(analysis["fields"]), ["sender"])
        self.assertEqual(analysis["analysis_source"], "nvidia")

    def test_comparison_status_is_decided_by_code_not_llm(self):
        documents = [
            {"name": "invoice.txt", "text": self.invoice_text + "Purchase Order: PO-778\n"},
            {"name": "po.txt", "text": self.po_text},
        ]
        # The model claims the totals agree and invents a 48,000 invoice total.
        fake = {
            "document_type": "invoice",
            "fields": {"total_amount": {"value": "48000", "evidence": "Grand Total: INR 52,400"}},
        }
        with patch.object(app, "NVIDIA_API_KEY", "test-token"), patch.object(
            app, "_llm_extract_comparison_fields", return_value=fake
        ):
            answer = app.answer_with_fallback("Does this invoice match the purchase order?", documents)

        comparison = answer["comparison"]
        self.assertEqual(comparison["status"], "MISMATCH")
        self.assertEqual(comparison["invoice_amount"], "52,400.00")
        self.assertEqual(answer["answer_source"], "verified_comparison")
        self.assertIn("₹4,400.00 higher", answer["answer"])
        self.assertIn("Grand Total: INR 52,400", [c["quote"] for c in answer["citations"]])

    def test_verified_llm_total_fills_gap_left_by_rules(self):
        text = "TAX INVOICE\nRAJU SUPPLIERS\nSubtotal 408161.08\nTotal Payable\n₹\n481630.08"
        fields = {
            "total_amount": {"value": "481630.08", "evidence": "Total Payable\n₹\n481630.08"},
            "vendor": {"value": "RAJU SUPPLIERS", "evidence": "RAJU SUPPLIERS"},
            "invoice_number": {"value": "INV-999", "evidence": "TAX INVOICE"},
        }
        with patch.object(app, "NVIDIA_API_KEY", "test-token"), patch.object(
            app, "_llm_extract_comparison_fields", return_value={"document_type": "invoice", "fields": fields}
        ):
            analysis = app.analyze_for_comparison("invoice.pdf", text)

        self.assertEqual(analysis["fields"]["total_amount"]["value"], "481,630.08")
        self.assertEqual(analysis["fields"]["vendor"]["value"], "RAJU SUPPLIERS")
        self.assertNotIn("invoice_number", analysis["fields"])  # value not in its evidence

    def test_comparison_without_purchase_order_reports_insufficient_data(self):
        result = compare_documents([analyze_document("invoice.txt", self.invoice_text)])
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")
        self.assertIn("invoice.txt (invoice)", result["observations"][0])

    def test_cited_llm_refusal_is_kept_as_missing_information_answer(self):
        documents = [
            {"name": "po.txt", "text": self.po_text},
            {"name": "dn.txt", "text": "DELIVERY NOTE\nItems delivered: 40 office chairs"},
        ]
        llm_result = {
            "answer": "I cannot determine this from the documents.",
            "explanation": "The PO lists only a total, not items.",
            "citations": [{"document": "po.txt", "quote": "Order Total: INR 48,000"}],
        }
        with patch.object(app, "NVIDIA_API_KEY", "test-token"), patch.object(
            app, "_llm_answer_question", return_value=llm_result
        ):
            answer = app.answer_with_fallback("Was everything on the PO delivered?", documents)

        self.assertEqual(answer["answer_source"], "nvidia")
        self.assertIn("cannot determine", answer["answer"])

    def test_uncited_llm_refusal_uses_grounded_passage_fallback(self):
        documents = [{
            "name": "policy.txt",
            "text": "EXPENSE POLICY\nEmployees must submit receipts within 30 days.",
        }]
        llm_result = {
            "answer": "I could not find a supported answer in the supplied documents.",
            "citations": [],
        }
        with patch.object(app, "NVIDIA_API_KEY", "test-token"), patch.object(
            app, "_llm_answer_question", return_value=llm_result
        ):
            answer = app.answer_with_fallback("What does the policy say about receipts?", documents)

        self.assertIn("submit receipts within 30 days", answer["answer"])
        self.assertTrue(answer["citations"])
        self.assertEqual(answer["answer_source"], "python_rules")
        self.assertEqual(answer["llm_status"], "response_unsupported_or_uncited")

    def test_classifies_delivery_note_not_purchase_order(self):
        analysis = analyze_document(
            "delivery.txt",
            "DELIVERY NOTE\nVendor: Acme Ltd\nPO Number: PO-100\nDelivery Date: 2026-01-04",
        )
        self.assertEqual(analysis["document_type"], "delivery_note")

    def test_ambiguous_invoice_po_text_is_unknown(self):
        analysis = analyze_document("mixed.txt", "INVOICE\nPURCHASE ORDER\nTotal: INR 100")
        self.assertEqual(analysis["document_type"], "unknown")

    def test_extracts_hash_style_fields(self):
        analysis = analyze_document(
            "invoice.txt",
            "INVOICE\nVendor: Northwind\nInvoice #: NW-91\nAmount Due: $840.50",
        )
        self.assertEqual(analysis["fields"]["invoice_number"]["value"], "NW-91")

        po_analysis = analyze_document(
            "po.txt",
            "PURCHASE ORDER\nSupplier: Elm Group\nPO #: EG-55\nOrder Total: INR 9,000",
        )
        self.assertEqual(po_analysis["fields"]["purchase_order_number"]["value"], "EG-55")

    def test_reads_api_key_from_project_env(self):
        self.assertEqual(os.environ.get("NVIDIA_API_KEY"), app.NVIDIA_API_KEY)

    def test_nvidia_chat_response_is_parsed(self):
        response = io.BytesIO(
            json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode()
        )
        with patch.object(app, "NVIDIA_API_KEY", "test-token"), patch(
            "app.urlopen", return_value=response
        ) as mocked_urlopen:
            result = app._call_nvidia("Return JSON")

        self.assertEqual(result, {"ok": True})
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")


if __name__ == "__main__":
    unittest.main()