"""Evaluate the live LLM pipeline against evaluation/cases.json.

    python evaluation/run_eval.py               # everything, each analysis case run twice
    python evaluation/run_eval.py --repeat 1    # faster, skips the consistency metric

Writes evaluation/results.json (raw) and docs/evaluation.md (report). Needs NVIDIA_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import llm_client  # noqa: E402
import pipeline  # noqa: E402
from document_types import FIELD_QUESTIONS  # noqa: E402
from grounding import numbers_in, to_decimal, words  # noqa: E402

CASES = json.loads((ROOT / "evaluation" / "cases.json").read_text())


class JsonFailureCounter(logging.Handler):
    """Counts replies the model sent as invalid JSON (each one triggers a retry)."""

    def __init__(self):
        super().__init__()
        self.count = 0

    def emit(self, record):
        if "invalid JSON" in record.getMessage():
            self.count += 1


def value_matches(expected, actual: str) -> bool:
    for option in expected if isinstance(expected, list) else [expected]:
        option = str(option)
        if to_decimal(option) is not None:
            if to_decimal(option) in numbers_in(actual):
                return True
            continue
        a, b = " ".join(words(option)), " ".join(words(actual))
        if a and b and (a in b or b in a):
            return True
    return False


def timed(fn, *args):
    start = time.time()
    try:
        return fn(*args), round(time.time() - start, 1), None
    except pipeline.LLMUnavailable as exc:
        return None, round(time.time() - start, 1), str(exc)


# ------------------------------------------------------------------ analysis

def run_analysis_case(case: dict, run: int) -> dict:
    result, seconds, error = timed(pipeline.analyze, f"{case['id']}-run{run}.txt", case["text"])
    if error:
        return {"id": case["id"], "run": run, "error": error, "seconds": seconds}
    expected_types = case["expected_type"] if isinstance(case["expected_type"], list) else [case["expected_type"]]
    fields = result["fields"]
    field_results = {}
    for name, expected in case["expected_fields"].items():
        actual = fields.get(name)
        field_results[name] = {
            "expected": expected,
            "actual": actual["value"] if actual else None,
            "correct": bool(actual) and value_matches(expected, actual["value"]),
            "verified": bool(actual and actual["verified"]),
        }
    missing_results = {
        name: FIELD_QUESTIONS.get(name, f"What is the {name.replace('_', ' ')}?") in result["missing_questions"]
        for name in case.get("expected_missing", [])
    }
    return {
        "id": case["id"],
        "run": run,
        "seconds": seconds,
        "type": result["document_type"],
        "type_correct": result["document_type"] in expected_types,
        "fields": field_results,
        "extracted": len(fields),
        "extracted_verified": sum(1 for item in fields.values() if item["verified"]),
        "missing_flagged": missing_results,
        "inconsistencies": [item["issue"] for item in result["inconsistencies"]],
        "summary": result["summary"],
    }


# ------------------------------------------------------------------ comparison & QA

def documents_for(refs: list) -> list[dict]:
    analysis = {case["id"]: case for case in CASES["analysis"]}
    comparison = {case["id"]: case for case in CASES["comparison"]}
    documents = []
    for ref in refs:
        if isinstance(ref, dict):
            documents.append(ref)
        elif ref in analysis:
            documents.append({"name": f"{ref}.txt", "text": analysis[ref]["text"]})
        else:
            documents.extend(comparison[ref]["documents"])
    return documents


def run_comparison_case(case: dict) -> dict:
    result, seconds, error = timed(pipeline.compare, case["documents"])
    if error:
        return {"id": case["id"], "error": error, "seconds": seconds}
    return {
        "id": case["id"],
        "seconds": seconds,
        "status": result["status"],
        "correct": result["status"] == case["expected_status"],
        "observations": result["observations"],
    }


def run_qa_case(case: dict) -> dict:
    result, seconds, error = timed(pipeline.answer, case["question"], documents_for(case["documents"]))
    if error:
        return {"id": case["id"], "error": error, "seconds": seconds}
    text = result["answer"].lower()
    checks = {}
    if "expect_numbers" in case:
        found = numbers_in(result["answer"])
        checks["numbers"] = all(to_decimal(number) in found for number in case["expect_numbers"])
    if "expect_all" in case:
        checks["all"] = all(item in text for item in case["expect_all"])
    if "expect_any" in case:
        checks["any"] = any(item in text for item in case["expect_any"])
    if "expect_none" in case:
        checks["none"] = not any(item in text for item in case["expect_none"])
    if "expect_verification" in case:
        checks["verification"] = result.get("verification") == case["expect_verification"]
    if "expect_source" in case:
        checks["source"] = result.get("answer_source") == case["expect_source"]
    if "expect_status" in case:
        checks["status"] = (result.get("comparison") or {}).get("status") == case["expect_status"]
    return {
        "id": case["id"],
        "seconds": seconds,
        "answer": result["answer"],
        "verification": result.get("verification"),
        "answer_source": result.get("answer_source"),
        "has_valid_citation": bool(result.get("citations")),
        "checks": checks,
        "correct": all(checks.values()),
    }


# ------------------------------------------------------------------ metrics

def pct(numerator: int, denominator: int) -> float | None:
    return round(100 * numerator / denominator, 1) if denominator else None


def summarize(analysis_runs: list[dict], comparisons: list[dict], qa: list[dict], json_failures: int, calls: int) -> dict:
    cases = {case["id"]: case for case in CASES["analysis"]}
    ok = [run for run in analysis_runs if "error" not in run]
    first = [run for run in ok if run["run"] == 1]
    field_items = [field for run in first for field in run["fields"].values()]
    clean = [run for run in first if cases[run["id"]].get("expect_inconsistency") is False]
    dirty = [run for run in first if cases[run["id"]].get("expect_inconsistency") is True]
    missing_items = [flag for run in first for flag in run["missing_flagged"].values()]
    no_field_cases = [run for run in first if cases[run["id"]].get("expect_no_fields")]

    by_id: dict[str, list[dict]] = {}
    for run in ok:
        by_id.setdefault(run["id"], []).append(run)
    repeated = [runs for runs in by_id.values() if len(runs) > 1]
    type_agree = sum(len({run["type"] for run in runs}) == 1 for runs in repeated)
    field_pairs = [
        (runs[0]["fields"][name]["actual"], runs[1]["fields"][name]["actual"])
        for runs in repeated for name in runs[0]["fields"]
    ]
    field_agree = sum(" ".join(words(str(a))) == " ".join(words(str(b))) for a, b in field_pairs)

    answered = [item for item in qa if "error" not in item]
    factual = [item for item in answered if item.get("verification") not in ("not_in_documents",) and item.get("answer_source") == "nvidia"]

    return {
        "classification_accuracy": pct(sum(run["type_correct"] for run in first), len(first)),
        "field_extraction_accuracy": pct(sum(field["correct"] for field in field_items), len(field_items)),
        "field_correct_and_verified": pct(sum(field["correct"] and field["verified"] for field in field_items), len(field_items)),
        "grounded_field_rate": pct(sum(run["extracted_verified"] for run in first), sum(run["extracted"] for run in first)),
        "missing_info_recall": pct(sum(missing_items), len(missing_items)),
        "inconsistency_recall": pct(sum(bool(run["inconsistencies"]) for run in dirty), len(dirty)),
        "inconsistency_false_positive_rate": pct(sum(bool(run["inconsistencies"]) for run in clean), len(clean)),
        "no_fields_on_garbage": pct(sum(run["extracted"] == 0 for run in no_field_cases), len(no_field_cases)),
        "comparison_status_accuracy": pct(sum(item.get("correct", False) for item in comparisons), len(comparisons)),
        "qa_accuracy": pct(sum(item.get("correct", False) for item in qa), len(qa)),
        "qa_answers_with_valid_citation": pct(sum(item["has_valid_citation"] for item in factual), len(factual)),
        "type_consistency_across_runs": pct(type_agree, len(repeated)),
        "field_consistency_across_runs": pct(field_agree, len(field_pairs)),
        "valid_structured_output": pct(calls - json_failures, calls),
        "llm_unavailable_errors": sum("error" in item for item in analysis_runs + comparisons + qa),
        "avg_seconds_analysis": round(sum(run["seconds"] for run in first) / max(len(first), 1), 1),
        "avg_seconds_qa": round(sum(item["seconds"] for item in answered) / max(len(answered), 1), 1),
    }


# ------------------------------------------------------------------ report

METRIC_LABELS = [
    ("classification_accuracy", "Classification accuracy", "Predicted type is (one of) the expected type(s)."),
    ("field_extraction_accuracy", "Field extraction accuracy", "Expected field present with the right value."),
    ("field_correct_and_verified", "Correct and verified", "Right value *and* found on the line the model cited."),
    ("grounded_field_rate", "Grounded field rate", "Share of all extracted fields that verification confirmed (the rest are shown as unverified)."),
    ("missing_info_recall", "Missing-information recall", "Missing required fields that produced a follow-up question."),
    ("inconsistency_recall", "Inconsistency recall", "Documents with a seeded inconsistency that were flagged."),
    ("inconsistency_false_positive_rate", "Inconsistency false positives", "Clean documents wrongly flagged (lower is better)."),
    ("no_fields_on_garbage", "No fields invented on garbage", "Garbage input produced zero fields."),
    ("comparison_status_accuracy", "Comparison status accuracy", "MATCH / MISMATCH / INSUFFICIENT_DATA as expected."),
    ("qa_accuracy", "Q&A accuracy", "Answer contains the expected facts / refuses when it should / resists injection."),
    ("qa_answers_with_valid_citation", "Answers with a valid citation", "Factual answers citing at least one real document line."),
    ("type_consistency_across_runs", "Type consistency", "Same type on two independent runs."),
    ("field_consistency_across_runs", "Field consistency", "Same field value on two independent runs."),
    ("valid_structured_output", "Valid structured output", "Model replies that parsed as JSON on the first try."),
]


def fmt(value) -> str:
    return "n/a" if value is None else f"{value}%"


def tick(value: bool) -> str:
    return "✅" if value else "❌"


def write_report(metrics: dict, analysis_runs: list[dict], comparisons: list[dict], qa: list[dict], repeat: int) -> str:
    analysis_cases = {case["id"]: case for case in CASES["analysis"]}
    lines = [
        "# Evaluation results",
        "",
        f"Generated by `python evaluation/run_eval.py --repeat {repeat}` on {date.today().isoformat()} "
        f"with model `{llm_client.NVIDIA_MODEL}`. Test cases: `evaluation/cases.json` "
        f"({len(CASES['analysis'])} analysis, {len(CASES['comparison'])} comparison, {len(CASES['qa'])} Q&A = "
        f"{len(CASES['analysis']) + len(CASES['comparison']) + len(CASES['qa'])} cases). Raw output: `evaluation/results.json`.",
        "",
        "LLM output varies between runs, so numbers can move by a few points when re-run.",
        "",
        "## Summary",
        "",
        "| Metric | Result | What it measures |",
        "|---|---|---|",
    ]
    lines += [f"| {label} | {fmt(metrics[key])} | {meaning} |" for key, label, meaning in METRIC_LABELS]
    lines += [
        "",
        f"Average latency: {metrics['avg_seconds_analysis']}s per document analysis, {metrics['avg_seconds_qa']}s per question. "
        f"LLM-unavailable errors during the run: {metrics['llm_unavailable_errors']}.",
        "",
        "## Analysis cases",
        "",
        "| ID | Category | Case | Type | Fields correct (verified) | Missing flagged | Inconsistencies |",
        "|---|---|---|---|---|---|---|",
    ]
    for run in (run for run in analysis_runs if run["run"] == 1):
        case = analysis_cases[run["id"]]
        if "error" in run:
            lines.append(f"| {run['id']} | {case['category']} | {case['title']} | error | | | |")
            continue
        fields = run["fields"]
        field_text = f"{sum(f['correct'] for f in fields.values())}/{len(fields)} ({sum(f['verified'] for f in fields.values())} verified)" if fields else "-"
        wrong = [f"{name}={f['actual']!r}" for name, f in fields.items() if not f["correct"]]
        if wrong:
            field_text += "<br>wrong/missing: " + ", ".join(wrong)
        missing = ", ".join(f"{tick(v)} {k}" for k, v in run["missing_flagged"].items()) or "-"
        expected_inc = case.get("expect_inconsistency")
        inconsistency = ("; ".join(run["inconsistencies"]) or "none")[:160]
        if expected_inc is not None:
            inconsistency = f"{tick(bool(run['inconsistencies']) == expected_inc)} {inconsistency}"
        lines.append(f"| {run['id']} | {case['category']} | {case['title']} | {tick(run['type_correct'])} {run['type']} | {field_text} | {missing} | {inconsistency} |")

    comparison_cases = {case["id"]: case for case in CASES["comparison"]}
    lines += ["", "## Comparison cases", "", "| ID | Category | Case | Expected | Got |", "|---|---|---|---|---|"]
    for item in comparisons:
        case = comparison_cases[item["id"]]
        got = item.get("error") or f"{tick(item['correct'])} {item['status']}"
        lines.append(f"| {item['id']} | {case['category']} | {case['title']} | {case['expected_status']} | {got} |")

    qa_cases = {case["id"]: case for case in CASES["qa"]}
    lines += ["", "## Question-answering cases", "", "| ID | Category | Question | Result | Label | Answer |", "|---|---|---|---|---|---|"]
    for item in qa:
        case = qa_cases[item["id"]]
        if "error" in item:
            lines.append(f"| {item['id']} | {case['category']} | {case['question']} | error | | {item['error']} |")
            continue
        answer = item["answer"].replace("\n", " ").replace("|", "/")
        lines.append(
            f"| {item['id']} | {case['category']} | {case['question']} | {tick(item['correct'])} | "
            f"{item['verification']} | {answer[:220]}{'…' if len(answer) > 220 else ''} |"
        )
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repeat", type=int, default=2, help="runs per analysis case (2+ measures consistency)")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if not llm_client.configured():
        sys.exit("NVIDIA_API_KEY is not set; the evaluation runs against the live model.")

    counter = JsonFailureCounter()
    logging.getLogger("llm_client").addHandler(counter)
    calls_before = 0
    original_call = llm_client.call_llm

    def counting_call(*call_args, **kwargs):
        nonlocal calls_before
        calls_before += 1
        return original_call(*call_args, **kwargs)

    llm_client.call_llm = counting_call

    jobs = [(case, run) for run in range(1, args.repeat + 1) for case in CASES["analysis"]]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        print(f"Analysis: {len(jobs)} runs…", flush=True)
        analysis_runs = list(pool.map(lambda job: run_analysis_case(*job), jobs))
        print(f"Comparison: {len(CASES['comparison'])} cases…", flush=True)
        comparisons = list(pool.map(run_comparison_case, CASES["comparison"]))
        print(f"Q&A: {len(CASES['qa'])} cases…", flush=True)
        qa = list(pool.map(run_qa_case, CASES["qa"]))

    metrics = summarize(analysis_runs, comparisons, qa, counter.count, calls_before)
    (ROOT / "evaluation" / "results.json").write_text(json.dumps(
        {"model": llm_client.NVIDIA_MODEL, "metrics": metrics, "analysis": analysis_runs, "comparison": comparisons, "qa": qa},
        indent=2, ensure_ascii=False,
    ))
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "evaluation.md").write_text(write_report(metrics, analysis_runs, comparisons, qa, args.repeat))
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
