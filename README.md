# AI Document Analyst

An LLM-powered system that reads business documents (invoices, purchase orders, delivery notes, emails and policies), extracts what matters, cross-checks documents against each other, and answers questions **using only the supplied documents, with line-level evidence**.

## Problem statement

A company receives documents from customers and suppliers in many formats. Build an AI system that can analyze a document and answer questions about it. It should:

1. identify the document type,
2. extract important fields,
3. summarize the document,
4. identify inconsistencies,
5. generate questions when information is missing,
6. answer user questions using only the supplied documents.

The headline challenge: given deliberately conflicting documents (an invoice for ₹52,400 against a purchase order for ₹48,000), the system must **report the discrepancy instead of blindly generating an answer**.

### Our understanding of the problem

* The input is **open-ended**: one type of document arrives in many layouts (label:value text, GST tables with CGST/SGST columns, letters, OCR-damaged scans, other languages). Keyword rules written for one layout do not carry over, so understanding has to come from an LLM.
* The output has to be **trustworthy**: a finance team cannot act on a number unless it can see where it came from. Every extracted value and answer therefore carries a citation to the exact document lines, and **code** verifies those citations.
* Some decisions must be **exact, not generated**: whether totals match, and whether subtotal + tax = total. Code makes these decisions, using only verified values.
* "I don't know" is a valid answer. Missing and conflicting information should be surfaced, not papered over.

## Solution overview

```
documents ──► numbered lines (L1, L2, …) ──► Nemotron 3 Super (reasoning LLM, JSON mode)
                                                   │  type, fields, summary, answers,
                                                   │  each citing line numbers
                                                   ▼
                                   code verifies every citation and value
                                                   │
                         ┌─────────────────────────┼──────────────────────────┐
                         ▼                         ▼                          ▼
              labelled answers          subtotal + tax = total       invoice vs PO status
     (verified / partially verified /     (checks.py)          (checks.py, verified values only)
      unverified / not in documents)
```

* **LLM** (`nvidia/nemotron-3-super-120b-a12b`): classification, extraction, summaries, understanding the question, reasoning (sums across rows, applying policy rules), and choosing when a question needs the invoice-vs-PO comparison.
* **Code**: PDF extraction that keeps table rows intact, line-numbered context, citation and value verification, arithmetic checks, the MATCH / MISMATCH / INSUFFICIENT_DATA decision, and required fields per document type.
* **UI** (React): upload several documents, analyze them, run the invoice vs purchase order check, and chat with all documents in context. Every answer shows its evidence and a verification label.

Example output for the seeded conflict (`Does this invoice match the purchase order?`):

```
Invoice:
₹52,400.00

Purchase Order:
₹48,000.00

Status:
MISMATCH

Observations:
- The invoice total (₹52,400.00) is ₹4,400.00 higher than the purchase order total (₹48,000.00), a 9.2% difference.
- delivery_note_778.txt (delivery note) also refers to PO-778.
```

## How to run

**Prerequisites:** Python 3.11+, Node 20+, and an NVIDIA API key from [build.nvidia.com](https://build.nvidia.com).

```bash
# 1. Backend (http://localhost:5001)
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # then put your NVIDIA_API_KEY in .env
python app.py

# 2. Frontend (http://localhost:3010), in a second terminal
cd frontend
npm install
npm run dev
```

Open http://localhost:3010 and click **Load sample set** to try the seeded invoice / PO conflict.

```bash
# Unit tests (LLM mocked, runs offline)
pytest

# Evaluation against the live model (about 10 minutes; needs NVIDIA_API_KEY)
python evaluation/run_eval.py
```

| Setting (`.env`) | Default | Purpose |
|---|---|---|
| `NVIDIA_API_KEY` | (required) | NVIDIA API key |
| `NVIDIA_MODEL` | `nvidia/nemotron-3-super-120b-a12b` | Any chat model on the NVIDIA API that supports JSON mode |
| `PORT` | `5001` | Backend port (the frontend proxies `/api` to it) |

## Team members

> **TODO:** add names and roles.

| Name | Role |
|---|---|
| | |

## Live application

Not deployed. Run locally as above.

## Demo

> **TODO (team):** add the demo video or screenshots link here.

## Repository guide

| Path | What it is |
|---|---|
| [`docs/design.md`](docs/design.md) | **Solution design**: decomposition, architecture diagram, where we use the LLM vs code, trade-offs |
| [`docs/prompts.md`](docs/prompts.md) | **Prompt engineering**: V1 → problem → V2 → … → final prompts, and why |
| [`docs/evaluation.md`](docs/evaluation.md) | **Evaluation results** on the 45-case test set |
| [`docs/failure_cases.md`](docs/failure_cases.md) | **Failure cases and improvements** (18 cases, 6 found by the evaluation) |
| [`docs/ai_usage.md`](docs/ai_usage.md) | **AI usage** during development |
| [`pipeline.py`](pipeline.py) | Analysis, Q&A and comparison; all prompts |
| [`grounding.py`](grounding.py) | Numbered-line context, citation and value verification, answer labels |
| [`checks.py`](checks.py) | Deterministic checks: invoice vs PO, subtotal + tax = total |
| [`document_types.py`](document_types.py) | Document types, their fields and required fields (configuration) |
| [`llm_client.py`](llm_client.py) | NVIDIA API client (JSON mode, retry, error logging) |
| [`app.py`](app.py) | Flask API and PDF text extraction |
| [`frontend/`](frontend) | React UI |
| [`evaluation/cases.json`](evaluation/cases.json) | Test set: 24 analysis, 7 comparison and 14 Q&A cases (normal, edge, ambiguous, failure) |
| [`evaluation/run_eval.py`](evaluation/run_eval.py) | Scores the live pipeline and writes `docs/evaluation.md` |
| [`tests/`](tests) | Unit tests for everything code is responsible for |
| [`data/sample_documents/`](data/sample_documents) | Demo document set with the seeded invoice / PO conflict |
