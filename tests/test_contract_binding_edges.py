import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('contract_edges', ROOT / 'scripts/validate_contract_ooxml.py')
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
PR = 'http://schemas.openxmlformats.org/package/2006/relationships'
COMMENTS_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments'


def paragraph(text):
    return '<w:p><w:r><w:t>' + escape(text) + '</w:t></w:r></w:p>'


class ContractBindingEdgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'source.docx'
        self.output = Path(self.temp.name) / 'reviewed.docx'
        self.body = ''.join(paragraph(x) for x in [
            '示例维护协议', '合同编号：SYN-001',
            '甲方：示例委托单位', '乙方：示例服务单位',
        ])

    def write(self, path, body, *, comments='', target='comments.xml', external=False):
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('word/document.xml', f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
            archive.writestr('word/styles.xml', f'<w:styles xmlns:w="{W}"/>')
            if comments:
                archive.writestr('word/comments.xml', f'<w:comments xmlns:w="{W}">{comments}</w:comments>')
                mode = ' TargetMode="External"' if external else ''
                archive.writestr('word/_rels/document.xml.rels',
                    f'<Relationships xmlns="{PR}"><Relationship Id="c" Type="{COMMENTS_REL}" Target="{target}"{mode}/></Relationships>')

    def identity(self):
        return {
            'contract_number': 'SYN-001', 'title': '示例维护协议',
            'parties': ['示例委托单位', '示例服务单位'],
            'reviewing_party': '示例委托单位', 'source_path': str(self.source),
            'source_sha256': hashlib.sha256(self.source.read_bytes()).hexdigest(),
            'output_path': str(self.output),
        }

    def test_every_source_party_declaration_is_bound(self):
        self.write(self.source, self.body + paragraph('乙方：示例服务单位'))
        self.write(self.output, self.body + paragraph('乙方：其他服务单位'))
        report = CHECKER.inspect_docx(self.output, baseline=self.source,
                                      identity=self.identity(), require_identity=True)
        self.assertEqual(report['identity_gate'], 'fail')
        self.assertIn('identity.parties', {x['code'] for x in report['findings']})

    def comment_body(self):
        return (self.body + '<w:p><w:commentRangeStart w:id="0"/><w:r><w:t>条款</w:t></w:r>'
                '<w:commentRangeEnd w:id="0"/><w:r><w:commentReference w:id="0"/></w:r></w:p>')

    def test_comment_relationship_must_bind_the_internal_comment_part(self):
        comment = '<w:comment w:id="0" w:author="Reviewer">' + paragraph('核对') + '</w:comment>'
        for target, external in [('styles.xml', False), ('comments.xml', True),
                                 ('../../word/comments.xml', False)]:
            with self.subTest(target=target, external=external):
                self.write(self.output, self.comment_body(), comments=comment, target=target, external=external)
                report = CHECKER.inspect_docx(self.output, expected_author='Reviewer')
                self.assertGreater(report['summary']['errors'], 0)
                self.assertIn('comments.relationship_invalid', {x['code'] for x in report['findings']})

    def test_duplicate_comment_ids_are_rejected(self):
        comments = ''.join('<w:comment w:id="0" w:author="Reviewer">' + paragraph(text) + '</w:comment>'
                           for text in ('第一条', '第二条'))
        self.write(self.output, self.comment_body(), comments=comments)
        report = CHECKER.inspect_docx(self.output, expected_author='Reviewer')
        self.assertIn('comments.id_duplicate', {x['code'] for x in report['findings']})


if __name__ == '__main__':
    unittest.main()
