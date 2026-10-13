# Prompt engineering: how the prompts evolved

The final prompts are in [`pipeline.py`](../pipeline.py) (`SYSTEM_PROMPT`, `ANALYZE_PROMPT`, `ANSWER_PROMPT`). This page records how we got there:
**V1 → result → problem → improvement → V2 → … → final**.

Our test document for most of these iterations was a real GST tax invoice: a 16-row goods table with HSN codes, CGST and SGST, and the question **"Total GST"**.

---

## V1: one-shot JSON prompts, quoted evidence

Model: `meta/llama-3.3-70b-instruct`. Three separate user-only prompts.

```text
You are a document intelligence system. Analyze the supplied document and return valid JSON only.
Rules:
- Use only information explicitly present in the document text.
- Return a JSON object with keys: name, document_type, fields, summary, missing_questions.
- document_type must be one of: invoice, purchase_order, delivery_note, email, policy, unknown.
- fields must be a dictionary of extracted field names to objects like {"value": "...", "evidence": "..."}.
...
```

```text
Compare these documents and return valid JSON only.
Return a JSON object with keys:
- status: either MATCH, MISMATCH, or INSUFFICIENT_DATA
- invoice_amount, purchase_order_amount, observations
Use only the supplied document text. If totals differ, say so clearly.
```

```text
Answer the user's question using only the supplied document text.
Return valid JSON only with keys:
- answer: string
- citations: list of objects with keys document and quote
```

Answers were accepted only if a citation `quote` was an exact substring of the document **and**, for tax questions, contained a tax keyword and a number. Otherwise the system fell back to keyword rules.

**Result.** "Total GST" returned a block of table text found by keyword matching, not a reasoned answer.

**Problems identified.**
1. The model was not being used at all: the Llama model had been **retired (HTTP 410)**, and the error was logged only as `HTTPError`, so every answer silently came from the keyword fallback.
2. Even with a working model, the compare prompt let the LLM decide MATCH/MISMATCH with nothing checking it, which is exactly the "blindly generating an answer" the assignment warns about.
3. The prompts never asked the model to *reason*: no instruction to interpret short questions, read tables or calculate.

---

## V2: reasoning model, system prompt, "work out what the user means"

**Improvements.**
* Switched to **`nvidia/nemotron-3-super-120b-a12b`**, a reasoning model available to our key, and logged the HTTP status and message on failure.
* Added a system prompt: *"Read the supplied documents, reason about them (interpret tables, add up or compare figures, resolve synonyms such as GST = CGST + SGST + IGST)…"*
* The answer prompt now said: *"Work out what the user means, even if the question is short (e.g. 'Total GST' means the total GST/tax charged). Reason over the document: read tables, add or compare figures…"* and asked for an `explanation` field.
* Comparison was split: the LLM only **extracts** the fields (with quoted evidence); code verifies the values and decides the status.

**Result.** The model's reasoning was correct: *"The total GST charged is ₹73,469.00. The invoice shows CGST of ₹36,734.50 and SGST of ₹36,734.50; adding these gives ₹73,469.00."*

**Problems identified.** The reasoning was right, but our checks kept **throwing the answer away**:
* The model stitched a label and value into one quote (`"Total Tax Amount\n₹ 73469.0"`), which is not an exact substring of the PDF text.
* pypdf put every table cell on its own line, so quotes became bare numbers like `"14639.87"`.
* Product names contain inch marks (`SEC 48"TRUST`). About one reply in three had an unescaped `"`, so the JSON failed to parse.
* The model wrote `"document": "Invoice"` instead of the real file name.

We patched each one (allowing multi-line quotes, "include the label next to the figure", unescaping `\"`). Each patch fixed one case and the next question found another. The team's feedback at this point was that the process was **fragile and too focused on one invoice**, and that was correct.

---

## V3 (final): numbered lines, cite line numbers, label instead of reject

**Improvements.**
1. **Context engineering: numbered lines.** Each document is sent as
   `<document id="D1" name="Invoice #26.pdf">` with every line prefixed `L1:`, `L2:`, …
   The model cites `{"document": "D1", "lines": [30, 31]}`. Verification becomes exact: the line exists, and the value is on it.
2. **Layout-preserving PDF extraction.** Table rows stay on one line (`3 | SEC 48" i30 FAN | 84145120 | 18.0% | 12.0 Pc. | …`), which helps both reasoning and citation.
3. **JSON mode** (`response_format: json_object`) plus one retry.
4. **The model decides intent.** The answer prompt asks the model to choose an `action`: a normal answer, or `compare_invoice_po`, which hands over to the code-verified comparison. Regex intent detection was removed.
5. **Explicit handling of missing and conflicting information.** *"If the documents do not contain enough to answer, say 'I cannot determine this from the documents', explain what is missing… If documents disagree, point out the conflict instead of picking one side."*
6. **Type definitions in the prompt.** `ANALYZE_PROMPT` lists every document type with its fields from `document_types.py`, so policies, emails and delivery notes get their own fields instead of invoice fields.
7. **Answers are labelled, not rejected**: verified / partially verified / unverified / not in documents.

**Small final fixes found while testing V3.**
* The model wrote "document D1" in explanations, so we added: *"refer to documents by their name, not their id, and do not mention line numbers."*
* `line_items` came back as "See lines 13-28", so the field description became *"names of the goods/services billed, separated by semicolons"*.

**Result.** "Total GST" → `₹73,469.00`, **verified**, citing the CGST, SGST and total lines. "list the product" → all 16 items, **verified**. "how many fans of 48 inch?" → `84 fans`, **partially verified**, with the note "Not printed in the documents (likely calculated): 84". That is honest: 72 + 12 is a calculation. "Is this bill in line with what we ordered?" (no keyword overlap with "match") → the model chose `compare_invoice_po` and code returned MISMATCH.

### Final prompts

```text
SYSTEM_PROMPT
You are a careful document analyst for a company that receives invoices, purchase orders,
delivery notes, emails and policy documents.
- Think through the documents before answering: read tables row by row, add up or compare figures,
  and work out what the user actually means even if the question is short or informal.
- Use only what the documents say. Never invent values. If something is not stated, say so.
- Every document line is numbered (L1, L2, ...). Support each claim by citing the document id and line numbers.
- Reply with one JSON object only.
```

```text
ANALYZE_PROMPT
Analyze the document below.

Document types and the fields that matter for each:
{types}            ← generated from document_types.py

Return JSON:
{
  "document_type": one of invoice, purchase_order, delivery_note, email, policy, unknown,
  "summary": "2-3 sentence summary of what the document is and its key facts",
  "fields": { "<field name>": {"value": "<value as printed>", "lines": [<line numbers>]} },
  "inconsistencies": [{"issue": "<anything contradictory, miscalculated or suspicious>", "lines": [<line numbers>]}],
  "missing_information": ["<question to ask about important information the document should have but does not>"]
}

Rules:
- Use the field names listed for the chosen type. You may add other important fields in snake_case.
- Omit fields the document does not state. Do not guess.
- For amounts give the number as printed (e.g. "52,400" or "481630.08"); for currency give an ISO code.
- Choose "unknown" if the document is not clearly one type or mixes several.

{context}          ← numbered document
```

```text
ANSWER_PROMPT
Answer the user's question about the documents below.

First decide what the user wants:
- "compare_invoice_po": they ask whether an invoice matches / agrees with a purchase order, or about
  discrepancies between them. Code will then run a verified comparison; just set the action.
- "answer": anything else.

Return JSON:
{
  "action": "answer" | "compare_invoice_po",
  "answer": "direct, concise answer in plain language; amounts with currency",
  "explanation": "1-2 sentences on how you got it: which rows/figures you used and any calculation",
  "citations": [{"document": "<document id, e.g. D1>", "lines": [<line numbers>]}]
}

Rules:
- If the documents do not contain enough to answer, say "I cannot determine this from the documents",
  explain what is missing, and cite what they do say.
- If documents disagree, point out the conflict instead of picking one side.
- In the answer and explanation, refer to documents by their name, not their id, and do not mention line numbers.

Question: {question}

{context}          ← all documents, numbered
```

### Why the final prompts look like this

* **System prompt = behaviour, user prompt = task.** Reasoning style and grounding rules apply to every call, so they live in one place.
* **"Think through… read tables row by row… work out what the user means"** is what turns a short "Total GST" into "add CGST and SGST".
* **Line numbers instead of quotes** separate *finding* evidence (the model's job) from *checking* it (code's job), and remove the copying errors.
* **"Value as printed"** lets code compare the value against the cited line; normalization happens in code.
* **"Omit fields… do not guess"** together with code verification keeps invented values out of the comparison.
* **The `action` field** lets the model recognise a comparison request in any wording, while the verdict stays with code.
* **Explicit wording for "cannot determine"** lets code label missing-information answers consistently.
* **Type definitions generated from configuration** keep the prompt and the "required fields" logic in sync.
