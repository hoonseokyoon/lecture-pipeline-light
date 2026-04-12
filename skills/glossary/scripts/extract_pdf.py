#!/usr/bin/env python
"""PDF → 텍스트 덤프 (페이지별 구분).

사용법:
    python extract_pdf.py file1.pdf file2.pdf -o output_dir
"""

import argparse
import sys
from pathlib import Path

import pdfplumber


def parse_args():
    p = argparse.ArgumentParser(description="PDF → txt (pdfplumber)")
    p.add_argument("pdf", nargs="+", help="PDF 파일 경로")
    p.add_argument("-o", "--output-dir", default=".",
                   help="출력 디렉토리 (기본: 현재 폴더)")
    return p.parse_args()


def extract_one(pdf_path: Path, output_dir: Path) -> Path:
    with pdfplumber.open(str(pdf_path)) as pdf:
        pages = []
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            pages.append(f"--- page {i} ---\n{text}")
    out = output_dir / (pdf_path.stem + ".txt")
    out.write_text("\n\n".join(pages), encoding="utf-8")
    return out


def main():
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for pdf in args.pdf:
        out = extract_one(Path(pdf), output_dir)
        print(f"{pdf} -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
