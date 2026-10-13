"""HTTP API for the document analyst. Logic lives in pipeline.py; this file only handles I/O."""

from __future__ import annotations

import io
import os
import re

from flask import Flask, jsonify, request
from flask_cors import CORS
from pypdf import PdfReader

import llm_client
import pipeline

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "data", "sample_documents")

app = Flask(__name__)
CORS(app)


@app.errorhandler(pipeline.LLMUnavailable)
def llm_unavailable(exc: pipeline.LLMUnavailable):
    return jsonify({"error": str(exc), "reason": exc.reason}), 503


def pdf_to_text(data: bytes) -> str:
    """Layout-preserving extraction, so each table row stays on one line.

    Runs of 3+ spaces (column gaps) become " | " to keep columns apart without wasting tokens.
    """
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except Exception:
            text = page.extract_text() or ""
        lines = [re.sub(r" {3,}", " | ", line.strip()) for line in text.splitlines()]
        pages.append("\n".join(line for line in lines if line))
    return "\n".join(pages)


def _documents_from(payload: dict) -> list[dict[str, str]]:
    return [
        {"name": item.get("name", "document.txt"), "text": item.get("text", "")}
        for item in payload.get("documents") or []
        if isinstance(item, dict) and item.get("text", "").strip()
    ]


@app.route("/")
def index():
    return "<html><body><h2>Document Analyst API</h2><p>Open the frontend on port 3010.</p></body></html>"


@app.route("/api/health")
def health():
    return jsonify({"llm_configured": llm_client.configured(), "model": llm_client.NVIDIA_MODEL})


@app.route("/api/upload", methods=["POST"])
def upload_document():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file uploaded."}), 400
    filename = file.filename or "upload.txt"
    data = file.read()
    if filename.lower().endswith(".pdf"):
        try:
            return jsonify({"name": filename, "text": pdf_to_text(data)})
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
    text = payload.get("text", "")
    if not text.strip():
        return jsonify({"error": "Document text is required."}), 400
    return jsonify(pipeline.analyze(payload.get("name", "document.txt"), text))


@app.route("/api/compare", methods=["POST"])
def compare_endpoint():
    documents = _documents_from(request.get_json(silent=True) or {})
    if len(documents) < 2:
        return jsonify({"error": "At least two documents are required to compare."}), 400
    return jsonify(pipeline.compare(documents))


@app.route("/api/answer", methods=["POST"])
def answer_endpoint():
    payload = request.get_json(silent=True) or {}
    question = payload.get("question", "")
    if not question.strip():
        return jsonify({"error": "Question is required."}), 400
    documents = _documents_from(payload)
    if not documents:
        return jsonify({"error": "At least one document is required to answer a question."}), 400
    return jsonify(pipeline.answer(question, documents))


@app.route("/api/samples")
def samples():
    documents = []
    for filename in sorted(os.listdir(SAMPLE_DIR)):
        with open(os.path.join(SAMPLE_DIR, filename), encoding="utf-8") as handle:
            documents.append({"name": filename, "text": handle.read()})
    return jsonify({"documents": documents})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(debug=True, host="0.0.0.0", port=port)
