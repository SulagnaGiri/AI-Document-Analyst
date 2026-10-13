from __future__ import annotations

import io
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from pypdf import PdfReader
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from document_analyst import (
    analyze_document,
    answer_from_documents,
    compare_documents,
    comparison_citations,
    format_comparison_answer,
    is_comparison_question,
    to_decimal,
)

ALLOWED_TYPES = {"invoice", "purchase_order", "delivery_note", "email", "policy", "unknown"}

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACEHUB_API_TOKEN")
DONUT_MODEL = os.environ.get("DONUT_MODEL", "naver-clova-ix/donut-base-finetuned-docvqa")
DONUT_API_URL = os.environ.get(
    "DONUT_API_URL", f"https://api-inference.huggingface.co/models/{DONUT_MODEL}"
)
MODEL_PROVIDER = os.environ.get("MODEL_PROVIDER", "donut" if (HF_TOKEN or DONUT_MODEL) else "nvidia")

NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY")
# Nemotron 3 Super is a reasoning model: it thinks before answering (returned
# separately as reasoning_content), which lets it add up tax lines, read tables, etc.
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "nvidia/nemotron-3-super-120b-a12b")
NVIDIA_API_URL = os.environ.get(
    "NVIDIA_API_URL", "https://integrate.api.nvidia.com/v1/chat/completions"
)

SYSTEM_PROMPT = (
    "You are a careful document analyst. Read the supplied documents, reason about them "
    "(interpret tables, add up or compare figures, resolve synonyms such as GST = CGST + SGST + IGST), "
    "and answer only from what the documents support. Always reply with a single valid JSON object and nothing else."
)

import base64

def _text_to_image_base64(text: str) -> str:
    """Render document text to a clean image canvas for Donut DocVQA."""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (850, 1100), color=(255, 255, 255))
        draw = ImageDraw.Draw(img)
        lines = text.splitlines()[:55]
        y = 40
        for line in lines:
            draw.text((40, y), line[:90], fill=(20, 20, 20))
            y += 18
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("utf-8")
    except Exception as exc:
        logging.getLogger(__name__).warning("Could not render text to image: %s", exc)
        return ""


def _call_donut_api(question: str, text: str) -> str | None:
    """Query Hugging Face Inference API for naver-clova-ix/donut-base-finetuned-docvqa."""
    if not HF_TOKEN:
        return None
    image_b64 = _text_to_image_base64(text)
    if not image_b64:
        return None
    endpoints = [
        f"https://router.huggingface.co/hf-inference/models/{DONUT_MODEL}",
        DONUT_API_URL,
    ]
    for endpoint in endpoints:
        try:
            payload = json.dumps({
                "inputs": {
                    "image": image_b64,
                    "question": question,
                }
            }).encode("utf-8")
            req = Request(
                endpoint,
                data=payload,
                headers={
                    "Authorization": f"Bearer {HF_TOKEN}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urlopen(req, timeout=35) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list) and len(data) > 0:
                return data[0].get("answer")
            if isinstance(data, dict):
                return data.get("answer")
        except Exception as exc:
            logging.getLogger(__name__).warning("Donut HF inference failed on %s: %s", endpoint, exc)
    return None


def _donut_extract_fields(name: str, text: str) -> dict:
    """Use Donut DocVQA to extract key fields."""
    extracted = {}
    queries = {
        "vendor": "Who is the vendor or supplier?",
        "invoice_number": "What is the invoice number?",
        "purchase_order_number": "What is the purchase order number?",
        "total_amount": "What is the grand total amount?",
        "tax_amount": "What is the total tax or GST amount?",
        "document_date": "What is the date of the document?",
    }
    for field, query in queries.items():
        ans = _call_donut_api(query, text)
        if ans and isinstance(ans, str) and ans.strip():
            clean_ans = ans.strip()
            if _quote_in_source(clean_ans, text):
                extracted[field] = {"value": clean_ans, "evidence": clean_ans, "source": "donut"}
    return extracted




def _clean_json_text(raw: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.S | re.I).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.I)
    if not cleaned.startswith("{"):
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if match:
            cleaned = match.group(0)
    return cleaned.strip()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).casefold().strip()


def _quote_in_source(quote: str, source: str) -> bool:
    """True if the quote is verbatim, or every line of a multi-line quote is in the source.

    Models often stitch a table label and its value ("Total Tax Amount\\n73469.0") into
    one quote; each piece is still real text from the document.
    """
    # Models sometimes double-escape inside JSON strings, sending a literal \n or \" instead of the character.
    quote = quote.replace("\\n", "\n").replace('\\"', '"')
    normalized_source = _normalize(source)
    if not _normalize(quote):
        return False
    if _normalize(quote) in normalized_source:
        return True
    parts = [_normalize(part) for part in re.split(r"[\n|]+", quote) if part.strip()]
    return len(parts) > 1 and all(part in normalized_source for part in parts)


def _format_documents(documents: list[dict[str, str]]) -> str:
    return "\n\n".join(
        f'=== Document name: "{item.get("name", "document.txt")}" ===\n{item.get("text", "")}'
        for item in documents
    )


def _call_nvidia(prompt: str, attempts: int = 2) -> dict | None:
    if not NVIDIA_API_KEY:
        return None
    for attempt in range(1, attempts + 1):
        result = _call_nvidia_once(prompt)
        if result is not _INVALID_JSON:
            return result
        logging.getLogger(__name__).warning("NVIDIA returned invalid JSON (attempt %s/%s)", attempt, attempts)
    return None


_INVALID_JSON = object()


def _call_nvidia_once(prompt: str):
    try:
        payload = json.dumps({
            "model": NVIDIA_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 8192,
            # JSON mode: without it, unescaped quotes in the document (e.g. 48" fan sizes) break the reply.
            "response_format": {"type": "json_object"},
        }).encode("utf-8")
        api_request = Request(
            NVIDIA_API_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {NVIDIA_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(api_request, timeout=180) as response:
            response_data = json.loads(response.read().decode("utf-8"))
        text = response_data["choices"][0]["message"]["content"]
        if not text:
            return None
        try:
            parsed = json.loads(_clean_json_text(text))
        except json.JSONDecodeError:
            return _INVALID_JSON
        return parsed if isinstance(parsed, dict) else None
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        logging.getLogger(__name__).warning(
            "NVIDIA request failed: HTTP %s for model %s: %s", exc.code, NVIDIA_MODEL, detail
        )
        return None
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "NVIDIA request failed (%s: %s)", type(exc).__name__, exc
        )
        return None


def _llm_analyze_document(name: str, text: str) -> dict | None:
    prompt = f"""
You are a document intelligence system. Analyze the supplied document and return valid JSON only.
Rules:
- Use only information explicitly present in the document text.
- Return a JSON object with keys: name, document_type, fields, summary, missing_questions.
- document_type must be one of: invoice, purchase_order, delivery_note, email, policy, unknown.
- fields must be a dictionary of extracted field names to objects like {{"value": "...", "evidence": "..."}}.
- summary must be concise and grounded in the document.
- missing_questions is a list of strings for required fields missing from the document.
- If unsure, choose unknown.

Document name: {name}
Document text:
{text}
"""
    return _call_nvidia(prompt)


COMPARISON_FIELDS = {
    "vendor": "the company that issued the invoice / will supply the goods (not the buyer)",
    "invoice_number": "the invoice's own number",
    "purchase_order_number": "this purchase order's number, or the PO number an invoice/delivery note/email refers to",
    "currency": "ISO code such as INR or USD",
    "subtotal": "amount before tax",
    "tax_amount": "total tax/GST charged (all tax components together)",
    "total_amount": "final total payable or order total, including tax",
}
AMOUNT_FIELDS = {"subtotal", "tax_amount", "total_amount"}


def _llm_extract_comparison_fields(name: str, text: str) -> dict | None:
    """The LLM only extracts values with evidence; it never decides MATCH/MISMATCH."""
    field_lines = "\n".join(f"- {field}: {meaning}" for field, meaning in COMPARISON_FIELDS.items())
    prompt = f"""
Extract fields from one business document so they can be compared with other documents by code.

Return JSON only:
{{"document_type": "invoice" | "purchase_order" | "delivery_note" | "email" | "policy" | "unknown",
  "fields": {{<field>: {{"value": "...", "evidence": "..."}} or null}}}}

Fields:
{field_lines}

Rules:
- Use null when the document does not state the field. Never guess or calculate a value that is not printed.
- For amounts, give the number only (e.g. "52400.00"), and make sure that number appears in the evidence.
- evidence must be copied character-for-character from the document, including the label next to the value,
  keeping the document's line breaks.

Document name: {name}
Document text:
{text}
"""
    return _call_nvidia(prompt)


def _verified_comparison_fields(llm_fields: Any, text: str) -> dict[str, dict[str, str]]:
    """Keep only LLM fields whose evidence is in the document and actually contains the value."""
    verified = {}
    if not isinstance(llm_fields, dict):
        return verified
    for field, details in llm_fields.items():
        if field not in COMPARISON_FIELDS or not isinstance(details, dict):
            continue
        value = str(details.get("value") or "").strip()
        evidence = str(details.get("evidence") or "").strip()
        if not value or not _quote_in_source(evidence, text):
            continue
        if field in AMOUNT_FIELDS:
            amount = to_decimal(value)
            evidence_amounts = {to_decimal(number) for number in re.findall(r"\d[\d,]*(?:\.\d+)?", evidence)}
            if amount is None or amount not in evidence_amounts:
                continue
            value = f"{amount:,.2f}"
        elif field == "currency":
            value = value.upper()
            if value not in {"INR", "USD", "EUR", "GBP"}:
                continue
        elif _normalize(value) not in _normalize(evidence):
            continue
        verified[field] = {"value": value, "evidence": evidence, "source": "nvidia"}
    return verified


def analyze_for_comparison(name: str, text: str) -> dict:
    """Rule-based fields, upgraded with LLM fields that pass verification."""
    analysis = analyze_document(name, text)
    for details in analysis["fields"].values():
        details.setdefault("source", "rules")
    if NVIDIA_API_KEY:
        llm_result = _llm_extract_comparison_fields(name, text)
        if isinstance(llm_result, dict):
            llm_type = llm_result.get("document_type")
            if llm_type in ALLOWED_TYPES and llm_type != "unknown" and analysis["document_type"] == "unknown":
                analysis["document_type"] = llm_type
            analysis["fields"].update(_verified_comparison_fields(llm_result.get("fields"), text))
    return analysis


def compare_with_verification(documents: list[dict[str, str]]) -> dict:
    with ThreadPoolExecutor(max_workers=6) as pool:
        analyses = list(pool.map(
            lambda item: analyze_for_comparison(item.get("name", "document.txt"), item.get("text", "")),
            documents,
        ))
    comparison = compare_documents(analyses)
    comparison["documents"] = [
        {"name": item["name"], "document_type": item["document_type"]} for item in analyses
    ]
    return comparison


def _llm_answer_question(question: str, documents: list[dict[str, str]]) -> dict | None:
    prompt = f"""
Answer the user's question about the documents below.

How to answer:
- Work out what the user means, even if the question is short (e.g. "Total GST" means the total GST/tax charged).
- Reason over the document: read tables, add or compare figures when needed, and state amounts with currency.
- Use only facts from the documents. If they do not contain enough to answer, say "I cannot determine this from the documents"
  and explain what information is missing, citing what the documents do say.
- If documents disagree with each other, point out the conflict instead of picking one.

Return JSON only, with keys:
- answer: a direct, concise answer in plain language (1-3 sentences).
- explanation: one or two sentences on how you arrived at it (which figures you used, any calculation).
- citations: list of {{"document": <exact document name>, "quote": <short text copied exactly from that document>}}.
  Each quote must be copied character-for-character from the document and should include the label
  next to the figure (e.g. "CGST\\n36734.5"), keeping the document's line breaks. Do not paraphrase.

Question: {question}

Documents:
{_format_documents(documents)}
"""
    return _call_nvidia(prompt)


def analyze_with_fallback(name: str, text: str) -> dict:
    fallback = analyze_document(name, text)
    fallback["model"] = DONUT_MODEL if MODEL_PROVIDER == "donut" else NVIDIA_MODEL

    if MODEL_PROVIDER == "donut" and HF_TOKEN:
        donut_fields = _donut_extract_fields(name, text)
        if donut_fields:
            fallback["fields"].update(donut_fields)
            fallback["analysis_source"] = "donut"
            return fallback

    if NVIDIA_API_KEY:
        llm_result = _llm_analyze_document(name, text)
        if isinstance(llm_result, dict):
            llm_type = llm_result.get("document_type")
            if llm_type not in ALLOWED_TYPES or (llm_type == "unknown" and fallback["document_type"] != "unknown"):
                llm_type = fallback["document_type"]

            llm_fields = llm_result.get("fields")
            validated_fields = {}
            invalid_field = not isinstance(llm_fields, dict)
            if isinstance(llm_fields, dict):
                for field, details in llm_fields.items():
                    if not isinstance(details, dict):
                        invalid_field = True
                        continue
                    value = str(details.get("value", "")).strip()
                    evidence = str(details.get("evidence", "")).strip()
                    if not re.search(r"[A-Za-z0-9]", value) or not _quote_in_source(evidence, text):
                        invalid_field = True
                        continue
                    validated_fields[field] = {"value": value, "evidence": evidence}

            # The keyword rules are invoice-oriented ("from" -> vendor, "total" -> amount), so
            # they are only used when the model gives us nothing verifiable, never mixed in.
            if validated_fields:
                llm_result["name"] = name
                llm_result["document_type"] = llm_type
                llm_result["fields"] = validated_fields
                if invalid_field or not isinstance(llm_result.get("summary"), str):
                    llm_result["summary"] = fallback["summary"]
                if not isinstance(llm_result.get("missing_questions"), list):
                    llm_result["missing_questions"] = fallback["missing_questions"]
                llm_result["analysis_source"] = "nvidia"
                llm_result["model"] = NVIDIA_MODEL
                return llm_result
    fallback["analysis_source"] = "python_rules"
    return fallback


def answer_with_fallback(question: str, documents: list[dict[str, str]]) -> dict:
    active_model = DONUT_MODEL if MODEL_PROVIDER == "donut" else NVIDIA_MODEL
    mentions_both = re.search(r"invoice", question, re.I) and re.search(r"purchase order|\bpo\b", question, re.I)
    if is_comparison_question(question) and (len(documents) > 1 or mentions_both):
        comparison = compare_with_verification(documents)
        return {
            "answer": format_comparison_answer(comparison),
            "citations": comparison_citations(comparison),
            "comparison": comparison,
            "answer_source": "verified_comparison",
            "model": active_model,
            "llm_status": "used_for_extraction" if (HF_TOKEN or NVIDIA_API_KEY) else "not_configured",
        }

    analyses = [
        analyze_document(item.get("name", "document.txt"), item.get("text", ""))
        for item in documents
        if item.get("text", "").strip()
    ]
    if not analyses:
        return {
            "answer": "I could not find enough document context to answer this question.",
            "citations": [],
            "answer_source": "python_rules",
            "model": active_model,
            "llm_status": "not_attempted_no_documents",
        }
    fallback = answer_from_documents(question, analyses, documents)
    fallback["answer_source"] = "python_rules"
    fallback["model"] = active_model
    fallback["llm_status"] = "not_configured"

    if MODEL_PROVIDER == "donut" and HF_TOKEN and len(documents) == 1:
        donut_ans = _call_donut_api(question, documents[0].get("text", ""))

        if donut_ans and isinstance(donut_ans, str) and donut_ans.strip():
            doc_name = documents[0].get("name", "document.txt")
            doc_text = documents[0].get("text", "")
            citations = []
            if _quote_in_source(donut_ans.strip(), doc_text):
                citations.append({"document": doc_name, "quote": donut_ans.strip()})
            return {
                "answer": donut_ans.strip(),
                "citations": citations,
                "answer_source": "donut",
                "llm_status": "answered",
                "model": DONUT_MODEL,
            }

    if NVIDIA_API_KEY:
        fallback["llm_status"] = "request_failed_or_invalid_response"
        llm_result = _llm_answer_question(question, documents)
        if isinstance(llm_result, dict):
            fallback["llm_status"] = "response_unsupported_or_uncited"
            answer = llm_result.get("answer")
            citations = llm_result.get("citations")
            valid_citations = []
            date_question = bool(
                re.search(r"\b(?:date|dated)\b|when.{0,20}(?:issued|dated)", question, re.I)
            )
            date_pattern = re.compile(
                r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\b"
            )
            tax_cost_question = (
                re.search(r"\b(?:gst|tax|cgst|sgst|igst)\b", question, re.I)
                and re.search(r"\b(?:total|amount|cost|price|charge)\b", question, re.I)
            )
            # A standalone number, so IDs such as a GSTIN ("29ABCDE1234F1Z5") don't count as amounts.
            amount_pattern = re.compile(r"(?<![A-Za-z0-9])\d[\d,]*(?:\.\d+)?(?![A-Za-z0-9%])")
            if isinstance(citations, list):
                for citation in citations:
                    if not isinstance(citation, dict):
                        continue
                    name = citation.get("document")
                    quote = str(citation.get("quote") or "")
                    # Trust the quote, not the model's spelling of the document name.
                    candidates = sorted(documents, key=lambda item: item.get("name") != name)
                    match = next(
                        (item for item in candidates if _quote_in_source(quote, item.get("text", ""))),
                        None,
                    )
                    if match:
                        valid_citations.append({"document": match.get("name"), "quote": quote})

            fallback_supported = "could not find" not in fallback["answer"].lower()
            is_refusal = isinstance(answer, str) and bool(
                re.search(r"could not find|cannot (?:be )?determine|not enough information", answer, re.I)
            )
            llm_supported = (
                isinstance(answer, str)
                and answer.strip()
                and valid_citations
                and (
                    # A cited "the documents don't say" is a real answer about missing information.
                    is_refusal
                    or (
                        (not date_question or date_pattern.search(answer))
                        and (not tax_cost_question or amount_pattern.search(answer))
                    )
                )
            )
            if llm_supported:
                result = {
                    "answer": answer.strip(),
                    "citations": valid_citations,
                    "answer_source": "nvidia",
                    "llm_status": "answered",
                    "model": NVIDIA_MODEL,
                }
                explanation = llm_result.get("explanation")
                if isinstance(explanation, str) and explanation.strip():
                    result["explanation"] = explanation.strip()
                return result
            if fallback_supported:
                return fallback

    return fallback


app = Flask(__name__)
CORS(app)


@app.route("/")
def index():
    try:
        return render_template("index.html")
    except Exception:
        return "<html><body><h2>AI Document Analyst API is running on port 5001.</h2><p>Access the React UI or use the API endpoints.</p></body></html>"


@app.route("/api/health")
def health():
    active_model = DONUT_MODEL if MODEL_PROVIDER == "donut" else NVIDIA_MODEL
    is_configured = bool(HF_TOKEN if MODEL_PROVIDER == "donut" else NVIDIA_API_KEY)
    return jsonify({
        "llm_configured": is_configured,
        "model": active_model,
        "provider": MODEL_PROVIDER,
        "donut_model": DONUT_MODEL,
        "nvidia_model": NVIDIA_MODEL,
    })


@app.route("/api/upload", methods=["POST"])
def upload_document():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file uploaded."}), 400

    filename = file.filename or "upload.txt"
    data = file.read()

    if filename.lower().endswith(".pdf"):
        try:
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            return jsonify({"name": filename, "text": text})
        except Exception as exc:
            return jsonify({"error": f"Could not parse PDF: {exc}"}), 400

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="replace")
    return jsonify({"name": filename, "text": text})


@app.route("/api/analyze", methods=["POST"])
def analyze_endpoint():
    payload = request.get_json(silent=True) or {}
    name = payload.get("name", "document.txt")
    text = payload.get("text", "")
    if not text.strip():
        return jsonify({"error": "Document text is required."}), 400
    return jsonify(analyze_with_fallback(name, text))


@app.route("/api/compare", methods=["POST"])
def compare_endpoint():
    payload = request.get_json(silent=True) or {}
    docs = payload.get("documents")
    if docs is None:  # Older two-field payload.
        docs = [
            {"name": payload.get("invoice_name", "invoice.txt"), "text": payload.get("invoice_text", "")},
            {"name": payload.get("po_name", "purchase_order.txt"), "text": payload.get("po_text", "")},
        ]
    valid_docs = [
        {"name": item.get("name", "document.txt"), "text": item.get("text", "")}
        for item in docs
        if item.get("text", "").strip()
    ]
    if len(valid_docs) < 2:
        return jsonify({"error": "At least two documents are required to compare."}), 400
    return jsonify(compare_with_verification(valid_docs))


SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "data", "sample_documents")


@app.route("/api/samples")
def samples():
    documents = []
    for filename in sorted(os.listdir(SAMPLE_DIR)):
        with open(os.path.join(SAMPLE_DIR, filename), encoding="utf-8") as handle:
            documents.append({"name": filename, "text": handle.read()})
    return jsonify({"documents": documents})


@app.route("/api/answer", methods=["POST"])
def answer_endpoint():
    payload = request.get_json(silent=True) or {}
    question = payload.get("question", "")
    docs = payload.get("documents", [])
    if not question.strip():
        return jsonify({"error": "Question is required."}), 400
    valid_docs = [
        {"name": item.get("name", "document.txt"), "text": item.get("text", "")}
        for item in docs
        if item.get("text", "").strip()
    ]
    if not valid_docs:
        return jsonify({"error": "At least one document is required to answer a question."}), 400
    return jsonify(answer_with_fallback(question, valid_docs))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(debug=True, host="0.0.0.0", port=port)

