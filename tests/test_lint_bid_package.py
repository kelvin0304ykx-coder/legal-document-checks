from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "lint_bid_package.py"
SPEC = importlib.util.spec_from_file_location("lint_bid_package", SCRIPT)
assert SPEC and SPEC.loader
linter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(linter)


class BidPackageLinterTest(unittest.TestCase):
    def test_clean_package_passes(self) -> None:
        documents = {
            "response.txt": (
                "星河法律顾问项目\n法定代表人签字并加盖公章。\n"
                "团队方案：负责人、成员分工与响应机制。\n"
                "递交截止日期：2026-08-15\n"
            )
        }
        requirements = self._requirements()

        report = linter.lint_texts(documents, requirements)

        self.assertEqual(0, report["summary"]["errors"])
        self.assertEqual([], report["findings"])

    def test_placeholders_are_errors_with_document_location(self) -> None:
        documents = {"response.txt": "星河项目 [待填写] {{quote}} XXXX"}

        report = linter.lint_texts(documents, {})
        placeholders = [
            item for item in report["findings"] if item["code"] == "placeholder"
        ]

        self.assertGreaterEqual(len(placeholders), 3)
        self.assertTrue(all(item["document"] == "response.txt" for item in placeholders))

    def test_legacy_project_names_are_rejected(self) -> None:
        requirements = {
            "current_project_name": "星河法律顾问项目",
            "legacy_project_names": ["旧城改造项目"],
        }
        documents = {"response.txt": "旧城改造项目响应文件"}

        report = linter.lint_texts(documents, requirements)

        self.assertIn("legacy_project_name", self._codes(report))
        self.assertIn("current_project_name_missing", self._codes(report))

    def test_mandatory_terms_and_scoring_coverage_are_checked(self) -> None:
        requirements = {
            "mandatory_terms": ["法定代表人签字", "加盖公章"],
            "scoring_items": [
                {
                    "id": "team",
                    "required_terms": ["负责人", "成员分工", "响应机制"],
                }
            ],
        }
        documents = {"response.txt": "法定代表人签字。团队仅列负责人。"}

        report = linter.lint_texts(documents, requirements)
        codes = self._codes(report)

        self.assertIn("mandatory_term_missing", codes)
        self.assertIn("scoring_coverage_missing", codes)
        scoring = [
            item
            for item in report["findings"]
            if item["code"] == "scoring_coverage_missing"
        ][0]
        self.assertEqual("team", scoring["requirement_id"])

    def test_date_conflicts_are_reported(self) -> None:
        requirements = {
            "dates": [
                {
                    "id": "deadline",
                    "label": "递交截止日期",
                    "expected": "2026-08-15",
                }
            ]
        }
        documents = {"response.txt": "递交截止日期：2026-08-16"}

        report = linter.lint_texts(documents, requirements)

        self.assertIn("date_conflict", self._codes(report))

    def test_price_arithmetic_uses_decimal(self) -> None:
        requirements = {
            "price_checks": [
                {
                    "id": "service",
                    "unit_price": "0.10",
                    "quantity": "3",
                    "total": "0.31",
                },
                {
                    "id": "exact",
                    "unit_price": "0.10",
                    "quantity": "3",
                    "total": "0.30",
                },
            ]
        }

        report = linter.lint_texts({}, requirements)
        arithmetic = [
            item for item in report["findings"] if item["code"] == "price_arithmetic"
        ]

        self.assertEqual(1, len(arithmetic))
        self.assertEqual("service", arithmetic[0]["requirement_id"])
        self.assertEqual("0.30", arithmetic[0]["expected"])

    def test_invalid_requirements_schema_is_reported_not_crashed(self) -> None:
        report = linter.lint_texts(
            {"response.txt": "safe"},
            {"mandatory_terms": "not-a-list", "price_checks": [{"unit_price": "x"}]},
        )
        self.assertIn("requirements.invalid", self._codes(report))

    def _codes(self, report: dict) -> set[str]:
        return {item["code"] for item in report["findings"]}

    def _requirements(self) -> dict:
        return {
            "current_project_name": "星河法律顾问项目",
            "legacy_project_names": ["旧城改造项目"],
            "mandatory_terms": ["法定代表人签字", "加盖公章"],
            "scoring_items": [
                {
                    "id": "team",
                    "required_terms": ["负责人", "成员分工", "响应机制"],
                }
            ],
            "dates": [
                {
                    "id": "deadline",
                    "label": "递交截止日期",
                    "expected": "2026-08-15",
                }
            ],
            "price_checks": [
                {
                    "id": "service",
                    "unit_price": "0.10",
                    "quantity": "3",
                    "total": "0.30",
                }
            ],
        }


if __name__ == "__main__":
    unittest.main()
