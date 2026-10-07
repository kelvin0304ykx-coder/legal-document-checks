"""Run matter-bound checks on temporary, synthetic OOXML fixtures."""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
import tempfile
import zipfile
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/validate_contract_ooxml.py'
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def run_demo():
    spec = importlib.util.spec_from_file_location('contract_checks', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lines = ['示例维护协议', '合同编号：SYN-001',
             '甲方：示例委托单位', '乙方：示例服务单位', '服务期限：三个月。']
    body = ''.join('<w:p><w:r><w:t>' + escape(line) +
                   '</w:t></w:r></w:p>' for line in lines)
    xml = f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    with tempfile.TemporaryDirectory(prefix='contract-check-demo-') as td:
        source = Path(td) / 'original.docx'
        output = Path(td) / 'reviewed.docx'
        for target in (source, output):
            with zipfile.ZipFile(target, 'x') as archive:
                archive.writestr('word/document.xml', xml)
        identity = {
            'contract_number': 'SYN-001', 'title': '示例维护协议',
            'parties': ['示例委托单位', '示例服务单位'],
            'reviewing_party': '示例委托单位',
            'source_path': str(source),
            'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'output_path': str(output),
        }
        return module.inspect_docx(output, baseline=source, identity=identity,
                                   require_identity=True, expected_author='Reviewer')


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    report = run_demo()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(1 if report['summary']['errors'] else 0)
