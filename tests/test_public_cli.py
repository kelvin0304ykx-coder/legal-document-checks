import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PublicCliTests(unittest.TestCase):
    def test_synthetic_contract_demo_checks_bound_identity(self):
        demo = ROOT / 'examples/contract_demo.py'
        self.assertTrue(demo.is_file(), 'Runnable contract example is missing')
        result = subprocess.run([sys.executable, '-B', str(demo)],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['summary']['errors'], 0)
        self.assertEqual(report['identity_gate'], 'pass')

    def bid(self, requirements):
        script = ROOT / 'scripts/lint_bid_package.py'
        self.assertTrue(script.is_file(), 'Public bid entry point is missing')
        return subprocess.run(
            [sys.executable, '-B', str(script), '--documents',
             str(ROOT / 'examples/bid/documents'), '--requirements',
             str(ROOT / 'examples/bid' / requirements), '--json'],
            capture_output=True, text=True, encoding='utf-8',
        )

    def test_clean_bid_cli_is_read_only(self):
        sample = ROOT / 'examples/bid/documents/response.md'
        self.assertTrue(sample.is_file(), 'Synthetic bid example is missing')
        before = hashlib.sha256(sample.read_bytes()).hexdigest()
        result = self.bid('requirements.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['summary']['errors'], 0)
        self.assertEqual(hashlib.sha256(sample.read_bytes()).hexdigest(), before)

    def test_mismatched_requirements_return_findings_and_failure(self):
        result = self.bid('requirements-mismatch.json')
        self.assertEqual(result.returncode, 1, result.stderr)
        findings = {item['code'] for item in json.loads(result.stdout)['findings']}
        self.assertIn('date_conflict', findings)
        self.assertIn('price_arithmetic', findings)


if __name__ == '__main__':
    unittest.main()
