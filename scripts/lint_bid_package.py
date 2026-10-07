#!/usr/bin/env python3
"""Deterministic mechanical linter for legal-service bid packages."""

from __future__ import annotations

import argparse
from datetime import date
import json
import re
import sys
import zipfile
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PLACEHOLDER_PATTERNS = (
    re.compile(r"\[(?:待填写|待补充|待确认|请填写)\]"),
    re.compile(r"\{\{[^{}\r\n]{1,100}\}\}"),
    re.compile(r"(?<![A-Za-z])X{3,}(?![A-Za-z])", re.IGNORECASE),
    re.compile(r"_{4,}"),
)
DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<year>20\d{2,})[年./-](?P<month>\d+)[月./-](?P<day>\d+)日?(?!\d)"
)
SUPPORTED = {".txt", ".md", ".docx"}


def _finding(
    code: str,
    message: str,
    *,
    severity: str = "error",
    document: str = "",
    line: int = 0,
    requirement_id: str = "",
    **extra: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "document": document,
        "line": line,
        "requirement_id": requirement_id,
        "message": message,
    }
    result.update(extra)
    return result


def _line_number(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def _normalize_date(value: str, *, exact: bool = False) -> str | None:
    match = DATE_PATTERN.fullmatch(value.strip()) if exact else DATE_PATTERN.search(value)
    if not match:
        return None
    if len(match.group('year')) != 4 or any(
        len(match.group(field)) > 2 for field in ('month', 'day')
    ):
        return None
    normalized = (
        f"{int(match.group('year')):04d}-"
        f"{int(match.group('month')):02d}-"
        f"{int(match.group('day')):02d}"
    )
    try:
        date.fromisoformat(normalized)
    except ValueError:
        return None
    return normalized


def lint_texts(
    documents: dict[str, str],
    requirements: dict[str, Any],
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    if not isinstance(requirements, dict):
        findings.append(_finding(
            "requirements.invalid", "Requirements must be a JSON object.",
            requirement_id="requirements",
        ))
        requirements = {}
    ordered_documents = {
        str(name): str(text) for name, text in sorted(documents.items())
    }
    combined = "\n".join(ordered_documents.values())

    for name, text in ordered_documents.items():
        for pattern in PLACEHOLDER_PATTERNS:
            for match in pattern.finditer(text):
                findings.append(
                    _finding(
                        "placeholder",
                        "Unresolved placeholder remains in the bid package.",
                        document=name,
                        line=_line_number(text, match.start()),
                    )
                )

    current_project = requirements.get("current_project_name")
    if current_project is not None and not isinstance(current_project, str):
        findings.append(
            _finding(
                "requirements.invalid",
                "current_project_name must be a string.",
                requirement_id="current_project_name",
            )
        )
    elif isinstance(current_project, str) and current_project:
        if current_project not in combined:
            findings.append(
                _finding(
                    "current_project_name_missing",
                    "Current project name does not appear in the package.",
                    requirement_id="current_project_name",
                )
            )

    legacy_names = requirements.get("legacy_project_names", [])
    if not isinstance(legacy_names, list):
        findings.append(
            _finding(
                "requirements.invalid",
                "legacy_project_names must be a list.",
                requirement_id="legacy_project_names",
            )
        )
    else:
        for legacy in legacy_names:
            if not isinstance(legacy, str):
                findings.append(
                    _finding(
                        "requirements.invalid",
                        "Legacy project names must be strings.",
                        requirement_id="legacy_project_names",
                    )
                )
                continue
            for name, text in ordered_documents.items():
                position = text.find(legacy)
                if legacy and position >= 0:
                    findings.append(
                        _finding(
                            "legacy_project_name",
                            "Legacy project name remains in the package.",
                            document=name,
                            line=_line_number(text, position),
                            requirement_id=legacy,
                        )
                    )

    mandatory_terms = requirements.get("mandatory_terms", [])
    if not isinstance(mandatory_terms, list):
        findings.append(
            _finding(
                "requirements.invalid",
                "mandatory_terms must be a list.",
                requirement_id="mandatory_terms",
            )
        )
    else:
        for term in mandatory_terms:
            if not isinstance(term, str) or not term:
                findings.append(
                    _finding(
                        "requirements.invalid",
                        "Mandatory terms must be non-blank strings.",
                        requirement_id="mandatory_terms",
                    )
                )
            elif term not in combined:
                findings.append(
                    _finding(
                        "mandatory_term_missing",
                        "A mandatory response term is absent.",
                        requirement_id=term,
                    )
                )

    scoring_items = requirements.get("scoring_items", [])
    if not isinstance(scoring_items, list):
        findings.append(
            _finding(
                "requirements.invalid",
                "scoring_items must be a list.",
                requirement_id="scoring_items",
            )
        )
    else:
        for index, item in enumerate(scoring_items):
            if not isinstance(item, dict):
                findings.append(
                    _finding(
                        "requirements.invalid",
                        "Each scoring item must be an object.",
                        requirement_id=f"scoring_items[{index}]",
                    )
                )
                continue
            requirement_id = str(item.get("id") or f"scoring_items[{index}]")
            terms = item.get("required_terms")
            if (
                not isinstance(terms, list)
                or not terms
                or not all(isinstance(term, str) and term for term in terms)
            ):
                findings.append(
                    _finding(
                        "requirements.invalid",
                        "Scoring required_terms must be a non-empty string list.",
                        requirement_id=requirement_id,
                    )
                )
                continue
            missing = [term for term in terms if term not in combined]
            if missing:
                findings.append(
                    _finding(
                        "scoring_coverage_missing",
                        "Scoring response is missing required coverage terms.",
                        requirement_id=requirement_id,
                        missing_terms=", ".join(missing),
                    )
                )

    date_rules = requirements.get("dates", [])
    if not isinstance(date_rules, list):
        findings.append(
            _finding(
                "requirements.invalid",
                "dates must be a list.",
                requirement_id="dates",
            )
        )
    else:
        for index, rule in enumerate(date_rules):
            if not isinstance(rule, dict):
                findings.append(
                    _finding(
                        "requirements.invalid",
                        "Each date rule must be an object.",
                        requirement_id=f"dates[{index}]",
                    )
                )
                continue
            requirement_id = str(rule.get("id") or f"dates[{index}]")
            label = rule.get("label")
            expected = _normalize_date(str(rule.get("expected", "")), exact=True)
            if not isinstance(label, str) or not label or expected is None:
                findings.append(
                    _finding(
                        "requirements.invalid",
                        "Date rule requires a label and valid expected date.",
                        requirement_id=requirement_id,
                    )
                )
                continue
            for name, text in ordered_documents.items():
                for label_match in re.finditer(re.escape(label), text):
                    window = text[label_match.end() : label_match.end() + 80]
                    actual = _normalize_date(window)
                    raw_date = DATE_PATTERN.search(window)
                    if raw_date is not None and actual is None:
                        findings.append(_finding(
                            "date_invalid", "Date near the required label is not a real calendar date.",
                            document=name, line=_line_number(text, label_match.start()),
                            requirement_id=requirement_id, expected=expected,
                            actual=raw_date.group(0),
                        ))
                    elif actual and actual != expected:
                        findings.append(
                            _finding(
                                "date_conflict",
                                "Date near the required label conflicts with requirements.",
                                document=name,
                                line=_line_number(text, label_match.start()),
                                requirement_id=requirement_id,
                                expected=expected,
                                actual=actual,
                            )
                        )

    price_checks = requirements.get("price_checks", [])
    if not isinstance(price_checks, list):
        findings.append(
            _finding(
                "requirements.invalid",
                "price_checks must be a list.",
                requirement_id="price_checks",
            )
        )
    else:
        for index, check in enumerate(price_checks):
            if not isinstance(check, dict):
                findings.append(
                    _finding(
                        "requirements.invalid",
                        "Each price check must be an object.",
                        requirement_id=f"price_checks[{index}]",
                    )
                )
                continue
            requirement_id = str(check.get("id") or f"price_checks[{index}]")
            try:
                unit_price = Decimal(str(check["unit_price"]))
                quantity = Decimal(str(check["quantity"]))
                total = Decimal(str(check["total"]))
                if not all(value.is_finite() for value in (unit_price, quantity, total)):
                    raise ValueError("Price values must be finite")
                expected_total = unit_price * quantity
                if not expected_total.is_finite():
                    raise ValueError("Price product must be finite")
            except (KeyError, DecimalException, ValueError):
                findings.append(
                    _finding(
                        "requirements.invalid",
                        "Price check requires finite Decimal values and a representable product.",
                        requirement_id=requirement_id,
                    )
                )
                continue
            if total != expected_total:
                findings.append(
                    _finding(
                        "price_arithmetic",
                        "Price total does not equal unit price multiplied by quantity.",
                        requirement_id=requirement_id,
                        expected=format(expected_total, "f"),
                        actual=format(total, "f"),
                    )
                )

    findings.sort(
        key=lambda item: (
            item["document"],
            item["line"],
            item["requirement_id"],
            item["code"],
        )
    )
    return {
        "schema_version": 1,
        "summary": {
            "documents": len(ordered_documents),
            "errors": sum(item["severity"] == "error" for item in findings),
            "warnings": sum(item["severity"] == "warning" for item in findings),
            "total": len(findings),
        },
        "findings": findings,
    }


def _read_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    paragraphs = []
    for paragraph in root.iter(f"{{{W}}}p"):
        paragraphs.append(
            "".join(node.text or "" for node in paragraph.iter(f"{{{W}}}t"))
        )
    return "\n".join(paragraphs)


def read_documents(root: Path) -> dict[str, str]:
    directory = Path(root).resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("documents path must be a directory")
    documents: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.name.startswith("~$")
            or path.suffix.lower() not in SUPPORTED
        ):
            continue
        relative = path.relative_to(directory).as_posix()
        if path.suffix.lower() == ".docx":
            documents[relative] = _read_docx(path)
        else:
            documents[relative] = path.read_text(
                encoding="utf-8-sig"
            )
    return documents


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    refused = False
    try:
        requirements = json.loads(args.requirements.read_text(encoding="utf-8-sig"))
        report = lint_texts(read_documents(args.documents), requirements)
    except (OSError, UnicodeError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError, RuntimeError) as exc:
        refused = True
        report = {
            "schema_version": 1,
            "summary": {"documents": 0, "errors": 1, "warnings": 0, "total": 1},
            "findings": [_finding("input.invalid", f"Input could not be read: {exc}")],
        }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for finding in report["findings"]:
            print(
                f"{finding['severity']} {finding['document']}:{finding['line']} "
                f"{finding['code']} {finding['message']}"
            )
        print(json.dumps(report["summary"], ensure_ascii=False))
    return 2 if refused else (1 if report["summary"]["errors"] else 0)


if __name__ == "__main__":
    raise SystemExit(main())
