from __future__ import annotations

import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1] / "scripts" / "validate_contract_ooxml.py"
)
SPEC = importlib.util.spec_from_file_location("validate_contract_ooxml", SCRIPT)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
COMMENTS_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
)


class ContractOoxmlValidatorTest(unittest.TestCase):
    def test_clean_synthetic_docx_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clean.docx"
            self._docx(path)

            report = validator.inspect_docx(path)

        self.assertEqual(0, report["summary"]["errors"])
        self.assertEqual([], report["findings"])
        self.assertEqual("YKX", report["expected_author"])
        self.assertEqual(1, report["summary"]["tracked_changes"])
        self.assertEqual(1, report["summary"]["comments"])

    def test_revision_and_comment_authors_must_be_ykx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "authors.docx"
            self._docx(path, revision_author="Other", comment_author="Reviewer")

            report = validator.inspect_docx(path)
            codes = self._codes(report)

        self.assertIn("revision.author", codes)
        self.assertIn("comment.author", codes)
        author_findings = [
            item for item in report["findings"] if item["code"].endswith(".author")
        ]
        self.assertTrue(all(item["element_id"] == "0" for item in author_findings))

    def test_comment_relationship_and_reference_mapping_are_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping.docx"
            self._docx(path, comment_reference_id="9", include_comments_rel=False)

            report = validator.inspect_docx(path)
            codes = self._codes(report)

        self.assertIn("comments.relationship_missing", codes)
        self.assertIn("comment.reference_missing", codes)
        self.assertIn("comment.unreferenced", codes)

    def test_contract_number_revision_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "number.docx"
            self._docx(path, revision_text="合同编号：HT-2026-001")

            report = validator.inspect_docx(path)

        self.assertIn("contract_number.edited", self._codes(report))

    def test_unresolved_placeholders_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "placeholder.docx"
            self._docx(path, body_suffix="[待填写] {{amount}}")

            report = validator.inspect_docx(path)

        placeholders = [
            item
            for item in report["findings"]
            if item["code"] == "placeholder.unresolved"
        ]
        self.assertGreaterEqual(len(placeholders), 2)
        self.assertTrue(all(item["part"] == "word/document.xml" for item in placeholders))

    def test_malformed_relationship_target_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "relation.docx"
            self._docx(path, comments_target="missing-comments.xml")

            report = validator.inspect_docx(path)

        self.assertIn("relationship.target_missing", self._codes(report))

    def test_invalid_zip_and_missing_document_are_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            invalid = Path(tmp) / "invalid.docx"
            invalid.write_text("not a zip", encoding="utf-8")
            missing = Path(tmp) / "missing.docx"
            with zipfile.ZipFile(missing, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")

            invalid_report = validator.inspect_docx(invalid)
            missing_report = validator.inspect_docx(missing)

        self.assertIn("docx.invalid_zip", self._codes(invalid_report))
        self.assertIn("part.document_missing", self._codes(missing_report))

    def test_full_paragraph_delete_insert_replacement_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "block-replacement.docx"
            self._docx_with_body(
                path,
                """
    <w:p w:paraId="bad1">
      <w:del w:id="10" w:author="YKX"><w:r><w:delText>乙方应当按照甲方要求完成全部工作并承担相应责任。</w:delText></w:r></w:del>
      <w:ins w:id="11" w:author="YKX"><w:r><w:t>乙方应在约定期限内依照验收标准完成全部工作；逾期或不合格的，应承担返工、赔偿及违约责任。</w:t></w:r></w:ins>
    </w:p>
                """,
            )

            report = validator.inspect_docx(path)

        self.assertIn("revision.block_replacement", self._codes(report))
        self.assertEqual(1, report["summary"]["block_replacements"])
        self.assertGreater(report["summary"]["errors"], 0)

    def test_precision_redline_with_unchanged_text_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "precision-redline.docx"
            self._docx_with_body(
                path,
                """
    <w:p w:paraId="good1">
      <w:r><w:t>乙方应在收到通知后</w:t></w:r>
      <w:del w:id="20" w:author="YKX"><w:r><w:delText>五日</w:delText></w:r></w:del>
      <w:ins w:id="21" w:author="YKX"><w:r><w:t>三个工作日</w:t></w:r></w:ins>
      <w:r><w:t>内完成整改并提交书面报告。</w:t></w:r>
    </w:p>
                """,
            )

            report = validator.inspect_docx(path)

        self.assertNotIn("revision.block_replacement", self._codes(report))
        self.assertEqual(0, report["summary"]["block_replacements"])

    def test_whole_clause_addition_or_deletion_alone_is_not_block_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "whole-clause-one-way.docx"
            self._docx_with_body(
                path,
                """
    <w:p w:paraId="new1">
      <w:ins w:id="30" w:author="YKX"><w:r><w:t>新增一条完整但没有对应删除内容的合同条款。</w:t></w:r></w:ins>
    </w:p>
    <w:p w:paraId="delete1">
      <w:del w:id="31" w:author="YKX"><w:r><w:delText>删除一条完整且不以新条款替换的原合同条款。</w:delText></w:r></w:del>
    </w:p>
                """,
            )

            report = validator.inspect_docx(path)

        self.assertNotIn("revision.block_replacement", self._codes(report))
        self.assertEqual(0, report["summary"]["block_replacements"])

    def _codes(self, report: dict) -> set[str]:
        return {item["code"] for item in report["findings"]}

    def _docx(
        self,
        path: Path,
        *,
        revision_author: str = "YKX",
        comment_author: str = "YKX",
        revision_text: str = "明确义务",
        comment_reference_id: str = "0",
        include_comments_rel: bool = True,
        comments_target: str = "comments.xml",
        body_suffix: str = "",
    ) -> None:
        document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W}" xmlns:r="{R}">
  <w:body>
    <w:p w:paraId="p1">
      <w:commentRangeStart w:id="{comment_reference_id}"/>
      <w:ins w:id="0" w:author="{revision_author}">
        <w:r><w:t>{revision_text}</w:t></w:r>
      </w:ins>
      <w:commentRangeEnd w:id="{comment_reference_id}"/>
      <w:r><w:commentReference w:id="{comment_reference_id}"/></w:r>
      <w:r><w:t>{body_suffix}</w:t></w:r>
    </w:p>
  </w:body>
</w:document>"""
        comments = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:comments xmlns:w="{W}">
  <w:comment w:id="0" w:author="{comment_author}">
    <w:p><w:r><w:t>核验提示</w:t></w:r></w:p>
  </w:comment>
</w:comments>"""
        relationships = [f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PR}">"""]
        if include_comments_rel:
            relationships.append(
                f'<Relationship Id="rId1" Type="{COMMENTS_REL}" '
                f'Target="{comments_target}"/>'
            )
        relationships.append("</Relationships>")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/comments.xml", comments)
            archive.writestr(
                "word/_rels/document.xml.rels", "".join(relationships)
            )

    def _docx_with_body(self, path: Path, body_xml: str) -> None:
        document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W}" xmlns:r="{R}">
  <w:body>{body_xml}</w:body>
</w:document>"""
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", document)


if __name__ == "__main__":
    unittest.main()
