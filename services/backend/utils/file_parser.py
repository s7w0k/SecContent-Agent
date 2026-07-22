"""Parse supported user-uploaded documents into Markdown text."""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

from docx import Document
from pypdf import PdfReader

ALLOWED_EXTENSIONS = frozenset({".txt", ".md", ".pdf", ".docx"})
MAX_FILE_SIZE = 10 * 1024 * 1024


class FileParseError(Exception):
    """Raised when a supported document cannot be parsed safely."""


def parse(filename: str, content: bytes) -> str:
    """Dispatch document parsing by the lower-cased filename extension."""
    extension = Path(filename).suffix.lower()
    if extension in {".txt", ".md"}:
        return _parse_text(content)
    if extension == ".pdf":
        return _parse_pdf(content)
    if extension == ".docx":
        return _parse_docx(content)
    raise FileParseError(f"不支持的文件类型: {extension or '无扩展名'}")


def _parse_text(content: bytes) -> str:
    """Decode UTF-8 and common simplified-Chinese text encodings."""
    for encoding in ("utf-8-sig", "gbk", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise FileParseError("无法识别文件编码")


def _parse_pdf(content: bytes) -> str:
    """Extract text from each unencrypted PDF page."""
    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            raise FileParseError("不支持加密 PDF")
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(page.strip() for page in pages if page.strip())
    except FileParseError:
        raise
    except Exception as exc:
        raise FileParseError(f"PDF 解析失败: {exc}") from exc


def _parse_docx(content: bytes) -> str:
    """Extract DOCX paragraphs and convert Heading 1-6 styles to Markdown."""
    try:
        document = Document(BytesIO(content))
        parts: list[str] = []
        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            style_name = getattr(paragraph.style, "name", "") or ""
            heading = re.fullmatch(r"Heading\s+([1-6])", style_name, flags=re.IGNORECASE)
            if heading:
                parts.append(f"{'#' * int(heading.group(1))} {text}")
            else:
                parts.append(text)
        return "\n\n".join(parts)
    except Exception as exc:
        raise FileParseError(f"docx 解析失败: {exc}") from exc
