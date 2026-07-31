from io import BytesIO

import pytest
from openpyxl import Workbook

from gateway.content_import import parse_copy_import


def xlsx_bytes(rows):
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output


@pytest.mark.parametrize(
    ("filename", "content", "expected_brand"),
    [
        (
            "copy.csv",
            "品牌名,文案,tag\n春日茶饮,夏日新品,#新品 #饮品\n".encode("utf-8-sig"),
            "春日茶饮",
        ),
        (
            "copy.tsv",
            "brand\tbody\ttags\nCity Coffee\tNew menu\t#coffee\n".encode(),
            "City Coffee",
        ),
    ],
)
def test_parse_copy_import_supports_delimited_files(
    filename,
    content,
    expected_brand,
):
    result = parse_copy_import(filename, BytesIO(content))

    assert result["total"] == 1
    assert result["errors"] == []
    assert result["rows"][0]["row"] == 2
    assert result["rows"][0]["brand_name"] == expected_brand


def test_parse_copy_import_supports_xlsx_and_reports_invalid_rows():
    source = xlsx_bytes(
        [
            ["品牌", "正文", "标签"],
            ["春日茶饮", "第一条", "#新品"],
            ["春日茶饮", "", "#空文案"],
            ["", "缺少品牌", "#错误"],
        ]
    )

    result = parse_copy_import("copy.xlsx", source)

    assert result["total"] == 3
    assert result["rows"] == [
        {
            "row": 2,
            "brand_name": "春日茶饮",
            "body": "第一条",
            "tags": "#新品",
        }
    ]
    assert result["errors"] == [
        {"row": 3, "error": "缺少文案"},
        {"row": 4, "error": "缺少品牌名"},
    ]


@pytest.mark.parametrize("filename", ["copy.txt", "copy.xls"])
def test_parse_copy_import_rejects_unsupported_files(filename):
    with pytest.raises(ValueError, match="仅支持"):
        parse_copy_import(filename, BytesIO(b"data"))


def test_parse_copy_import_rejects_missing_required_headers():
    source = BytesIO("文案,tag\n正文,#tag\n".encode())

    with pytest.raises(ValueError, match="缺少必要表头"):
        parse_copy_import("copy.csv", source)


def test_parse_copy_import_rejects_empty_or_corrupt_files():
    with pytest.raises(ValueError, match="导入文件为空"):
        parse_copy_import("copy.csv", BytesIO(b""))

    with pytest.raises(ValueError, match="Excel 文件无法读取"):
        parse_copy_import("copy.xlsx", BytesIO(b"not-an-xlsx"))
