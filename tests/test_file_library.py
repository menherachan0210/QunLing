"""Synthetic attachment and artifact tests; no QQ, network or model calls."""

import io
import json
import os
import shutil
import stat
import subprocess
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from qunling.files import (
    FileError, FileLimits, export_file, inspect_file, read_archive_member,
    read_file, search_file,
)
from qunling.files.archives import validate_native_listing


def write_zip(path, members):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in members:
            archive.writestr(name, body)
    return path


def test_text_paging_and_search_preserve_content(tmp_path):
    path = tmp_path / "source.py"
    body = "print('只读取不执行')\n" * 600
    path.write_bytes(body.encode("utf-8"))
    assert inspect_file(path)["format"] == "source"
    collected, selector = [], {}
    while True:
        result = read_file(path, selector)
        collected.extend(section["text"] for section in result["sections"])
        selector = result["next"]
        if selector is None:
            break
    assert "".join(collected) == body
    hits = search_file(path, "不执行", limit=2)
    assert len(hits["hits"]) == 2 and hits["truncated"]


@pytest.mark.parametrize("format,payload", [
    ("txt", {"text": '{"这是": "文本，不改为JSON导出"}'}),
    ("md", {"text": "# 测试\n正文"}),
    ("json", {"data": {"标题": "附件", "values": [1, True, None]}}),
    ("csv", {"rows": [["名称", "值"], ["a,b", 12], ["=HYPERLINK(\"https://invalid\")", 0]]}),
])
def test_plain_exports_and_no_overwrite(tmp_path, format, payload):
    output = tmp_path / f"output.{format}"
    artifact = export_file(format, payload, output)
    assert artifact["size"] == output.stat().st_size and len(artifact["sha256"]) == 64
    assert read_file(output)["sections"]
    if format == "csv":
        assert read_file(output)["sections"][0]["rows"][2]["cells"][0].startswith("'=")
        assert artifact["warnings"]
    with pytest.raises(FileError, match="已存在") as error:
        export_file(format, payload, output)
    assert error.value.code == "output_exists"


def test_csv_row_and_cell_navigation(tmp_path):
    path = tmp_path / "table.csv"
    export_file("csv", {"rows": [[f"行{i}", i] for i in range(7)]}, path)
    first = read_file(path, {"limit": 3})
    second = read_file(path, first["next"])
    assert [r["row"] for r in first["sections"][0]["rows"]] == [1, 2, 3]
    assert [r["row"] for r in second["sections"][0]["rows"]] == [4, 5, 6]
    cell = read_file(path, {"section": "table:1", "cell": "R2C1"})
    assert json.loads(cell["sections"][0]["text"]) == "行1"


def test_mixed_archive_lists_and_reads_exact_members(tmp_path):
    path = tmp_path / "mixed.zip"
    export_file("zip", {"files": [
        {"name": "notes/说明.md", "text": "# 测试"},
        {"name": "tables/data.csv", "text": "a,b\n1,2\n"},
        {"name": "program.py", "text": "raise RuntimeError('must not execute')"},
    ]}, path)
    manifest = inspect_file(path)
    assert manifest["format"] == "zip" and manifest["structure"][0]["members"] == 3
    listing = read_file(path)["sections"][0]["entries"]
    assert {entry["path"] for entry in listing} == {"notes/说明.md", "tables/data.csv", "program.py"}
    assert read_archive_member(path, "program.py").startswith(b"raise RuntimeError")
    assert read_file(path, {"member": "tables/data.csv"})["sections"][0]["rows"][1]["cells"] == ["1", "2"]
    assert search_file(path, "说明")["hits"][0]["location"]["member"] == "notes/说明.md"


@pytest.mark.parametrize("name", ["../out", "/abs", "C:/abs", "a\\..\\out", "nul.txt", "a/./b"])
def test_archive_traversal_refused(tmp_path, name):
    path = write_zip(tmp_path / "unsafe.zip", [(name, b"test")])
    with pytest.raises(FileError) as error:
        inspect_file(path)
    assert error.value.code == "unsafe_member"
    assert not (tmp_path / "out").exists()


def test_zip_symlinks_collisions_and_expansion_rejected(tmp_path):
    path = tmp_path / "link.zip"
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "target")
    with pytest.raises(FileError) as error:
        inspect_file(path)
    assert error.value.code == "unsafe_member"
    path = write_zip(tmp_path / "case.zip", [("A.txt", "a"), ("a.txt", "b")])
    with pytest.raises(FileError) as error:
        inspect_file(path)
    assert error.value.code == "ambiguous_member"
    path = write_zip(tmp_path / "large.zip", [("data.txt", b"x" * 4096)])
    with pytest.raises(FileError) as error:
        inspect_file(path, limits=replace(FileLimits(), max_member_bytes=1024))
    assert error.value.code == "member_limit"


def test_office_xml_entities_refused_before_parser(tmp_path):
    path = write_zip(tmp_path / "bomb.docx", [
        ("[Content_Types].xml", '<?xml version="1.0"?><Types/>'),
        ("word/document.xml", '<!DOCTYPE x [<!ENTITY a "content">]><x>&a;</x>'),
    ])
    with pytest.raises(FileError) as error:
        inspect_file(path)
    assert error.value.code == "unsafe_xml"


def test_office_roundtrip_body_table_sheet_slide(tmp_path):
    pytest.importorskip("docx")
    pytest.importorskip("pptx")
    pytest.importorskip("openpyxl")
    doc = tmp_path / "report.docx"
    export_file("docx", {"blocks": [{"heading": "报告", "level": 1},
                                   {"text": "正文说明"}, {"table": [["姓名", "数量"], ["测试", 3]]}]}, doc)
    assert inspect_file(doc)["format"] == "docx"
    assert search_file(doc, "测试")["hits"][0]["location"]["cell"] == "R2C1"
    # A filename does not determine real content type.
    disguised = tmp_path / "image.png"
    disguised.write_bytes(doc.read_bytes())
    assert inspect_file(disguised)["format"] == "docx"
    assert "extension_content_mismatch" in inspect_file(disguised)["warnings"]
    workbook = tmp_path / "table.xlsx"
    export_file("xlsx", {"sheets": [{"name": "数据", "rows": [["标题", "数值"], ["=1+1", 3]]}]}, workbook)
    result = read_file(workbook, {"sheet": "数据"})
    assert result["sections"][0]["rows"][1]["cells"][0] == {"value": "=1+1"}
    slides = tmp_path / "deck.pptx"
    export_file("pptx", {"slides": [{"title": "标题", "text": "第一行\n第二行"}]}, slides)
    assert "第二行" in read_file(slides, {"page": 1})["sections"][0]["text"]


def test_xlsx_formula_and_missing_cache_are_distinct(tmp_path):
    library = pytest.importorskip("openpyxl")
    path = tmp_path / "formula.xlsx"
    book = library.Workbook()
    book.active["A1"] = 2
    book.active["B1"] = "=A1+3"
    book.save(path)
    book.close()
    result = read_file(path)
    formula = result["sections"][0]["rows"][0]["cells"][1]
    assert formula == {"formula": "=A1+3", "cached_value": None, "cache_available": False}
    assert "formulas_not_evaluated_cached_values_may_be_stale" in result["warnings"]


def test_long_docx_page_and_large_cell_have_continuations(tmp_path):
    pytest.importorskip("docx")
    pytest.importorskip("openpyxl")
    body = "含引号\"和反斜杠\\的长文本\n" * 1500
    path = tmp_path / "long.docx"
    export_file("docx", {"blocks": [{"text": body}, {"text": "尾段"}]}, path)
    result, collected = read_file(path), []
    while True:
        collected.extend(item.get("text", "") for item in result["sections"])
        if not result["next"]:
            break
        result = read_file(path, result["next"])
    assert "".join(collected) == body + "尾段"
    cell_text = "x" * 30000
    book = tmp_path / "large-cell.xlsx"
    export_file("xlsx", {"sheets": [{"name": "数据", "rows": [[cell_text]]}]}, book)
    with pytest.raises(FileError) as error:
        read_file(book)
    assert error.value.code == "result_limit"
    result = read_file(book, {"sheet": "数据", "cell": "R1C1"})
    pieces = []
    while True:
        pieces.append(result["sections"][0]["text"])
        if not result["next"]:
            break
        result = read_file(book, result["next"])
    assert json.loads("".join(pieces)) == {"value": cell_text}


def test_pdf_text_and_empty_page_marked_for_ocr(tmp_path):
    library = pytest.importorskip("pypdf")
    from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject
    writer = library.PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 10 700 Td (Synthetic text) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    writer.add_blank_page(width=600, height=800)
    path = tmp_path / "document.pdf"
    writer.write(path)
    manifest = inspect_file(path)
    assert len(manifest["structure"]) == 2 and manifest["needs_ocr"]
    assert "Synthetic text" in read_file(path, {"page": 1})["sections"][0]["text"]
    assert read_file(path, {"page": 2})["needs_ocr"]


def test_budget_and_schema_errors_are_explicit(tmp_path):
    path = tmp_path / "large.txt"
    path.write_bytes(b"a" * 2000)
    with pytest.raises(FileError) as error:
        read_file(path, limits=replace(FileLimits(), max_text_chars=100))
    assert error.value.code == "text_limit"
    with pytest.raises(FileError):
        read_file(path, {"command": "execute"})
    with pytest.raises(FileError):
        export_file("zip", {"files": [{"name": "../out", "text": "x"}]}, tmp_path / "out.zip")
    with pytest.raises(FileError):
        export_file("xlsx", {"sheets": [{"name": "data", "rows": [[{"formula": "=1+1"}]]}]},
                    tmp_path / "out.xlsx")
    assert not (tmp_path / "out.xlsx").exists()


def test_native_listing_rejects_link_and_accepts_mixed_paths():
    prefix = "--\nPath = archive\nType = 7z\nPhysical Size = 100\n\n----------\n"
    entry = "Path = documents/a.txt\nSize = 3\nPacked Size = 3\nFolder = -\nEncrypted = -\n"
    result = validate_native_listing((prefix + entry).encode(), "7z", 100, FileLimits())
    assert result[0]["path"] == "documents/a.txt"
    with pytest.raises(FileError) as error:
        validate_native_listing((prefix + entry + "Symbolic Link = target\n").encode(), "7z", 100, FileLimits())
    assert error.value.code == "unsafe_member"


def test_real_7z_mixed_archive_when_decoder_available(tmp_path):
    configured = os.environ.get("QUNLING_TEST_7ZIP")
    decoder = configured or shutil.which("7zz") or shutil.which("7z")
    if not decoder:
        pytest.skip("Optional native 7-Zip executable is not installed")
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("原生解压测试", encoding="utf-8")
    (source / "b.csv").write_text("a,b\n1,2", encoding="utf-8")
    path = tmp_path / "mixed.7z"
    subprocess.run([decoder, "a", "-t7z", str(path), "a.txt", "b.csv"],
                   cwd=source, check=True, capture_output=True, timeout=20)
    limits = replace(FileLimits(), seven_zip_path=str(Path(decoder).resolve()))
    manifest = inspect_file(path, limits=limits)
    assert manifest["format"] == "7z" and manifest["structure"][0]["members"] == 2
    assert read_archive_member(path, "a.txt", limits=limits).decode() == "原生解压测试"


@pytest.mark.parametrize("filename,code", [
    ("synthetic-comic-rar4.rar", None),
    ("test_read_format_rar.rar", "unsafe_member"),
    ("test_read_format_rar5_stored.rar", None),
    ("test_read_format_rar5_symlink.rar", "unsafe_member"),
    ("test_read_format_rar5_hardlink.rar", "unsafe_member"),
    ("test_read_format_rar5_encrypted.rar", "encrypted_file"),
])
def test_optional_native_rar_fixtures(filename, code):
    directory = os.environ.get("QUNLING_TEST_ARCHIVES")
    decoder = os.environ.get("QUNLING_TEST_7ZIP")
    if not directory or not decoder:
        pytest.skip("Optional native decoder fixtures not installed")
    path = Path(directory) / filename
    if not path.is_file():
        pytest.skip("Optional synthetic archive fixture not installed")
    limits = replace(FileLimits(), seven_zip_path=str(Path(decoder).resolve()))
    if code:
        with pytest.raises(FileError) as error:
            inspect_file(path, limits=limits)
        assert error.value.code == code
    else:
        result = read_file(path, limits=limits)
        entry = next(item for item in result["sections"][0]["entries"] if not item["directory"])
        assert len(read_archive_member(path, entry["path"], limits=limits)) == entry["size"]
