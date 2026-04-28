from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile


def _xml_escape(value: str):
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _sanitize_text(value):
    text = str(value if value is not None else "")
    cleaned = []
    for char in text:
        code = ord(char)
        if code in (0x9, 0xA, 0xD) or code >= 0x20:
            cleaned.append(char)
    return "".join(cleaned)


def _column_name(index: int):
    name = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _sheet_xml(rows):
    row_chunks = []
    for row_index, row in enumerate(rows, start=1):
        cell_chunks = []
        for col_index, value in enumerate(row, start=1):
            cell_ref = f"{_column_name(col_index)}{row_index}"
            text = _sanitize_text(value)
            preserve = ' xml:space="preserve"' if text[:1].isspace() or text[-1:].isspace() or "\n" in text else ""
            cell_chunks.append(
                f'<c r="{cell_ref}" t="inlineStr"><is><t{preserve}>{_xml_escape(text)}</t></is></c>'
            )
        row_chunks.append(f'<row r="{row_index}">{"".join(cell_chunks)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(row_chunks)}</sheetData>'
        "</worksheet>"
    )


def _content_types_xml(sheet_count: int):
    overrides = [
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>',
    ]
    for index in range(1, sheet_count + 1):
        overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'{"".join(overrides)}'
        "</Types>"
    )


def _root_rels_xml():
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        "</Relationships>"
    )


def _workbook_xml(sheet_names):
    sheets = []
    for index, name in enumerate(sheet_names, start=1):
        sheets.append(
            f'<sheet name="{_xml_escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets>{"".join(sheets)}</sheets>'
        "</workbook>"
    )


def _workbook_rels_xml(sheet_count: int):
    relationships = []
    for index in range(1, sheet_count + 1):
        relationships.append(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
    relationships.append(
        f'<Relationship Id="rId{sheet_count + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'{"".join(relationships)}'
        "</Relationships>"
    )


def _styles_xml():
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )


def _core_xml(title: str):
    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f'<dc:title>{_xml_escape(title)}</dc:title>'
        '<dc:creator>POD Sync Console</dc:creator>'
        '<cp:lastModifiedBy>POD Sync Console</cp:lastModifiedBy>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{created_at}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{created_at}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def _app_xml(sheet_names):
    titles = "".join(f"<vt:lpstr>{_xml_escape(name)}</vt:lpstr>" for name in sheet_names)
    count = len(sheet_names)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        '<Application>POD Sync Console</Application>'
        f'<TitlesOfParts><vt:vector size="{count}" baseType="lpstr">{titles}</vt:vector></TitlesOfParts>'
        "</Properties>"
    )


def build_xlsx_workbook(sheet_specs, *, title="Workbook"):
    normalized_specs = []
    used_names = set()
    for index, spec in enumerate(sheet_specs, start=1):
        name = (spec.get("name") or f"Sheet{index}")[:31]
        if not name:
            name = f"Sheet{index}"
        while name in used_names:
            name = f"{name[:28]}_{index}"[:31]
        used_names.add(name)
        rows = spec.get("rows") or []
        normalized_specs.append({"name": name, "rows": rows})

    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types_xml(len(normalized_specs)))
        archive.writestr("_rels/.rels", _root_rels_xml())
        archive.writestr("xl/workbook.xml", _workbook_xml([item["name"] for item in normalized_specs]))
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_rels_xml(len(normalized_specs)))
        archive.writestr("xl/styles.xml", _styles_xml())
        archive.writestr("docProps/core.xml", _core_xml(title))
        archive.writestr("docProps/app.xml", _app_xml([item["name"] for item in normalized_specs]))
        for index, spec in enumerate(normalized_specs, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _sheet_xml(spec["rows"]))
    return buffer.getvalue()
