#!/usr/bin/env python
"""PDF → 페이지별 텍스트 파일.

사용법:
    python extract_pdf.py slides.pdf -o _raw

`_raw/page_001.txt`, `_raw/page_002.txt`, ... 형태로 저장.
페이지 번호는 1-based, zero-padded 3자리.
"""

import argparse
import sys
from pathlib import Path

import pdfplumber


def parse_args():
    p = argparse.ArgumentParser(description="PDF → 페이지별 텍스트 (pdfplumber)")
    p.add_argument("pdf", help="PDF 파일 경로")
    p.add_argument("-o", "--output-dir", default=".",
                   help="출력 디렉토리 (기본: 현재 폴더)")
    return p.parse_args()


def main():
    args = parse_args()
    pdf_path = Path(args.pdf)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            out = output_dir / f"page_{i:03d}.txt"
            out.write_text(text, encoding="utf-8")
            print(f"page {i} -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
