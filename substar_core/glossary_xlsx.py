from __future__ import annotations

from io import BytesIO
from re import match
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
import zipfile


EXCEL_FIELDS = (
    "source",
    "standard_source",
    "target",
    "aliases",
    "type",
    "scope",
    "project",
    "case_sensitive",
    "do_not_translate",
    "enabled",
    "hotword_weight",
    "notes",
)
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _column_name(index: int) -> str:
    value = index + 1
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _cell_text(value: Any, field: str) -> str:
    if field == "aliases" and isinstance(value, list):
        return ";".join(str(item) for item in value)
    if field in {"case_sensitive", "do_not_translate", "enabled"} and isinstance(value, (bool, int, float)):
        return "true" if bool(value) else "false"
    return "" if value is None else str(value)


def _inline_cell(reference: str, value: Any, field: str, style: int = 0) -> str:
    text = escape(_cell_text(value, field))
    style_attribute = f' s="{style}"' if style else ""
    return (
        f'<c r="{reference}" t="inlineStr"{style_attribute}>'
        f'<is><t xml:space="preserve">{text}</t></is></c>'
    )


def glossary_xlsx_bytes(entries: list[dict[str, Any]]) -> bytes:
    rows: list[str] = []
    header_cells = "".join(
        _inline_cell(f"{_column_name(index)}1", field, field, style=1)
        for index, field in enumerate(EXCEL_FIELDS)
    )
    rows.append(f'<row r="1">{header_cells}</row>')
    for row_index, entry in enumerate(entries, start=2):
        cells = "".join(
            _inline_cell(
                f"{_column_name(column_index)}{row_index}",
                entry.get(field, ""),
                field,
            )
            for column_index, field in enumerate(EXCEL_FIELDS)
        )
        rows.append(f'<row r="{row_index}">{cells}</row>')
    last_column = _column_name(len(EXCEL_FIELDS) - 1)
    widths = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate((24, 24, 24, 28, 16, 12, 24, 14, 16, 12, 16, 34), start=1)
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<cols>{widths}</cols>"
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
        'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        f'<autoFilter ref="A1:{last_column}1"/>'
        f'<sheetData>{"".join(rows)}</sheetData></worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="热词表" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )
    workbook_xml_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>'
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" applyFont="1"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '<dxfs count="0"/><tableStyles count="0" defaultTableStyle="TableStyleMedium2" '
        'defaultPivotStyle="PivotStyleMedium9"/>'
        '</styleSheet>'
    )
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", workbook_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_xml_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        archive.writestr("xl/styles.xml", styles)
    return output.getvalue()


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except (KeyError, ET.ParseError):
        return []
    return ["".join(node.text or "" for node in item.iter(_NS + "t")) for item in root.iter(_NS + "si")]


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(_NS + "t"))
    value = cell.find(_NS + "v")
    raw = "" if value is None or value.text is None else value.text
    if cell_type == "s":
        try:
            return shared[int(raw)]
        except (IndexError, ValueError):
            return ""
    return raw


def _to_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "是", "启用"}


def parse_glossary_xlsx(data: bytes) -> list[dict[str, Any]]:
    try:
        archive = zipfile.ZipFile(BytesIO(data))
        sheet_data = archive.read("xl/worksheets/sheet1.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError("热词表必须是有效的 .xlsx 文件") from exc
    shared = _shared_strings(archive)
    try:
        root = ET.fromstring(sheet_data)
    except ET.ParseError as exc:
        raise ValueError("热词表工作表 XML 无法解析") from exc
    rows: list[list[str]] = []
    for row in root.iter(_NS + "row"):
        values = [""] * len(EXCEL_FIELDS)
        for cell in row.findall(_NS + "c"):
            reference = cell.attrib.get("r", "")
            column = match(r"([A-Z]+)", reference)
            if not column:
                continue
            index = 0
            for character in column.group(1):
                index = index * 26 + ord(character) - 64
            index -= 1
            if 0 <= index < len(values):
                values[index] = _cell_value(cell, shared)
        rows.append(values)
    if not rows:
        return []
    header = [value.strip() for value in rows[0]]
    indexes = {name: header.index(name) for name in EXCEL_FIELDS if name in header}
    if "source" not in indexes:
        raise ValueError("热词表缺少 source 列")
    result: list[dict[str, Any]] = []
    for row in rows[1:]:
        entry: dict[str, Any] = {}
        for field, index in indexes.items():
            entry[field] = row[index] if index < len(row) else ""
        entry["aliases"] = [item.strip() for item in entry.get("aliases", "").replace("，", ";").split(";") if item.strip()]
        for field in ("case_sensitive", "do_not_translate", "enabled"):
            if field in entry:
                entry[field] = _to_bool(entry[field])
        if "hotword_weight" in entry:
            try:
                entry["hotword_weight"] = int(float(entry["hotword_weight"]))
            except (TypeError, ValueError):
                entry["hotword_weight"] = 4
        if any(str(value).strip() for value in row):
            result.append(entry)
    return result
