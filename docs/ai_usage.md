# AI usage

I used AI coding assistants throughout this project. This page records what they did, and which decisions I made.

> **TODO:** add any other tools or sessions you used (e.g. ChatGPT, Gemini or Cursor for the first version of the app, brainstorming, the earlier keyword-rule baseline).

## Claude Code (Anthropic), in VS Code

Used for a debugging and redesign session after the first version was working.

| Activity | What the AI did | What I decided or checked |
|---|---|---|
| Debugging | Reproduced "Total GST returns raw table text", traced it to the retired Llama model (HTTP 410), and listed the Nemotron models my key could reach | I reported the symptom and asked whether to switch to Nemotron |
| Model choice | Tested candidate models on the real invoice for availability, latency and output format | Chose Nemotron 3 Super |
| Design review | Pointed out that the keyword rules and their tests were invoice-specific | **I pushed back twice:** the pipeline was "very fragile" and "focused only on invoices", and the system must handle POs, emails, leave and office policies, and invoices in many formats. This led to the V3 redesign. |
| Redesign | Proposed and implemented line-number citations, answer labels, removing the keyword fallback, and type definitions as configuration | I approved the redesign and asked for everything unnecessary to be removed |
| Code | Wrote `grounding.py`, `checks.py`, `document_types.py`, `llm_client.py` and `pipeline.py`, and refactored `app.py` | Reviewed in the IDE during the session |
| Tests | Wrote unit tests with the LLM mocked, plus the 45-case evaluation set and runner | Must be able to explain each case and metric |
| UI | Rebuilt the React frontend (multi-document, comparison card, verification labels) | Requested a better-looking UI |
| Documentation | Drafted these docs from what happened in the session | **TODO:** review, correct and add your own reasoning before submitting |

## What I learned about using AI here

* The first fixes the assistant made were **patches for one failing example at a time**. They passed the test in front of them and failed on the next document. It took a human asking "is this design right?" to switch to a structural fix. When using AI, check whether each fix generalises.
* The AI was most useful for **fast experiments against the live model** (which models exist, how JSON mode behaves, what pypdf layout mode produces) and for writing tests that pin behaviour down.
* Everything the model produces in the app is checked by code. I applied the same principle to the AI assistant: run the tests and the evaluation, and read the diffs.
