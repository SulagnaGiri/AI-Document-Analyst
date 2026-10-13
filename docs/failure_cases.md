# Failure cases and improvements

Every case below was observed while building and testing this system. Each has a regression test, an evaluation case, or both.

| # | Failure | Fixed? |
|---|---|---|
| 1 | The LLM was silently never called | ✅ |
| 2 | Correct, reasoned answers were thrown away by strict quote matching | ✅ |
| 3 | Inch marks in product names (`48"`) broke the model's JSON | ✅ |
| 4 | PDF tables lost their rows | ✅ |
| 5 | "I can't tell" was treated as failure and replaced with irrelevant text | ✅ |
| 6 | The LLM decided MATCH / MISMATCH with nothing checking it | ✅ |
| 7 | Invoice-only keyword rules leaked into other document types | ✅ |
| 8 | Values in Hindi could never be verified | ✅ |
| 9 | "Suppliers" verified the currency as INR | ✅ |
| 10 | Explanations can be wrong even when citations are right | ⚠️ open |
| 11 | A citation proves location, not meaning | ⚠️ open |

---

### 1. The LLM was silently never called

* **What went wrong:** Asking "Total GST" about a GST invoice returned a raw block of table text instead of an answer.
* **Why:** The configured model, `meta/llama-3.3-70b-instruct`, had been retired by NVIDIA (HTTP 410 Gone). The client logged only `NVIDIA request failed (HTTPError)` and the app fell back to keyword matching, so it *looked* like the LLM was answering badly.
* **What we changed:** Switched to `nvidia/nemotron-3-super-120b-a12b`, which we tested against the models our key can reach. Logged the HTTP status and NVIDIA's error message. Later removed the keyword fallback altogether: if the model is unreachable, the API returns **HTTP 503** with a clear message instead of a guess (`test_api_returns_503_when_model_unavailable`).

### 2. Correct, reasoned answers were thrown away by strict quote matching

* **What went wrong:** With Nemotron working, "Total GST" was still answered by the fallback, even though the model's own answer (CGST ₹36,734.50 + SGST ₹36,734.50 = ₹73,469.00) was right.
* **Why:** Answers were accepted only if each citation was an exact substring of the PDF text and, for tax questions, contained a tax keyword. The model cited `"Total Tax Amount\n₹ 73469.0"`, stitched from two places, and `"Total ₹ 73469.0"`, which has no "GST" in it. Both were rejected.
* **What we changed:** Redesigned grounding. Documents are sent with numbered lines and the model cites **line numbers**, which code checks exactly. Answers are no longer rejected; they get a label (verified / partially verified / unverified / not in documents). See `grounding.py` and [prompts.md](prompts.md#v3-final-numbered-lines-cite-line-numbers-label-instead-of-reject).

### 3. Inch marks in product names (`48"`) broke the model's JSON

* **What went wrong:** "list the product" worked on some tries and failed on others with the same document.
* **Why:** Product names like `SEC 48"TRUST HIGHSPEED` contain a double quote. Without JSON mode the model sometimes copied it unescaped, so the reply failed to parse: `Unterminated string starting at: line 7 column 16`.
* **What we changed:** Enabled `response_format: {"type": "json_object"}` (4 of 4 valid in a direct test, against about 2 of 3 before) and added one automatic retry on invalid JSON (`test_invalid_json_is_retried_once`). The evaluation reports the first-try JSON validity rate.

### 4. PDF tables lost their rows

* **What went wrong:** Citations came back as bare numbers (`"14639.87"`), and answers about specific rows were harder for the model.
* **Why:** pypdf's default extraction put every table cell on its own line, so a row like *item, HSN, qty, rate, amount* became nine separate lines with no visible connection.
* **What we changed:** Switched to pypdf's `extraction_mode="layout"`, turning column gaps into ` | ` so each row is one line (`app.pdf_to_text`, `test_column_gaps_become_separators`).

### 5. "I can't tell" was treated as failure and replaced with irrelevant text

* **What went wrong:** "Was everything on the PO delivered?" (sample set) returned unrelated lines.
* **Why:** The model correctly answered that it could not determine this (the PO has a total but no item list), citing both documents. Our validator treated any "could not find" answer as unsupported and fell back to keyword matching.
* **What we changed:** "Cannot determine" is now a legitimate result labelled `not_in_documents`. The prompt asks the model to explain what is missing (`test_refusal_is_a_missing_information_answer`). This is the assignment's "handle missing information" requirement.

### 6. The LLM decided MATCH / MISMATCH with nothing checking it

* **What went wrong:** The original compare endpoint returned the model's JSON (status, amounts, observations) with no checks. A model could answer MATCH for ₹52,400 vs ₹48,000.
* **Why:** The prompt asked the model for the verdict.
* **What we changed:** The model now only extracts fields with line citations. Code keeps only values found on the cited lines and computes the status in `checks.compare_documents`. In `test_compare_action_runs_code_comparison_and_llm_cannot_fake_status`, a mocked model claims the invoice total is 48,000 while citing the 52,400 line: the value is rejected, and the result is INSUFFICIENT_DATA, not a false MATCH.

### 7. Invoice-only keyword rules leaked into other document types

* **What went wrong:** For an email, the `From:` line became the "vendor". A policy got both `effective_date` and a duplicate `document_date`. Policies and delivery notes got almost no useful fields without the model.
* **Why:** Regex rules (`"from"` → vendor, `"total"` → amount, `"date"` → document date) were written around invoices and merged into the LLM's output. The unit tests pinned those rules to one invoice's exact wording (`ODS01262700015`, `73469.00`).
* **What we changed:** Deleted the keyword rules and their invoice-specific tests. Added `document_types.py`, which defines fields and required fields for all five types and is generated into the prompt. The evaluation set now covers GST, US and letter-style invoices, purchase orders, delivery notes, HR and customer emails, and leave, IT and expense policies.

### 8. Values in Hindi could never be verified

* **What went wrong:** A vendor name such as `शर्मा ट्रेडर्स` would always show as unverified. Worse, a first fix made *different* Hindi names match each other.
* **Why:** Word matching used `[a-z0-9]`, which ignores Devanagari. Python's `\w` splits Hindi words at vowel signs (`शर्मा` → `शर`, `म`), so unrelated names shared fragments.
* **What we changed:** Added a Unicode-aware tokenizer that keeps letters, digits and combining marks together (`grounding.words`, `test_non_latin_values_can_be_verified`). Evaluation case A08 is a Hindi-label invoice.

### 9. "Suppliers" verified the currency as INR

* **What went wrong:** A currency of INR counted as verified when its cited line was `RAJU SUPPLIERS`.
* **Why:** The check looked for the substring `"rs"` (for "Rs.") anywhere in the line.
* **What we changed:** It now requires `₹` or a standalone `Rs`/`Rs.` (`test_rupee_symbol_supports_inr_but_word_suppliers_does_not`).

### 10. Explanations can be wrong even when citations are right (open)

* **What went wrong:** For "Was everything on the PO delivered?", the explanation said the delivery note did not reference the PO. It does (`PO Number: PO-778`).
* **Why:** We verify citations and the figures in the answer, not free-text reasoning.
* **Mitigation and next step:** The UI shows the cited lines next to every answer so the user can check. A further step would be to check that every document or identifier the explanation names appears in the cited lines.

### 11. A citation proves location, not meaning (open)

* **What could go wrong:** If the model answered a "GST amount" question with the supplier's GSTIN and cited the GSTIN line, the citation would be valid and the answer labelled verified.
* **Why:** Verification checks *where* a value comes from, not whether it is the right kind of value.
* **Mitigation:** For extraction, amount fields must contain a number that appears on the cited line, and only verified fields reach the comparison. For free-text answers we rely on the reasoning model, and the evaluation includes adversarial questions (Q10, Q11 unanswerable; Q14 prompt injection) to measure this.
