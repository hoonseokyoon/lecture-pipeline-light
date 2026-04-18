"""PDF → 페이지별 PNG 렌더. PyMuPDF (fitz) 사용 — 호스트에 이미 설치됨."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image


def render_pages(pdf_path: Path, dpi: int = 200,
                 max_side: int = 2000) -> list[Image.Image]:
    """PDF 전체 페이지를 PIL.Image 리스트로 렌더.

    max_side 넘는 긴 변은 LANCZOS 로 다운스케일.
    """
    import fitz  # PyMuPDF (host requirements.txt 에 포함)

    out: list[Image.Image] = []
    doc = fitz.open(str(pdf_path))
    try:
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        for i in range(doc.page_count):
            page = doc[i]
            pix = page.get_pixmap(matrix=mat)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            img.load()
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            w, h = img.size
            if max(w, h) > max_side:
                s = max_side / max(w, h)
                img = img.resize(
                    (round(w * s), round(h * s)), Image.LANCZOS,
                )
            out.append(img)
    finally:
        doc.close()
    return out


def get_page_count(pdf_path: Path) -> int:
    import fitz
    doc = fitz.open(str(pdf_path))
    try:
        return doc.page_count
    finally:
        doc.close()
