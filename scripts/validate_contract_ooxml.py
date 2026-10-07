#!/usr/bin/env python3
"""Read-only deterministic checks for contract-review DOCX OOXML."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import zipfile
import zlib
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
COMMENTS_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
)
DOCUMENT_PART = "word/document.xml"
COMMENTS_PART = "word/comments.xml"
RELS_PART = "word/_rels/document.xml.rels"
TRACKED_TAGS = {f"{{{W}}}{name}" for name in (
    "ins", "del", "moveFrom", "moveTo", "rPrChange", "pPrChange",
    "tblPrChange", "trPrChange", "tcPrChange", "sectPrChange", "tblGridChange",
    "numberingChange", "cellIns", "cellDel", "cellMerge",
)}
COMMENT_REFERENCE_TAGS = {
    f"{{{W}}}commentRangeStart",
    f"{{{W}}}commentRangeEnd",
    f"{{{W}}}commentReference",
}
PLACEHOLDER_PATTERNS = (
    re.compile(r"\[(?:待填写|待补充|待确认|请填写)\]"),
    re.compile(r"\{\{[^{}\r\n]{1,100}\}\}"),
    re.compile(r"(?<![A-Za-z])X{3,}(?![A-Za-z])", re.IGNORECASE),
    re.compile(r"_{4,}"),
)
CONTRACT_NUMBER_PATTERN = re.compile(
    r"(?:合同|协议)\s*(?:编号|号)|(?:编号|合同号)\s*[:：]",
    re.IGNORECASE,
)
BLOCK_REPLACEMENT_MIN_CHARS = 20
BLOCK_REPLACEMENT_MAX_UNCHANGED_CHARS = 8
BLOCK_REPLACEMENT_MAX_UNCHANGED_RATIO = 0.10
STORY_PART_PATTERN = re.compile(r"word/(?:document|header\d+|footer\d+|footnotes|endnotes)\.xml")
NUMBER_VALUE_PATTERN = re.compile(r"(?:(?:合同|协议)\s*(?:编号|号)|编号)\s*[:：]?\s*([^\s:：，,；;。]+)")


def _finding(
    code: str,
    severity: str,
    part: str,
    element_id: str,
    message: str,
) -> dict[str, str]:
    return {
        "code": code,
        "severity": severity,
        "part": part,
        "element_id": element_id,
        "message": message,
    }


def _parse_part(
    archive: zipfile.ZipFile,
    part: str,
    findings: list[dict[str, str]],
) -> ET.Element | None:
    try:
        payload = archive.read(part)
    except KeyError:
        findings.append(
            _finding(
                f"part.{Path(part).stem}_missing",
                "error",
                part,
                "",
                f"Required OOXML part is missing: {part}",
            )
        )
        return None
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error):
        findings.append(_finding("zip.member_unreadable", "error", part, "", "OOXML package member cannot be read or has a bad checksum."))
        return None
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        findings.append(
            _finding(
                "xml.malformed",
                "error",
                part,
                "",
                "OOXML part is not well-formed XML.",
            )
        )
        return None
    expected = (f"{{{PR}}}Relationships" if part == RELS_PART else
                f"{{{W}}}document" if part == DOCUMENT_PART else
                f"{{{W}}}comments" if part == COMMENTS_PART else
                f"{{{W}}}hdr" if re.fullmatch(r"word/header\d+\.xml", part) else
                f"{{{W}}}ftr" if re.fullmatch(r"word/footer\d+\.xml", part) else
                f"{{{W}}}{Path(part).stem}")
    if root.tag != expected or (part == DOCUMENT_PART and len(root.findall(f"{{{W}}}body")) != 1):
        findings.append(_finding("xml.invalid_structure", "error", part, "", "OOXML part has the wrong root element or document body."))
        return None
    return root


def _element_text(element: ET.Element) -> str:
    return "".join(
        node.text or ""
        for node in element.iter()
        if node.tag in {f"{{{W}}}t", f"{{{W}}}delText"}
    )


def _visible_text(element: ET.Element, *, original: bool = False) -> str:
    """Read a view, without accepting or rejecting any revisions in the file."""
    skipped = {f"{{{W}}}ins", f"{{{W}}}moveTo"} if original else {f"{{{W}}}del", f"{{{W}}}moveFrom"}
    if element.tag in skipped:
        return ""
    if element.tag in {f"{{{W}}}t", f"{{{W}}}delText"}:
        return element.text or ""
    if element.tag in {f"{{{W}}}tab", f"{{{W}}}br", f"{{{W}}}cr"}:
        return " "
    text = "".join(_visible_text(child, original=original) for child in element)
    return text + ("\n" if element.tag == f"{{{W}}}p" else "")


def _signature(element: ET.Element) -> tuple:
    # Namespace-expanded tags ignore serialization prefixes; formatting inside a
    # revision is still significant. XML indentation outside text nodes is not.
    return (element.tag, tuple(sorted(element.attrib.items())),
            element.text if element.text and (element.text.strip() or element.tag in
                {f"{{{W}}}t", f"{{{W}}}delText", f"{{{W}}}instrText", f"{{{W}}}delInstrText"}) else None,
            tuple(_signature(child) for child in element))


def _view_elements(root: ET.Element, tags: set[str], *, original: bool = False):
    skipped = {f"{{{W}}}ins", f"{{{W}}}moveTo"} if original else {f"{{{W}}}del", f"{{{W}}}moveFrom"}
    if root.tag in skipped:
        return
    if root.tag in tags:
        yield root
    for child in root:
        yield from _view_elements(child, tags, original=original)


def _read_baseline(path: Path, findings: list[dict[str, str]]) -> tuple[dict[str, ET.Element], str]:
    try:
        payload = path.read_bytes()
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            if DOCUMENT_PART not in names:
                raise ValueError("missing document")
            parts = {name: _parse_part(archive, name, findings) for name in names
                     if STORY_PART_PATTERN.fullmatch(name) or name == COMMENTS_PART}
            if any(root is None for root in parts.values()):
                raise ValueError("invalid source structure")
        return parts, hashlib.sha256(payload).hexdigest()
    except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError, KeyError):
        findings.append(_finding("baseline.invalid", "error", "", "", "Mother DOCX is missing, unreadable, or malformed."))
        return {}, ""


def _number_values(root: ET.Element, *, original: bool = False) -> set[str]:
    # Paragraphs cover normal fields; rows also cover a label and value in
    # different table cells. Deleted text is excluded in the current view.
    contexts = _view_elements(root, {f"{{{W}}}p", f"{{{W}}}tr"}, original=original)
    return {match.group(1) for context in contexts
            for match in NUMBER_VALUE_PATTERN.finditer(_visible_text(context, original=original))
            if not re.fullmatch(r"_+", match.group(1))}


def _matching_paragraph(source: ET.Element, index: int, candidates: list[ET.Element]) -> ET.Element | None:
    ident = source.get(f"{{{W14}}}paraId") or source.get(f"{{{W}}}paraId")
    if ident:
        matches = [p for p in candidates if ident in (p.get(f"{{{W14}}}paraId"), p.get(f"{{{W}}}paraId"))]
        return matches[0] if len(matches) == 1 else None
    return candidates[index] if index < len(candidates) else None


def _field_marker(paragraph: ET.Element, field: str, value: str) -> str | None:
    text = re.sub(r"\s+", "", _visible_text(paragraph))
    literal = re.sub(r"\s+", "", value)
    if text.strip("《》“”\"") == literal:
        return "standalone"
    if field == "title":
        for label in ("合同名称", "协议名称", "合同标题", "协议标题"):
            if re.fullmatch(re.escape(label) + r"[:：]《?" + re.escape(literal) + r"》?", text):
                return label
        return None
    roles = r"甲方|乙方|丙方|丁方|戊方|己方|庚方|采购人|供应商|委托人|受托人|委托方|受托方|买方|卖方|出卖人|买受人|发包人|承包人|出租人|承租人|许可方|被许可方|保证人"
    for match in re.finditer(r"(?:^|[；;])(" + roles + r")(?:[（(][^）)]{0,30}[）)])?[:：]", text):
        rest = text[match.end():]
        if re.match(re.escape(literal) + r"(?![\w\u3400-\u9fff])", rest):
            return match.group(1)
    return None


def _identity_checks(identity: Any, baseline: Path | None, source_parts: dict,
                     source_hash: str, output: Path, parts: dict,
                     findings: list[dict[str, str]]) -> None:
    required = {"contract_number", "title", "parties", "reviewing_party", "source_path", "source_sha256", "output_path"}
    valid = isinstance(identity, dict) and required <= identity.keys()
    if valid:
        valid = all(isinstance(identity[k], str) and identity[k].strip() for k in required - {"parties", "contract_number"})
        number = identity["contract_number"]
        valid = valid and (number is None or isinstance(number, str) and bool(number.strip()))
        parties = identity["parties"]
        valid = valid and isinstance(parties, list) and bool(parties) and all(isinstance(p, str) and p.strip() for p in parties)
        valid = valid and identity["reviewing_party"] in parties
        valid = valid and bool(re.fullmatch(r"[a-fA-F0-9]{64}", identity["source_sha256"]))
        valid = valid and all("\0" not in identity[k] and Path(identity[k]).is_absolute() for k in ("source_path", "output_path"))
    resolved_paths = {}
    if valid:
        try:
            resolved_paths = {key: Path(identity[key]).resolve() for key in ("source_path", "output_path")}
        except (OSError, ValueError, RuntimeError):
            valid = False
    if not valid:
        findings.append(_finding("identity.invalid", "error", "", "", "Identity record must bind number, title, parties, reviewing party, absolute source/output paths and source SHA-256."))
        return

    def mismatch(field: str, message: str) -> None:
        findings.append(_finding("identity." + field, "error", "", "", message))

    if baseline is None:
        findings.append(_finding("baseline.required", "error", "", "", "Matter-bound verification requires the named mother DOCX."))
    else:
        if resolved_paths["source_path"] != baseline.resolve():
            mismatch("source_path", "Selected mother differs from the locked source path.")
        if identity["source_sha256"].lower() != source_hash:
            mismatch("source_sha256", "Mother content has changed or differs from the intake fingerprint.")
    if resolved_paths["output_path"] != output.resolve():
        mismatch("output_path", "Candidate differs from the locked output path.")

    for prefix, roots in (("source_", source_parts), ("", parts)):
        if not roots:
            continue
        stories = [root for name, root in roots.items() if STORY_PART_PATTERN.fullmatch(name)]
        numbers = set().union(*(_number_values(root) for root in stories)) if stories else set()
        expected_numbers = {identity["contract_number"]} if identity["contract_number"] is not None else set()
        if numbers != expected_numbers:
            mismatch(prefix + "contract_number", "Current contract-number fields are absent, ambiguous, or do not equal the locked number.")

    for field, values in (("title", [identity["title"]]), ("parties", identity["parties"])):
        for value in values:
            anchors = []
            for name, root in sorted(source_parts.items()):
                if not STORY_PART_PATTERN.fullmatch(name):
                    continue
                for index, paragraph in enumerate(_view_elements(root, {f"{{{W}}}p"})):
                    marker = _field_marker(paragraph, field, value)
                    if marker is not None:
                        anchors.append((name, index, paragraph, marker))
            if not anchors:
                mismatch("source_" + field, "Locked identity cannot be located in a mother title or party declaration field.")
                continue
            for name, index, paragraph, marker in anchors:
                candidates = list(_view_elements(parts[name], {f"{{{W}}}p"})) if name in parts else []
                current = _matching_paragraph(paragraph, index, candidates)
                if current is None or _field_marker(current, field, value) != marker:
                    mismatch(field, "Identity at a mother-linked title or party declaration field has changed or cannot be verified.")


def _inspect_placeholders(root: ET.Element, part: str, baseline_root: ET.Element | None,
                          mode: str, findings: list[dict[str, str]], summary: dict,
                          inherited_elements: set[ET.Element]) -> None:
    old_paragraphs = list(_view_elements(baseline_root, {f"{{{W}}}p"})) if baseline_root is not None else []
    paragraphs = list(_view_elements(root, {f"{{{W}}}p"}))
    source_by_candidate: dict[ET.Element, ET.Element | None] = {}
    for index, source in enumerate(old_paragraphs):
        candidate = _matching_paragraph(source, index, paragraphs)
        if candidate is not None:
            # Anchor from the mother, not from IDs newly assigned on save.
            # Ambiguous many-to-one matches must not exempt a blank.
            source_by_candidate[candidate] = (
                None if candidate in source_by_candidate else source
            )
    new_paragraphs = {
        paragraph
        for insertion in _view_elements(root, {f"{{{W}}}ins", f"{{{W}}}moveTo"})
        if insertion not in inherited_elements
        for paragraph in insertion.iter(f"{{{W}}}p")
    }
    for index, paragraph in enumerate(paragraphs):
        text = _visible_text(paragraph)
        old = source_by_candidate.get(paragraph)
        old_text = _visible_text(old) if old is not None else ""
        equal_ranges = [(j, j + size) for _i, j, size in SequenceMatcher(None, old_text, text, autojunk=False).get_matching_blocks() if size]

        def unmodified_text(element: ET.Element, newly_inserted: bool = False) -> str:
            if element.tag in {f"{{{W}}}del", f"{{{W}}}moveFrom"}:
                return ""
            if element.tag in {f"{{{W}}}ins", f"{{{W}}}moveTo"} and element not in inherited_elements:
                newly_inserted = True
            if element.tag in {f"{{{W}}}t", f"{{{W}}}delText"}:
                value = element.text or ""
                return "\0" * len(value) if newly_inserted else value
            if element.tag in {f"{{{W}}}tab", f"{{{W}}}br", f"{{{W}}}cr"}:
                return " "
            result = "".join(unmodified_text(child, newly_inserted) for child in element)
            return result + ("\n" if element.tag == f"{{{W}}}p" else "")

        unchanged_view = unmodified_text(paragraph, paragraph in new_paragraphs)
        for pattern in PLACEHOLDER_PATTERNS:
            for match in pattern.finditer(text):
                preserved = (mode == "review" and bool(re.fullmatch(r"_{4,}", match.group()))
                             and unchanged_view[match.start():match.end()] == match.group()
                             and any(start <= match.start() and match.end() <= end for start, end in equal_ranges))
                summary["unresolved_placeholders"] += 1
                findings.append(_finding(
                    "placeholder.preserved_blank" if preserved else "placeholder.unresolved",
                    "warning" if preserved else "error", part, f"paragraph-{index + 1}",
                    "Unchanged source blank retained; assess its consequence before signing." if preserved else "Unresolved placeholder remains in current visible text."))


def _compact_text_length(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def _paragraph_revision_text(paragraph: ET.Element, inherited: set | None = None) -> tuple[str, str, str]:
    inserted: list[str] = []
    deleted: list[str] = []
    unchanged: list[str] = []

    def visit(element: ET.Element, revision_kind: str | None = None) -> None:
        if inherited and element in inherited:
            if element.tag in {f"{{{W}}}del", f"{{{W}}}moveFrom"}:
                return
            revision_kind = None
        elif element.tag == f"{{{W}}}ins":
            revision_kind = "inserted"
        elif element.tag == f"{{{W}}}del":
            revision_kind = "deleted"
        if element.tag in {f"{{{W}}}t", f"{{{W}}}delText"}:
            target = (
                inserted
                if revision_kind == "inserted"
                else deleted
                if revision_kind == "deleted"
                else unchanged
            )
            target.append(element.text or "")
        for child in element:
            visit(child, revision_kind)

    visit(paragraph)
    return "".join(inserted), "".join(deleted), "".join(unchanged)


def _inspect_precision_redlines(
    document: ET.Element,
    findings: list[dict[str, str]],
    inherited: set | None = None,
    part: str = DOCUMENT_PART,
) -> int:
    block_replacements = 0
    for index, paragraph in enumerate(document.findall(f".//{{{W}}}p"), start=1):
        inserted, deleted, unchanged = _paragraph_revision_text(paragraph, inherited)
        inserted_len = _compact_text_length(inserted)
        deleted_len = _compact_text_length(deleted)
        unchanged_len = _compact_text_length(unchanged)
        shorter_change = min(inserted_len, deleted_len)
        if shorter_change < BLOCK_REPLACEMENT_MIN_CHARS:
            continue
        unchanged_ratio = unchanged_len / shorter_change if shorter_change else 1.0
        if (
            unchanged_len > BLOCK_REPLACEMENT_MAX_UNCHANGED_CHARS
            or unchanged_ratio > BLOCK_REPLACEMENT_MAX_UNCHANGED_RATIO
        ):
            continue
        block_replacements += 1
        paragraph_id = (
            paragraph.get(f"{{{W14}}}paraId")
            or paragraph.get(f"{{{W}}}paraId")
            or f"paragraph-{index}"
        )
        findings.append(
            _finding(
                "revision.block_replacement",
                "error",
                part,
                paragraph_id,
                "Long text was deleted and reinserted as a near-total paragraph "
                "replacement. Preserve unchanged text and track only the minimum "
                "necessary words or sentences.",
            )
        )
    return block_replacements


def _inspect_relationships(
    archive: zipfile.ZipFile,
    names: set[str],
    findings: list[dict[str, str]],
    has_comments: bool,
) -> None:
    if RELS_PART not in names:
        if has_comments:
            findings.append(
                _finding(
                    "comments.relationship_missing",
                    "error",
                    RELS_PART,
                    "",
                    "comments.xml exists but document relationships are missing.",
                )
            )
        return
    root = _parse_part(archive, RELS_PART, findings)
    if root is None:
        return
    comments_relationship = False
    comments_relationship_count = 0
    base = PurePosixPath(DOCUMENT_PART).parent
    for relationship in root.findall(f"{{{PR}}}Relationship"):
        relationship_id = relationship.get("Id", "")
        target = relationship.get("Target", "")
        is_comments = relationship.get("Type") == COMMENTS_REL
        if is_comments:
            comments_relationship_count += 1
            if relationship.get("TargetMode") not in (None, "Internal") or not target:
                findings.append(_finding("comments.relationship_invalid", "error", RELS_PART,
                                         relationship_id, "Comments must link to an internal comments.xml part."))
                continue
        if relationship.get("TargetMode") == "External" or not target:
            continue
        target_path = (PurePosixPath(target.lstrip("/")) if target.startswith("/")
                       else base / PurePosixPath(target)).as_posix()
        normalized_parts: list[str] = []
        for part in PurePosixPath(target_path).parts:
            if part == "..":
                if normalized_parts and normalized_parts[-1] != "..":
                    normalized_parts.pop()
                else:
                    normalized_parts.append(part)
            elif part != ".":
                normalized_parts.append(part)
        normalized = "/".join(normalized_parts)
        if is_comments:
            if normalized == COMMENTS_PART and normalized in names:
                comments_relationship = True
            else:
                findings.append(_finding("comments.relationship_invalid", "error", RELS_PART,
                                         relationship_id, "Comments relationship does not target word/comments.xml."))
        if normalized not in names:
            findings.append(
                _finding(
                    "relationship.target_missing",
                    "error",
                    RELS_PART,
                    relationship_id,
                    f"Relationship target is absent from the package: {target}",
                )
            )
    if comments_relationship_count > 1:
        findings.append(_finding("comments.relationship_invalid", "error", RELS_PART, "",
                                 "Document has more than one comments relationship."))
    if has_comments and not comments_relationship:
        findings.append(
            _finding(
                "comments.relationship_missing",
                "error",
                RELS_PART,
                "",
                "comments.xml is not linked from document.xml.",
            )
        )


def inspect_docx(path: Path, expected_author: str = "YKX", *,
                 baseline: Path | None = None, identity: Any = None,
                 mode: str = "review", require_identity: bool = False) -> dict[str, Any]:
    docx_path = Path(path)
    findings: list[dict[str, str]] = []
    summary = {
        "errors": 0,
        "warnings": 0,
        "tracked_changes": 0,
        "comments": 0,
        "unresolved_placeholders": 0,
        "block_replacements": 0,
    }
    report: dict[str, Any] = {
        "schema_version": 2,
        "path": str(docx_path),
        "expected_author": expected_author,
        "mode": mode,
        "identity_gate": "fail" if identity is not None or require_identity else "not_checked",
        "output_sha256": "",
        "summary": summary,
        "findings": findings,
    }
    if mode not in {"review", "finalization"}:
        findings.append(_finding("mode.invalid", "error", "", "", "Mode must be review or finalization."))
    source_parts: dict[str, ET.Element] = {}
    source_hash = ""
    if baseline is not None:
        baseline = Path(baseline)
        if baseline.resolve() == docx_path.resolve() or (baseline.is_file() and docx_path.is_file() and baseline.samefile(docx_path)):
            findings.append(_finding("baseline.same_file", "error", "", "", "Output must be a different file from the read-only mother."))
        source_parts, source_hash = _read_baseline(baseline, findings)
    report["source_sha256"] = source_hash
    inherited_pool = Counter((part, _signature(element)) for part, root in source_parts.items()
                             for element in root.iter() if element.tag in TRACKED_TAGS | {f"{{{W}}}comment"})

    def is_inherited(part: str, element: ET.Element) -> bool:
        key = (part, _signature(element))
        if inherited_pool[key] > 0:
            inherited_pool[key] -= 1
            return True
        return False

    def finish() -> dict[str, Any]:
        findings.sort(key=lambda item: (item["part"], item["element_id"], item["code"]))
        summary["errors"] = sum(item["severity"] == "error" for item in findings)
        summary["warnings"] = sum(item["severity"] == "warning" for item in findings)
        if identity is not None or require_identity:
            report["identity_gate"] = "pass" if not summary["errors"] else "fail"
        return report

    if require_identity and identity is None:
        findings.append(_finding("identity.required", "error", "", "", "Delivery requires an intake-locked identity record."))
    if not docx_path.is_file():
        findings.append(
            _finding(
                "docx.missing", "error", "", "", "DOCX file does not exist."
            )
        )
        return finish()
    try:
        payload = docx_path.read_bytes()
        report["output_sha256"] = hashlib.sha256(payload).hexdigest()
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (OSError, zipfile.BadZipFile):
        findings.append(
            _finding(
                "docx.invalid_zip",
                "error",
                "",
                "",
                "DOCX is not a readable ZIP package.",
            )
        )
        return finish()

    with archive:
        names = set(archive.namelist())
        document = _parse_part(archive, DOCUMENT_PART, findings)
        if document is None:
            return finish()
        parts = {DOCUMENT_PART: document}
        for name in sorted(names - {DOCUMENT_PART}):
            if STORY_PART_PATTERN.fullmatch(name):
                root = _parse_part(archive, name, findings)
                if root is not None:
                    parts[name] = root
        if identity is not None:
            _identity_checks(identity, baseline, source_parts, source_hash, docx_path, parts, findings)
        comments = (
            _parse_part(archive, COMMENTS_PART, findings)
            if COMMENTS_PART in names
            else None
        )
        comment_ids: set[str] = set()
        if comments is not None:
            for comment in comments.findall(f".//{{{W}}}comment"):
                comment_id = comment.get(f"{{{W}}}id", "")
                if comment_id in comment_ids:
                    findings.append(_finding("comments.id_duplicate", "error", COMMENTS_PART,
                                             comment_id, "Comment IDs must be unique."))
                comment_ids.add(comment_id)
                summary["comments"] += 1
                author = comment.get(f"{{{W}}}author", "")
                if not is_inherited(COMMENTS_PART, comment) and author != expected_author:
                    findings.append(
                        _finding(
                            "comment.author",
                            "error",
                            COMMENTS_PART,
                            comment_id,
                            f"Comment author must be {expected_author}.",
                        )
                    )

        reference_ids: set[str] = set()
        for part, root in parts.items():
            inherited_elements: set[ET.Element] = set()
            for element in root.iter():
                if element.tag in TRACKED_TAGS:
                    summary["tracked_changes"] += 1
                    if is_inherited(part, element):
                        inherited_elements.add(element)
                    elif element.get(f"{{{W}}}author", "") != expected_author:
                        findings.append(_finding("revision.author", "error", part, element.get(f"{{{W}}}id", ""), f"New or changed tracked-change author must be {expected_author}."))
                if element.tag in COMMENT_REFERENCE_TAGS:
                    reference_ids.add(element.get(f"{{{W}}}id", ""))
            old_root = source_parts.get(part)
            before_numbers = _number_values(old_root) if old_root is not None else _number_values(root, original=True)
            if _number_values(root) != before_numbers:
                findings.append(_finding("contract_number.edited", "warning", part, "", "Tracked changes modify a contract-number field, including its value."))
            summary["block_replacements"] += _inspect_precision_redlines(root, findings, inherited_elements, part)
            _inspect_placeholders(root, part, source_parts.get(part), mode, findings, summary, inherited_elements)

        if source_parts and any(inherited_pool.values()):
            findings.append(_finding("baseline.history_changed", "warning", "", "", "Some inherited revisions/comments were removed or changed; reconcile with the user's current authorization."))

        for reference_id in sorted(reference_ids - comment_ids):
            findings.append(
                _finding(
                    "comment.reference_missing",
                    "error",
                    DOCUMENT_PART,
                    reference_id,
                    "Document references a comment that is absent.",
                )
            )
        for comment_id in sorted(comment_ids - reference_ids):
            findings.append(
                _finding(
                    "comment.unreferenced",
                    "warning",
                    COMMENTS_PART,
                    comment_id,
                    "Comment exists but has no document reference.",
                )
            )

        _inspect_relationships(
            archive,
            names,
            findings,
            has_comments=COMMENTS_PART in names,
        )

    return finish()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Inspect contract-review DOCX OOXML without modifying it."
    )
    parser.add_argument("docx", type=Path)
    parser.add_argument("--expected-author", default="YKX")
    parser.add_argument("--baseline", type=Path, help="Read-only current mother DOCX.")
    parser.add_argument("--identity", type=Path, help="Intake-locked matter identity JSON.")
    parser.add_argument("--require-identity", action="store_true", help="Fail unless matter identity is checked.")
    parser.add_argument("--mode", choices=("review", "finalization"), default="review")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    identity = None
    if args.identity is not None:
        try:
            identity = json.loads(args.identity.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            identity = {}  # Invalid input must fail closed, still as a JSON report.
        if identity is None:
            identity = {}
    report = inspect_docx(args.docx, expected_author=args.expected_author,
                          baseline=args.baseline, identity=identity, mode=args.mode,
                          require_identity=args.require_identity or args.identity is not None)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for finding in report["findings"]:
            print(
                f"{finding['severity']} {finding['part']} "
                f"{finding['element_id']} {finding['code']}: "
                f"{finding['message']}"
            )
        print(json.dumps(report["summary"], ensure_ascii=False))
    return 1 if report["summary"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
