"""Evidence handling: line-numbered context for the LLM, and verification of what it cites.

Instead of asking the model to copy quotes (which it paraphrases, merges or mis-escapes),
every document line gets an id like L12 and the model cites line numbers. Checking a
citation is then exact: the line exists, and the cited value really appears on it.
Answers are never thrown away for imperfect evidence; they get a verification label.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

NUMBER = re.compile(r"(?<![A-Za-z0-9])\d[\d,]*(?:\.\d+)?(?![A-Za-z0-9])")


def words(text: str) -> list[str]:
    """Lower-cased words in any script. Letters, digits and combining marks stay together, so
    Hindi/Bengali words are not split at their vowel signs (which plain \\w does)."""
    tokens, current = [], []
    for char in text.lower():
        if unicodedata.category(char)[0] in "LNM":
            current.append(char)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def numbered_lines(text: str) -> list[str]:
    """Non-blank lines of a document; line N (1-based) is index N-1."""
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def document_ids(documents: list[dict[str, str]]) -> list[str]:
    return [f"D{index}" for index in range(1, len(documents) + 1)]


def format_context(documents: list[dict[str, str]]) -> str:
    blocks = []
    for doc_id, document in zip(document_ids(documents), documents):
        lines = numbered_lines(document.get("text", ""))
        body = "\n".join(f"L{number}: {line}" for number, line in enumerate(lines, 1))
        blocks.append(f'<document id="{doc_id}" name="{document.get("name", "document.txt")}">\n{body}\n</document>')
    return "\n\n".join(blocks)


def _line_numbers(raw: Any) -> list[int]:
    """Accept 12, "L12", "12-14", "L12-L14" or lists of those."""
    items = raw if isinstance(raw, list) else [raw]
    numbers = []
    for item in items:
        for start, end in re.findall(r"L?(\d+)(?:\s*-\s*L?(\d+))?", str(item)):
            first, last = int(start), int(end or start)
            if last >= first:
                numbers.extend(range(first, min(last, first + 50) + 1))
    return list(dict.fromkeys(numbers))


def _find_document(reference: Any, documents: list[dict[str, str]]) -> int | None:
    ids = document_ids(documents)
    reference = str(reference or "").strip()
    if reference in ids:
        return ids.index(reference)
    for index, document in enumerate(documents):
        if reference and reference == document.get("name"):
            return index
    return 0 if len(documents) == 1 else None


def resolve_citations(raw_citations: Any, documents: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Turn model citations ({document, lines}) into real line text. Invalid lines are dropped."""
    resolved = []
    for citation in raw_citations if isinstance(raw_citations, list) else []:
        if not isinstance(citation, dict):
            continue
        index = _find_document(citation.get("document"), documents)
        if index is None:
            continue
        lines = numbered_lines(documents[index].get("text", ""))
        valid = [number for number in _line_numbers(citation.get("lines")) if 1 <= number <= len(lines)]
        if valid:
            resolved.append({
                "document": documents[index].get("name", "document.txt"),
                "lines": valid,
                "quote": "\n".join(lines[number - 1] for number in valid),
            })
    return resolved


def to_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def numbers_in(text: str) -> set[Decimal]:
    return {number for raw in NUMBER.findall(text) if (number := to_decimal(raw)) is not None}


def value_supported(value: str, evidence: str) -> bool:
    """Every figure in the value is on the cited lines, and most of its words are too."""
    value_numbers = numbers_in(value)
    if value_numbers and not value_numbers <= numbers_in(evidence):
        return False
    value_words = [word for word in words(value) if not word.isdigit()]
    if not value_words:
        return bool(value_numbers)
    evidence_words = set(words(evidence))
    return sum(word in evidence_words for word in value_words) / len(value_words) >= 0.6


def grade_answer(answer: str, citations: list[dict[str, Any]], documents: list[dict[str, str]], question: str) -> dict[str, str]:
    """Label how well an answer is backed by the documents instead of accepting/rejecting it."""
    if re.search(r"cannot determine|could not find|not (?:stated|mentioned|provided) in the documents", answer, re.I):
        return {"verification": "not_in_documents", "verification_note": "The documents do not contain this information."}
    if not citations:
        return {"verification": "unverified", "verification_note": "The answer did not cite any valid document lines."}
    known = numbers_in(question)
    for document in documents:
        known |= numbers_in(document.get("text", ""))
    unsupported = sorted({raw for raw in NUMBER.findall(answer) if to_decimal(raw) not in known})
    if unsupported:
        return {
            "verification": "partially_verified",
            "verification_note": "Not printed in the documents (likely calculated): " + ", ".join(unsupported),
        }
    return {"verification": "verified", "verification_note": "Cited lines exist and every figure appears in the documents."}
