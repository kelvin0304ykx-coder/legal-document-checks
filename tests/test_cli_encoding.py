import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CliEncodingTests(unittest.TestCase):
    def test_non_utf8_input_is_rejected_explicitly(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            documents = root / 'documents'
            documents.mkdir()
            (documents/'sample.txt').write_bytes('[待填写]'.encode('gb18030'))
            requirements = root / 'requirements.json'
            requirements.write_text('{}', encoding='utf-8')
            result = subprocess.run(
                [sys.executable, '-B', str(ROOT/'scripts/lint_bid_package.py'),
                 '--documents', str(documents), '--requirements', str(requirements), '--json'],
                capture_output=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn('input.invalid', {x['code'] for x in json.loads(result.stdout)['findings']})

    def restricted_output_env(self):
        return dict(os.environ, PYTHONUTF8='0', PYTHONIOENCODING='cp1252')

    def test_bid_json_output_is_utf8_under_a_legacy_stdout_encoding(self):
        with tempfile.TemporaryDirectory() as td:
            req = Path(td)/'requirements.json'
            req.write_text(json.dumps({'mandatory_terms': ['缺少的测试词']}, ensure_ascii=False), encoding='utf-8')
            result = subprocess.run(
                [sys.executable, '-B', str(ROOT/'scripts/lint_bid_package.py'),
                 '--documents', str(ROOT/'examples/bid/documents'), '--requirements', str(req), '--json'],
                env=self.restricted_output_env(), capture_output=True,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(json.loads(result.stdout)['findings'][0]['requirement_id'], '缺少的测试词')

    def test_contract_json_output_is_utf8_under_a_legacy_stdout_encoding(self):
        result = subprocess.run(
            [sys.executable, '-B', str(ROOT/'scripts/validate_contract_ooxml.py'),
             str(ROOT/'examples/不存在.docx'), '--expected-author', '审查人', '--json'],
            env=self.restricted_output_env(), capture_output=True,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)['expected_author'], '审查人')


if __name__ == '__main__':
    unittest.main()
