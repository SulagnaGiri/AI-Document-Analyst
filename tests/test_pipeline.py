"""Tests for the LLM-first pipeline. The LLM is mocked; these check what code does with its output."""

import io
import json
import os
import unittest
from unittest.mock import patch

import app
import grounding
import llm_client
import pipeline

INVOICE = """TAX INVOICE
Vendor: Northstar Supplies
Invoice No: INV-204
Invoice Date: 2026-04-10
Purchase Order: PO-778
Subtotal: INR 44,400
Tax: INR 8,000
Grand Total: INR 52,400"""

PURCHASE_ORDER = """PURCHASE ORDER
Supplier: Northstar Supplies
PO Number: PO-778
Order Date: 2026-04-02
Order Total: INR 48,000"""


def llm_on():
    return patch.object(llm_client, "NVIDIA_API_KEY", "test-token")


def fake_analysis(document_type, fields):
    return {"document_type": document_type, "summary": "s", "fields": fields, "inconsistencies": [], "missing_information": []}


class GroundingTests(unittest.TestCase):
    def test_context_numbers_non_blank_lines(self):
        context = grounding.format_context([{"name": "a.txt", "text": "TITLE\n\nTotal: 5"}])
        self.assertIn('<document id="D1" name="a.txt">', context)
        self.assertIn("L1: TITLE\nL2: Total: 5", context)

    def test_citations_accept_ranges_names_and_drop_invalid_lines(self):
        documents = [{"name": "a.txt", "text": "one\ntwo\nthree"}, {"name": "b.txt", "text": "x"}]
        resolved = grounding.resolve_citations(
            [{"document": "D1", "lines": ["L1-L2", 99]}, {"document": "b.txt", "lines": [1]}, {"document": "D9", "lines": [1]}],
            documents,
        )
        self.assertEqual(resolved, [
            {"document": "a.txt", "lines": [1, 2], "quote": "one\ntwo"},
            {"document": "b.txt", "lines": [1], "quote": "x"},
        ])

    def test_value_must_appear_on_cited_line(self):
        self.assertTrue(grounding.value_supported("52,400", "Grand Total: INR 52,400"))
        self.assertTrue(grounding.value_supported("Northstar Supplies", "Vendor: Northstar Supplies"))
        self.assertFalse(grounding.value_supported("48,000", "Grand Total: INR 52,400"))
        self.assertFalse(grounding.value_supported("Acme Ltd", "Vendor: Northstar Supplies"))

    def test_non_latin_values_can_be_verified(self):
        self.assertTrue(grounding.value_supported("शर्मा ट्रेडर्स", "विक्रेता: शर्मा ट्रेडर्स"))
        self.assertFalse(grounding.value_supported("गुप्ता स्टोर्स", "विक्रेता: शर्मा ट्रेडर्स"))

    def test_calculated_figure_is_flagged_not_rejected(self):
        documents = [{"name": "inv.pdf", "text": "Fan 48 | 72.0 Pc.\nFan 48 i30 | 12.0 Pc."}]
        citations = grounding.resolve_citations([{"document": "D1", "lines": [1, 2]}], documents)
        grade = grounding.grade_answer("84 fans", citations, documents, "how many 48 inch fans?")
        self.assertEqual(grade["verification"], "partially_verified")
        self.assertIn("84", grade["verification_note"])


class AnswerTests(unittest.TestCase):
    def test_reasoned_answer_with_line_citations_is_verified(self):
        documents = [{"name": "Invoice #26.pdf", "text": "TAX INVOICE\nCGST | 36734.5\nSGST | 36734.5\nTotal | ₹ 73469.0"}]
        reply = {"action": "answer", "answer": "Total GST is ₹73,469.00.", "explanation": "CGST + SGST.",
                 "citations": [{"document": "D1", "lines": ["L2-L4"]}]}
        with llm_on(), patch.object(pipeline, "_llm_answer_question", return_value=reply):
            result = pipeline.answer("Total GST", documents)

        self.assertEqual(result["answer_source"], "nvidia")
        self.assertEqual(result["verification"], "verified")
        self.assertEqual(result["citations"][0]["document"], "Invoice #26.pdf")
        self.assertIn("CGST | 36734.5", result["citations"][0]["quote"])
        self.assertEqual(result["explanation"], "CGST + SGST.")

    def test_invented_figure_is_labelled_not_hidden(self):
        documents = [{"name": "inv.txt", "text": INVOICE}]
        reply = {"action": "answer", "answer": "The total is INR 99,999.", "citations": [{"document": "D1", "lines": [8]}]}
        with llm_on(), patch.object(pipeline, "_llm_answer_question", return_value=reply):
            result = pipeline.answer("What is the total?", documents)
        self.assertEqual(result["verification"], "partially_verified")
        self.assertIn("99,999", result["verification_note"])

    def test_answer_without_valid_citations_is_unverified(self):
        documents = [{"name": "inv.txt", "text": INVOICE}]
        reply = {"action": "answer", "answer": "Northstar Supplies.", "citations": [{"document": "D1", "lines": [42]}]}
        with llm_on(), patch.object(pipeline, "_llm_answer_question", return_value=reply):
            result = pipeline.answer("Who is the vendor?", documents)
        self.assertEqual(result["verification"], "unverified")
        self.assertEqual(result["citations"], [])

    def test_refusal_is_a_missing_information_answer(self):
        documents = [{"name": "po.txt", "text": PURCHASE_ORDER}]
        reply = {"action": "answer", "answer": "I cannot determine this from the documents; the PO lists no items.",
                 "citations": [{"document": "D1", "lines": [5]}]}
        with llm_on(), patch.object(pipeline, "_llm_answer_question", return_value=reply):
            result = pipeline.answer("Was everything delivered?", documents)
        self.assertEqual(result["answer_source"], "nvidia")
        self.assertEqual(result["verification"], "not_in_documents")

    def test_llm_failure_is_reported_not_guessed(self):
        documents = [{"name": "inv.txt", "text": INVOICE}]
        with llm_on(), patch.object(pipeline, "_llm_answer_question", return_value=None):
            with self.assertRaises(pipeline.LLMUnavailable) as raised:
                pipeline.answer("What is the invoice number?", documents)
        self.assertEqual(raised.exception.reason, "request_failed")

    def test_api_returns_503_when_model_unavailable(self):
        with patch.object(llm_client, "NVIDIA_API_KEY", None):
            response = app.app.test_client().post("/api/answer", json={
                "question": "Total?", "documents": [{"name": "inv.txt", "text": INVOICE}],
            })
        self.assertEqual(response.status_code, 503)
        self.assertIn("NVIDIA_API_KEY", response.get_json()["error"])

    def test_compare_action_runs_code_comparison_and_llm_cannot_fake_status(self):
        documents = [{"name": "invoice.txt", "text": INVOICE}, {"name": "po.txt", "text": PURCHASE_ORDER}]
        analyses = {
            # The model claims the invoice total is 48,000 while citing the 52,400 line: rejected.
            "invoice.txt": fake_analysis("invoice", {
                "vendor": {"value": "Northstar Supplies", "lines": [2]},
                "purchase_order_number": {"value": "PO-778", "lines": [5]},
                "total_amount": {"value": "48000", "lines": [8]},
            }),
            "po.txt": fake_analysis("purchase_order", {
                "vendor": {"value": "Northstar Supplies", "lines": [2]},
                "purchase_order_number": {"value": "PO-778", "lines": [3]},
                "total_amount": {"value": "48,000", "lines": [5]},
            }),
        }
        with llm_on(), patch.object(pipeline, "_llm_answer_question", return_value={"action": "compare_invoice_po", "answer": "They match."}), \
                patch.object(pipeline, "_llm_analyze_document", side_effect=lambda name, text: analyses[name]):
            pipeline._analysis_cache.clear()
            result = pipeline.answer("Is this bill in line with what we ordered?", documents)

        comparison = result["comparison"]
        self.assertEqual(result["answer_source"], "verified_comparison")
        self.assertEqual(comparison["status"], "INSUFFICIENT_DATA")
        self.assertIsNone(comparison["invoice_amount"])
        self.assertNotIn("They match", result["answer"])

    def test_compare_flags_mismatch_with_verified_values(self):
        documents = [{"name": "invoice.txt", "text": INVOICE}, {"name": "po.txt", "text": PURCHASE_ORDER}]
        analyses = {
            "invoice.txt": fake_analysis("invoice", {"total_amount": {"value": "52,400", "lines": [8]},
                                                     "purchase_order_number": {"value": "PO-778", "lines": [5]}}),
            "po.txt": fake_analysis("purchase_order", {"total_amount": {"value": "48,000", "lines": [5]},
                                                       "purchase_order_number": {"value": "PO-778", "lines": [3]}}),
        }
        with llm_on(), patch.object(pipeline, "_llm_analyze_document", side_effect=lambda name, text: analyses[name]):
            pipeline._analysis_cache.clear()
            comparison = pipeline.compare(documents)
        self.assertEqual(comparison["status"], "MISMATCH")
        self.assertEqual(comparison["invoice_amount"], "52,400.00")
        self.assertEqual(comparison["purchase_order_amount"], "48,000.00")


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        pipeline._analysis_cache.clear()

    def test_fields_are_verified_against_cited_lines(self):
        reply = fake_analysis("invoice", {
            "vendor": {"value": "Northstar Supplies", "lines": [2]},
            "invoice_number": {"value": "INV-204", "lines": [3]},
            "document_date": {"value": "2026-04-10", "lines": [2]},  # wrong line
            "total_amount": {"value": "52,400", "lines": [8]},
        })
        with llm_on(), patch.object(pipeline, "_llm_analyze_document", return_value=reply):
            analysis = pipeline.analyze("invoice.txt", INVOICE)

        self.assertTrue(analysis["fields"]["vendor"]["verified"])
        self.assertFalse(analysis["fields"]["document_date"]["verified"])
        self.assertEqual(analysis["fields"]["total_amount"]["value"], "52,400.00")
        # An unverified required field still counts as missing.
        self.assertIn("What is the document date?", analysis["missing_questions"])

    def test_non_invoice_types_use_their_own_required_fields(self):
        text = "From: buyer@example.com\nSubject: Question about invoice INV-204\nPlease confirm the total."
        reply = fake_analysis("email", {"sender": {"value": "buyer@example.com", "lines": [1]}})
        with llm_on(), patch.object(pipeline, "_llm_analyze_document", return_value=reply):
            analysis = pipeline.analyze("mail.eml", text)
        self.assertEqual(analysis["document_type"], "email")
        self.assertEqual(analysis["missing_questions"], ["What is the subject?"])
        self.assertNotIn("vendor", analysis["fields"])

    def test_code_catches_totals_that_do_not_add_up(self):
        text = "INVOICE\nSubtotal: 100\nTax: 18\nTotal: 120"
        reply = fake_analysis("invoice", {
            "subtotal": {"value": "100", "lines": [2]},
            "tax_amount": {"value": "18", "lines": [3]},
            "total_amount": {"value": "120", "lines": [4]},
        })
        with llm_on(), patch.object(pipeline, "_llm_analyze_document", return_value=reply):
            analysis = pipeline.analyze("bad.txt", text)
        self.assertEqual(len(analysis["inconsistencies"]), 1)
        self.assertEqual(analysis["inconsistencies"][0]["source"], "code")
        self.assertIn("118.00", analysis["inconsistencies"][0]["issue"])

    def test_unknown_type_from_model_is_normalized(self):
        with llm_on(), patch.object(pipeline, "_llm_analyze_document", return_value=fake_analysis("receipt", {})):
            analysis = pipeline.analyze("x.txt", "something")
        self.assertEqual(analysis["document_type"], "unknown")

    def test_amounts_with_symbols_are_normalized_and_currency_inferred_from_symbol(self):
        reply = fake_analysis("invoice", {
            "total_amount": {"value": "$840.00", "lines": [2]},
            "currency": {"value": "usd", "lines": [2]},
        })
        with llm_on(), patch.object(pipeline, "_llm_analyze_document", return_value=reply):
            analysis = pipeline.analyze("us.txt", "INVOICE\nTotal Due: $840.00")
        self.assertEqual(analysis["fields"]["total_amount"]["value"], "840.00")
        self.assertEqual(analysis["fields"]["currency"]["value"], "USD")
        self.assertTrue(analysis["fields"]["currency"]["verified"])

    def test_analysis_is_cached_per_document(self):
        reply = fake_analysis("invoice", {})
        with llm_on(), patch.object(pipeline, "_llm_analyze_document", return_value=reply) as mocked:
            pipeline.analyze("a.txt", INVOICE)
            pipeline.analyze("a.txt", INVOICE)
        self.assertEqual(mocked.call_count, 1)

    def test_rupee_symbol_supports_inr_but_word_suppliers_does_not(self):
        reply = fake_analysis("invoice", {"currency": {"value": "INR", "lines": [2]}})
        with llm_on(), patch.object(pipeline, "_llm_analyze_document", return_value=reply):
            self.assertTrue(pipeline.analyze("a.txt", "INVOICE\nTotal ₹ 100")["fields"]["currency"]["verified"])
        pipeline._analysis_cache.clear()
        with llm_on(), patch.object(pipeline, "_llm_analyze_document", return_value=reply):
            self.assertFalse(pipeline.analyze("b.txt", "INVOICE\nRAJU SUPPLIERS")["fields"]["currency"]["verified"])


class LlmClientTests(unittest.TestCase):
    @staticmethod
    def reply(content):
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

    def test_reads_api_key_from_project_env(self):
        self.assertEqual(os.environ.get("NVIDIA_API_KEY"), llm_client.NVIDIA_API_KEY)

    def test_uses_json_mode_and_auth(self):
        with llm_on(), patch("llm_client.urlopen", return_value=self.reply('{"ok": true}')) as mocked:
            self.assertEqual(llm_client.call_llm("sys", "Return JSON"), {"ok": True})
        request = mocked.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(json.loads(request.data)["response_format"], {"type": "json_object"})

    def test_invalid_json_is_retried_once(self):
        broken = '{"answer": "SEC 48"TRUST HIGHSPEED"}'
        with llm_on(), patch("llm_client.urlopen", side_effect=[self.reply(broken), self.reply('{"answer": "ok"}')]) as mocked:
            self.assertEqual(llm_client.call_llm("sys", "p"), {"answer": "ok"})
        self.assertEqual(mocked.call_count, 2)

    def test_incomplete_reply_is_retried(self):
        with llm_on(), patch("llm_client.urlopen", side_effect=[
            self.reply('{"document_type": "unknown"}'), self.reply('{"document_type": "invoice", "summary": "s", "fields": {}}'),
        ]) as mocked:
            result = llm_client.call_llm("sys", "p", required_keys=("document_type", "summary", "fields"))
        self.assertEqual(result["document_type"], "invoice")
        self.assertEqual(mocked.call_count, 2)

    def test_think_block_and_fences_are_stripped(self):
        content = '<think>CGST plus SGST...</think>\n```json\n{"answer": "73469"}\n```'
        with llm_on(), patch("llm_client.urlopen", return_value=self.reply(content)):
            self.assertEqual(llm_client.call_llm("sys", "p"), {"answer": "73469"})


class PdfTests(unittest.TestCase):
    def test_column_gaps_become_separators(self):
        class Page:
            def extract_text(self, extraction_mode=None):
                return "1     SEC 48\"FAN      84145120     72.0 Pc.\n\n   Subtotal     408161.08  "

        with patch("app.PdfReader", return_value=type("R", (), {"pages": [Page()]})()):
            text = app.pdf_to_text(b"%PDF")
        self.assertEqual(text, '1 | SEC 48"FAN | 84145120 | 72.0 Pc.\nSubtotal | 408161.08')


if __name__ == "__main__":
    unittest.main()
