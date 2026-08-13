"""Shared test helper: build a small, genuinely valid PDF with real
extractable text, without an external fixture file or a reportlab dependency.
"""

from __future__ import annotations

import io

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def make_pdf_bytes(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)

    content = f"BT /F1 12 Tf 20 250 Td ({text}) Tj ET".encode()
    stream_obj = DecodedStreamObject()
    stream_obj.set_data(content)
    stream_ref = writer._add_object(stream_obj)  # pyright: ignore[reportPrivateUsage]

    font_dict = DictionaryObject()
    font_dict[NameObject("/Type")] = NameObject("/Font")
    font_dict[NameObject("/Subtype")] = NameObject("/Type1")
    font_dict[NameObject("/BaseFont")] = NameObject("/Helvetica")
    font_ref = writer._add_object(font_dict)  # pyright: ignore[reportPrivateUsage]

    resources = DictionaryObject()
    font_res = DictionaryObject()
    font_res[NameObject("/F1")] = font_ref
    resources[NameObject("/Font")] = font_res

    page[NameObject("/Contents")] = stream_ref
    page[NameObject("/Resources")] = resources

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
