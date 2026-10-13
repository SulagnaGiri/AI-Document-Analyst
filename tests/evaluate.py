"""Run the transparent baseline against the checked-in evaluation set."""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from document_analyst import analyze_document, compare_documents


ROOT = Path(__file__).resolve().parents[1]
DATA = json.loads((Path(__file__).parent / "evaluation_cases.json").read_text())


def evaluate() -> dict[str, float | int]:
    type_correct = 0
    expected_type_count = 0
    field_correct = 0
    expected_field_count = 0
    grounded_fields = 0
    extracted_fields = 0
    structured_outputs = 0
    for case in DATA["cases"]:
        analysis = analyze_document(case["name"], case["text"])
        expected_type_count += 1
        type_correct += analysis["document_type"] == case["expected_type"]
        expected_fields = case["expected_fields"]
        expected_field_count += len(expected_fields)
        field_correct += sum(
            analysis["fields"].get(key, {}).get("value") == value
            for key, value in expected_fields.items()
        )
        extracted_fields += len(analysis["fields"])
        grounded_fields += sum(
            field["evidence"] in case["text"]
            for field in analysis["fields"].values()
        )
        structured_outputs += all(
            key in analysis for key in ("name", "document_type", "fields", "summary", "missing_questions")
        )

    comparison_correct = 0
    for case in DATA["comparisons"]:
        analyses = [
            analyze_document("invoice", case["invoice"]),
            analyze_document("purchase order", case["purchase_order"]),
        ]
        comparison_correct += compare_documents(analyses)["status"] == case["expected_status"]

    return {
        "examples": len(DATA["cases"]),
        "classification_accuracy_pct": round(100 * type_correct / expected_type_count, 1),
        "expected_field_exact_match_pct": round(100 * field_correct / expected_field_count, 1),
        "grounded_field_evidence_pct": round(100 * grounded_fields / extracted_fields, 1),
        "structured_output_validity_pct": round(100 * structured_outputs / expected_type_count, 1),
        "comparison_accuracy_pct": round(100 * comparison_correct / len(DATA["comparisons"]), 1),
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(), indent=2))