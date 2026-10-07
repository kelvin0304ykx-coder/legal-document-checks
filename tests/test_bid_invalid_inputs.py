import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('bid_invalid', ROOT / 'scripts/lint_bid_package.py')
LINTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LINTER)


class BidInvalidInputTests(unittest.TestCase):
    def assert_invalid(self, requirements):
        report = LINTER.lint_texts({'response.md': '递交截止日期：2026-08-15'}, requirements)
        self.assertIn('requirements.invalid', {x['code'] for x in report['findings']})
        self.assertGreater(report['summary']['errors'], 0)

    def test_nonobject_requirements_fail_as_a_report(self):
        for value in ([], None, 'not an object', 1):
            with self.subTest(value=value):
                self.assert_invalid(value)

    def test_impossible_expected_date_is_not_accepted(self):
        self.assert_invalid({'dates': [{'label': '递交截止日期', 'expected': '2026-02-30'}]})

    def test_impossible_document_date_is_a_finding(self):
        report = LINTER.lint_texts(
            {'response.md': '递交截止日期：2026-02-30'},
            {'dates': [{'label': '递交截止日期', 'expected': '2026-08-15'}]},
        )
        self.assertIn('date_invalid', {x['code'] for x in report['findings']})

    def test_nonfinite_price_values_are_rejected(self):
        for value in ('NaN', 'Infinity', '-Infinity', 'sNaN'):
            for field in ('unit_price', 'quantity', 'total'):
                check = {'unit_price': '0.10', 'quantity': '3', 'total': '0.30'}
                check[field] = value
                with self.subTest(value=value, field=field):
                    self.assert_invalid({'price_checks': [check]})

    def test_extra_date_digits_cannot_match_a_valid_prefix(self):
        for bad in ('2026-02-280', '2026-002-28', '20260-02-28'):
            with self.subTest(expected=bad):
                self.assert_invalid({'dates': [{'label': '递交截止日期', 'expected': bad}]})
            with self.subTest(actual=bad):
                report = LINTER.lint_texts(
                    {'response.md': '递交截止日期：' + bad},
                    {'dates': [{'label': '递交截止日期', 'expected': '2026-02-28'}]},
                )
                self.assertGreater(report['summary']['errors'], 0)


if __name__ == '__main__':
    unittest.main()
