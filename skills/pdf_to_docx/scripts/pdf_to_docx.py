#!/usr/bin/env python
"""PDF 페이지를 이미지로 변환하여 docx 양식에 삽입.

사용법:
    python pdf_to_docx.py slides.pdf template.docx
    python pdf_to_docx.py slides.pdf template.docx -o output.docx
    python pdf_to_docx.py slides.pdf template.docx --width 6.0 --dpi 150

필요 패키지: pip install python-docx pdf2image pypdf
"""

import argparse
import shutil
import tempfile
from pathlib import Path

from docx import Document
from docx.shared import Inches
from pdf2image import convert_from_path


def parse_args():
    p = argparse.ArgumentParser(description="PDF → docx 이미지 삽입")
    p.add_argument("pdf", help="PDF 파일 경로")
    p.add_argument("docx", help="docx 양식 파일 경로")
    p.add_argument("-o", "--output", help="출력 경로 (기본: 양식명_완성.docx)")
    p.add_argument("--width", type=float, default=1.0,
                   help="너비 비율 (기본: 1.0 = 6.5인치)")
    p.add_argument("--height", type=float, default=None,
                   help="높이 비율 (기본: 원본 비율 유지, 예: 0.8 = 80%%)")
    p.add_argument("--dpi", type=int, default=200,
                   help="PDF 변환 해상도 (기본: 200)")
    return p.parse_args()


def main():
    args = parse_args()

    pdf_path = Path(args.pdf)
    docx_path = Path(args.docx)
    output = args.output or str(docx_path.with_stem(docx_path.stem + "_완성"))

    print(f"PDF: {pdf_path.name}")
    print(f"양식: {docx_path.name}")

    # PDF → 이미지
    print(f"변환 중 (dpi={args.dpi})...", end=" ", flush=True)
    images = convert_from_path(str(pdf_path), dpi=args.dpi)
    print(f"{len(images)}페이지")

    tmpdir = tempfile.mkdtemp()
    img_paths = []
    for i, img in enumerate(images):
        p = Path(tmpdir) / f"slide_{i}.png"
        img.save(str(p), "PNG")
        img_paths.append(p)

    # docx에 삽입
    doc = Document(str(docx_path))
    doc.add_page_break()

    BASE_WIDTH = 6.5
    actual_width = BASE_WIDTH * args.width

    # 원본 비율로 기본 높이 계산
    sample = images[0]
    aspect = sample.size[1] / sample.size[0]  # height/width
    base_height = actual_width * aspect

    if args.height is not None:
        actual_height = base_height * args.height
    else:
        actual_height = None  # 원본 비율 유지

    print(f"이미지: {actual_width:.1f}\" x "
          f"{actual_height:.1f}\"" if actual_height else
          f"이미지: {actual_width:.1f}\" x auto")

    for i, img_path in enumerate(img_paths):
        kwargs = {"width": Inches(actual_width)}
        if actual_height is not None:
            kwargs["height"] = Inches(actual_height)
        doc.add_picture(str(img_path), **kwargs)
        if i < len(img_paths) - 1:
            doc.add_paragraph()

    doc.save(output)
    shutil.rmtree(tmpdir)

    print(f"저장: {output}")


if __name__ == "__main__":
    main()
