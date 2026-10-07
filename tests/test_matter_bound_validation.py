from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape


SCRIPT = Path(__file__).parents[1] / "scripts" / "validate_contract_ooxml.py"
SPEC = importlib.util.spec_from_file_location("matter_validator", SCRIPT)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"


def paragraph(text: str) -> str:
    return f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>"


def revision(kind: str, text: str, author: str = "YKX", ident: str = "1") -> str:
    tag = "delText" if kind in {"del", "moveFrom"} else "t"
    return (f'<w:{kind} w:id="{ident}" w:author="{author}">'
            f"<w:r><w:{tag}>{escape(text)}</w:{tag}></w:r></w:{kind}>")


class MatterBoundValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "source.docx"
        self.output = self.root / "reviewed.docx"
        self.body = (paragraph("示例维护协议") + paragraph("合同编号：SYN-001")
                     + paragraph("甲方：示例委托单位") + paragraph("乙方：示例服务单位"))
        self.write(self.source, self.body)
        self.write(self.output, self.body)

    def write(self, path: Path, body: str, comments: str = "", header: str = "") -> None:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml",
                             f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
            if comments:
                archive.writestr("word/comments.xml", f'<w:comments xmlns:w="{W}">{comments}</w:comments>')
                archive.writestr("word/_rels/document.xml.rels", f'<Relationships xmlns="{PR}">'
                                 '<Relationship Id="c" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>'
                                 '</Relationships>')
            if header:
                archive.writestr("word/header1.xml", f'<w:hdr xmlns:w="{W}">{header}</w:hdr>')

    def identity(self) -> dict:
        return {"contract_number": "SYN-001", "title": "示例维护协议",
                "parties": ["示例委托单位", "示例服务单位"], "reviewing_party": "示例委托单位",
                "source_path": str(self.source), "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                "output_path": str(self.output)}

    def inspect(self, **kwargs) -> dict:
        return validator.inspect_docx(self.output, baseline=self.source, **kwargs)

    @staticmethod
    def codes(report: dict) -> set[str]:
        return {item["code"] for item in report["findings"]}

    def test_deleted_placeholder_is_not_in_accepted_text(self):
        self.write(self.output, self.body + "<w:p>" + revision("del", "[待填写]")
                   + revision("ins", "三日", ident="2") + "</w:p>")
        self.assertNotIn("placeholder.unresolved", self.codes(validator.inspect_docx(self.output)))

    def test_move_from_placeholder_is_not_in_accepted_text(self):
        self.write(self.output, self.body + "<w:p>" + revision("moveFrom", "{{old}}") + "</w:p>")
        self.assertNotIn("placeholder.unresolved", self.codes(validator.inspect_docx(self.output)))

    def test_inherited_authors_are_preserved_but_new_authors_are_checked(self):
        old = "<w:p>" + revision("ins", "历史修订", "PreviousReviewer") + "</w:p>"
        anchors = '<w:p><w:commentRangeStart w:id="8"/><w:r><w:t>核验点</w:t></w:r><w:commentRangeEnd w:id="8"/><w:r><w:commentReference w:id="8"/></w:r></w:p>'
        comment = '<w:comment w:id="8" w:author="PreviousReviewer">' + paragraph("历史批注") + '</w:comment>'
        self.write(self.source, self.body + old + anchors, comment)
        self.write(self.output, self.body + old + anchors, comment)
        self.assertEqual(0, self.inspect()["summary"]["errors"])
        self.write(self.output, self.body + old + anchors + "<w:p>" + revision("ins", "新修订", "Other", "9") + "</w:p>", comment)
        self.assertIn("revision.author", self.codes(self.inspect()))

    def test_reusing_old_revision_id_does_not_hide_changed_content(self):
        self.write(self.source, self.body + "<w:p>" + revision("ins", "旧文", "Old") + "</w:p>")
        self.write(self.output, self.body + "<w:p>" + revision("ins", "新文", "Old") + "</w:p>")
        self.assertIn("revision.author", self.codes(self.inspect()))

    def test_preserved_signature_blank_is_warning_in_review_but_error_at_signing(self):
        body = self.body + paragraph("签署日期：____年____月____日")
        self.write(self.source, body)
        self.write(self.output, body)
        report = self.inspect()
        self.assertEqual(0, report["summary"]["errors"])
        self.assertIn("placeholder.preserved_blank", self.codes(report))
        self.assertIn("placeholder.unresolved", self.codes(self.inspect(mode="finalization")))

    def test_new_blank_or_new_copy_of_blank_is_not_grandfathered(self):
        blank = paragraph("签署日期：____年____月____日")
        self.write(self.source, self.body + blank)
        self.write(self.output, self.body + blank + blank)
        self.assertIn("placeholder.unresolved", self.codes(self.inspect()))

    def test_preserved_drafting_prompt_is_still_error(self):
        self.write(self.source, self.body + paragraph("[待填写]"))
        self.write(self.output, self.body + paragraph("[待填写]"))
        self.assertIn("placeholder.unresolved", self.codes(self.inspect()))

    def test_value_only_number_revision_is_detected(self):
        number = '<w:p><w:r><w:t>合同编号：</w:t></w:r>' + revision("del", "SYN-001") + revision("ins", "SYN-002", ident="2") + '</w:p>'
        self.write(self.output, self.body.replace(paragraph("合同编号：SYN-001"), number))
        self.assertIn("contract_number.edited", self.codes(validator.inspect_docx(self.output)))
        self.assertIn("identity.contract_number", self.codes(self.inspect(identity=self.identity())))

    def test_correct_matter_passes_with_hashes_and_is_read_only(self):
        before = (self.source.read_bytes(), self.output.read_bytes())
        report = self.inspect(identity=self.identity())
        self.assertEqual("pass", report["identity_gate"])
        self.assertEqual(0, report["summary"]["errors"])
        self.assertEqual(hashlib.sha256(before[1]).hexdigest(), report["output_sha256"])
        self.assertEqual(before, (self.source.read_bytes(), self.output.read_bytes()))

    def test_no_identity_is_explicitly_structural_only(self):
        self.assertEqual("not_checked", validator.inspect_docx(self.output)["identity_gate"])

    def test_wrong_matter_number_title_and_party_are_blocked(self):
        self.write(self.output, self.body.replace("SYN-001", "SYN-002").replace("示例维护协议", "示例租赁协议").replace("示例服务单位", "其他单位"))
        report = self.inspect(identity=self.identity())
        self.assertEqual("fail", report["identity_gate"])
        self.assertTrue({"identity.contract_number", "identity.title", "identity.parties"} <= self.codes(report))

    def test_number_prefix_is_not_an_identity_match(self):
        self.write(self.output, self.body.replace("SYN-001", "SYN-0010"))
        self.assertIn("identity.contract_number", self.codes(self.inspect(identity=self.identity())))

    def test_old_title_only_in_deleted_text_does_not_match(self):
        self.write(self.output, self.body.replace(paragraph("示例维护协议"), "<w:p>" + revision("del", "示例维护协议") + revision("ins", "示例租赁协议", ident="2") + "</w:p>"))
        self.assertIn("identity.title", self.codes(self.inspect(identity=self.identity())))

    def test_wrong_source_is_blocked_even_if_output_has_expected_words(self):
        self.write(self.source, self.body.replace("SYN-001", "SYN-009"))
        self.assertIn("identity.source_contract_number", self.codes(self.inspect(identity=self.identity())))

    def test_stale_source_hash_blocks_gate(self):
        identity = self.identity()
        self.write(self.source, self.body + paragraph("用户后改"))
        self.assertIn("identity.source_sha256", self.codes(self.inspect(identity=identity)))

    def test_source_and_output_paths_are_bound(self):
        for field in ("source_path", "output_path"):
            with self.subTest(field=field):
                identity = self.identity()
                identity[field] = str(self.root / "different.docx")
                self.assertIn("identity." + field, self.codes(self.inspect(identity=identity)))

    def test_same_file_alias_is_rejected(self):
        alias = self.root / "alias.docx"
        alias.symlink_to(self.source)
        report = validator.inspect_docx(alias, baseline=self.source)
        self.assertIn("baseline.same_file", self.codes(report))

    def test_incomplete_or_malformed_identity_fails_closed(self):
        for identity in ({}, [], {"title": "示例维护协议"}):
            with self.subTest(identity=identity):
                report = self.inspect(identity=identity)
                self.assertEqual("fail", report["identity_gate"])
                self.assertIn("identity.invalid", self.codes(report))

    def test_unlisted_reviewing_party_fails_closed(self):
        identity = self.identity()
        identity["reviewing_party"] = "其他委托人"
        self.assertIn("identity.invalid", self.codes(self.inspect(identity=identity)))

    def test_identity_requires_readable_baseline(self):
        report = validator.inspect_docx(self.output, identity=self.identity())
        self.assertEqual("fail", report["identity_gate"])
        self.assertIn("baseline.required", self.codes(report))

    def test_header_placeholders_and_header_only_contract_number_are_checked(self):
        body = self.body.replace(paragraph("合同编号：SYN-001"), "")
        self.write(self.source, body, header=paragraph("合同编号：SYN-001"))
        self.write(self.output, body, header=paragraph("合同编号：SYN-001"))
        self.assertEqual("pass", self.inspect(identity=self.identity())["identity_gate"])
        self.write(self.output, body, header=paragraph("合同编号：SYN-002 {{header}}"))
        report = self.inspect(identity=self.identity())
        self.assertIn("identity.contract_number", self.codes(report))
        self.assertIn("placeholder.unresolved", self.codes(report))

    def test_cli_delivery_requires_identity_and_rejects_bad_json(self):
        missing = subprocess.run([sys.executable, "-B", str(SCRIPT), str(self.output), "--require-identity", "--json"], capture_output=True, text=True)
        self.assertEqual(1, missing.returncode)
        self.assertEqual("fail", json.loads(missing.stdout)["identity_gate"])
        manifest = self.root / "identity.json"
        manifest.write_text("{broken", encoding="utf-8")
        broken = subprocess.run([sys.executable, "-B", str(SCRIPT), str(self.output), "--identity", str(manifest), "--json"], capture_output=True, text=True)
        self.assertEqual(1, broken.returncode)
        self.assertIn("identity.invalid", self.codes(json.loads(broken.stdout)))

    def test_deleted_whole_paragraph_does_not_supply_current_identity_or_placeholder(self):
        deleted = '<w:del w:id="5" w:author="YKX">' + paragraph("合同编号：SYN-002 [待填写]") + '</w:del>'
        self.write(self.output, self.body + deleted)
        report = self.inspect(identity=self.identity())
        self.assertEqual(0, report["summary"]["errors"])
        self.assertEqual("pass", report["identity_gate"])

    def test_party_prefix_and_split_paragraph_title_are_not_identity_matches(self):
        self.write(self.output, self.body.replace("示例服务单位", "示例服务单位分公司"))
        self.assertIn("identity.parties", self.codes(self.inspect(identity=self.identity())))
        self.write(self.output, self.body.replace(paragraph("示例维护协议"), paragraph("示例维护") + paragraph("协议")))
        self.assertIn("identity.title", self.codes(self.inspect(identity=self.identity())))

    def test_unchanged_inherited_number_edit_is_not_a_new_number_warning(self):
        number = '<w:p><w:r><w:t>合同编号：</w:t></w:r>' + revision("del", "OLD-001", "Old") + revision("ins", "SYN-001", "Old", "2") + '</w:p>'
        body = self.body.replace(paragraph("合同编号：SYN-001"), number)
        self.write(self.source, body)
        self.write(self.output, body)
        self.assertNotIn("contract_number.edited", self.codes(self.inspect()))

    def test_table_separate_number_cell_and_numeric_only_change_are_checked(self):
        table = '<w:tbl><w:tr><w:tc>' + paragraph("合同编号：") + '</w:tc><w:tc>' + paragraph("SYN-001") + '</w:tc></w:tr></w:tbl>'
        self.write(self.source, self.body.replace(paragraph("合同编号：SYN-001"), table))
        self.write(self.output, self.body.replace(paragraph("合同编号：SYN-001"), table))
        self.assertEqual("pass", self.inspect(identity=self.identity())["identity_gate"])
        changed = '<w:p><w:r><w:t>SYN-</w:t></w:r>' + revision("del", "001") + revision("ins", "002", ident="2") + '</w:p>'
        self.write(self.output, self.body.replace(paragraph("合同编号：SYN-001"), table.replace(paragraph("SYN-001"), changed)))
        self.assertIn("identity.contract_number", self.codes(self.inspect(identity=self.identity())))

    def test_identity_reports_plain_number_change_without_tracking(self):
        self.write(self.output, self.body.replace("SYN-001", "SYN-002"))
        self.assertIn("identity.contract_number", self.codes(self.inspect(identity=self.identity())))

    def test_duplicate_history_node_is_not_author_exempt(self):
        old = '<w:p>' + revision("ins", "旧文", "Old") + '</w:p>'
        self.write(self.source, self.body + old)
        self.write(self.output, self.body + old + old)
        self.assertIn("revision.author", self.codes(self.inspect()))

    def test_new_wrong_comment_author_is_not_exempt(self):
        anchor = '<w:p><w:r><w:commentReference w:id="8"/></w:r></w:p>'
        old = '<w:comment w:id="8" w:author="Old">' + paragraph("原批注") + '</w:comment>'
        new = '<w:comment w:id="8" w:author="Old">' + paragraph("新批注") + '</w:comment>'
        self.write(self.source, self.body + anchor, old)
        self.write(self.output, self.body + anchor, new)
        self.assertIn("comment.author", self.codes(self.inspect()))

    def test_inherited_block_replacement_is_not_rewritten_to_satisfy_validator(self):
        old = '<w:p>' + revision("del", "这是此前作者删除的一条完整条款，不应擅自重新编辑。", "Old") + revision("ins", "这是此前作者插入的另一条完整条款，用户已经保留此文。", "Old", "2") + '</w:p>'
        self.write(self.source, self.body + old)
        self.write(self.output, self.body + old)
        self.assertEqual(0, self.inspect()["summary"]["block_replacements"])

    def test_correct_cli_matter_bound_result_has_success_status(self):
        manifest = self.root / "identity.json"
        manifest.write_text(json.dumps(self.identity(), ensure_ascii=False), encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), str(self.output), "--baseline", str(self.source), "--identity", str(manifest), "--require-identity", "--json"], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("pass", json.loads(result.stdout)["identity_gate"])

    def test_explicit_absent_number_supports_unnumbered_mother(self):
        body = self.body.replace(paragraph("合同编号：SYN-001"), "")
        self.write(self.source, body)
        self.write(self.output, body)
        identity = self.identity()
        identity["contract_number"] = None
        self.assertEqual("pass", self.inspect(identity=identity)["identity_gate"])
        self.write(self.output, body + paragraph("合同编号：SYN-009"))
        self.assertIn("identity.contract_number", self.codes(self.inspect(identity=identity)))

    def test_retained_unfilled_number_does_not_require_filling_during_review(self):
        body = self.body.replace("合同编号：SYN-001", "合同编号：____")
        self.write(self.source, body)
        self.write(self.output, body)
        identity = self.identity()
        identity["contract_number"] = None
        report = self.inspect(identity=identity)
        self.assertEqual("pass", report["identity_gate"])
        self.assertIn("placeholder.preserved_blank", self.codes(report))
        self.assertGreater(self.inspect(identity=identity, mode="finalization")["summary"]["errors"], 0)

    def test_old_matter_mentioned_in_body_cannot_replace_identity_fields(self):
        body = self.body.replace("示例维护协议", "示例租赁协议").replace("乙方：示例服务单位", "乙方：其他服务单位")
        body += paragraph("本合同取代《示例维护协议》，此前服务商为：示例服务单位。")
        self.write(self.output, body)
        codes = self.codes(self.inspect(identity=self.identity()))
        self.assertTrue({"identity.title", "identity.parties"} <= codes)

    def test_old_reference_in_wrong_mother_cannot_supply_source_identity(self):
        body = self.body.replace("示例维护协议", "示例租赁协议").replace("乙方：示例服务单位", "乙方：其他服务单位")
        body += paragraph("本合同取代《示例维护协议》，此前服务商为：示例服务单位。")
        self.write(self.source, body)
        self.write(self.output, body)
        codes = self.codes(self.inspect(identity=self.identity()))
        self.assertTrue({"identity.source_title", "identity.source_parties"} <= codes)

    def test_same_paragraph_edit_preserves_unchanged_blank(self):
        self.write(self.source, self.body + paragraph("乙方应在____日内完成交付。"))
        changed = '<w:p><w:r><w:t>乙方应在____日内完成</w:t></w:r>' + revision("del", "交付") + revision("ins", "验收", ident="2") + '<w:r><w:t>。</w:t></w:r></w:p>'
        self.write(self.output, self.body + changed)
        self.assertEqual(0, self.inspect()["summary"]["errors"])

    def test_reinserted_blank_is_not_unchanged_source_text(self):
        self.write(self.source, self.body + paragraph("乙方应在____日内完成交付。"))
        changed = '<w:p><w:r><w:t>乙方应在</w:t></w:r>' + revision("del", "____") + revision("ins", "____", ident="2") + '<w:r><w:t>日内完成交付。</w:t></w:r></w:p>'
        self.write(self.output, self.body + changed)
        self.assertIn("placeholder.unresolved", self.codes(self.inspect()))

    def test_new_paragraph_id_does_not_change_preserved_source_blank(self):
        blank = paragraph("签署日期：____年____月____日")
        self.write(self.source, self.body + blank)
        saved = blank.replace("<w:p>", f'<w:p xmlns:w14="{W14}" w14:paraId="12345678">')
        self.write(self.output, self.body + saved)
        report = self.inspect(identity=self.identity())
        self.assertEqual("pass", report["identity_gate"])
        self.assertEqual(0, report["summary"]["errors"])
        self.assertIn("placeholder.preserved_blank", self.codes(report))

    def test_source_paragraph_id_must_remain_bound_when_present(self):
        blank = paragraph("签署日期：____年____月____日")
        identified = blank.replace("<w:p>", f'<w:p xmlns:w14="{W14}" w14:paraId="12345678">')
        self.write(self.source, self.body + identified)
        for candidate in (blank, identified.replace("12345678", "87654321")):
            with self.subTest(candidate=candidate):
                self.write(self.output, self.body + candidate)
                self.assertIn("placeholder.unresolved", self.codes(self.inspect()))
        self.write(self.output, self.body + paragraph("新增说明") + identified)
        self.assertEqual(0, self.inspect()["summary"]["errors"])

    def test_new_whole_paragraph_revision_does_not_preserve_source_blank(self):
        blank = paragraph("签署日期：____年____月____日")
        self.write(self.source, self.body + blank)
        for kind in ("ins", "moveTo"):
            with self.subTest(kind=kind):
                added = f'<w:{kind} w:id="8" w:author="YKX">{blank}</w:{kind}>'
                self.write(self.output, self.body + added)
                self.assertIn("placeholder.unresolved", self.codes(self.inspect()))

    def test_inherited_whole_paragraph_revision_preserves_source_blank(self):
        blank = paragraph("签署日期：____年____月____日")
        old = f'<w:ins w:id="8" w:author="Old">{blank}</w:ins>'
        self.write(self.source, self.body + old)
        self.write(self.output, self.body + old)
        self.assertEqual(0, self.inspect()["summary"]["errors"])
        self.assertIn("placeholder.preserved_blank", self.codes(self.inspect()))

    def test_changed_field_instruction_is_not_inherited_revision(self):
        field = '<w:p><w:ins w:id="1" w:author="Old"><w:r><w:instrText>REF OldBookmark</w:instrText><w:t>十日</w:t></w:r></w:ins></w:p>'
        self.write(self.source, self.body + field)
        self.write(self.output, self.body + field.replace("OldBookmark", "NewBookmark"))
        self.assertIn("revision.author", self.codes(self.inspect()))

    def test_new_format_revision_author_is_checked(self):
        body = self.body + '<w:p><w:r><w:rPr><w:b/><w:rPrChange w:id="1" w:author="Other"><w:rPr/></w:rPrChange></w:rPr><w:t>重点条款</w:t></w:r></w:p>'
        self.write(self.output, body)
        self.assertIn("revision.author", self.codes(self.inspect()))

    def test_non_word_document_root_fails_closed(self):
        with zipfile.ZipFile(self.output, "w") as archive:
            archive.writestr("word/document.xml", f'<arbitrary xmlns:w="{W}">{self.body}</arbitrary>')
        report = self.inspect(identity=self.identity())
        self.assertEqual("fail", report["identity_gate"])
        self.assertIn("xml.invalid_structure", self.codes(report))

    def test_corrupt_zip_member_returns_failure_report(self):
        payload = self.output.read_bytes().replace(b"SYN-001", b"SYN-002", 1)
        self.output.write_bytes(payload)
        report = self.inspect(identity=self.identity())
        self.assertEqual("fail", report["identity_gate"])
        self.assertIn("zip.member_unreadable", self.codes(report))

    def test_nul_in_identity_path_returns_failure_report(self):
        identity = self.identity()
        identity["source_path"] = str(self.root) + "/\0source.docx"
        report = self.inspect(identity=identity)
        self.assertEqual("fail", report["identity_gate"])
        self.assertIn("identity.invalid", self.codes(report))


if __name__ == "__main__":
    unittest.main()
