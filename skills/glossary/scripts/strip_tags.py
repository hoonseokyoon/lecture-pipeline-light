#!/usr/bin/env python
"""transcript 파일의 <text> 블록만 추출.

사용법:
    python strip_tags.py file1.txt file2.txt -o output_dir

태그가 없으면 원본을 그대로 복사.
"""

import argparse
import re
import sys
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser(description="transcript <text> 블록 추출")
    p.add_argument("input", nargs="+", help="입력 파일")
    p.add_argument("-o", "--output-dir", default=".",
                   help="출력 디렉토리 (기본: 현재 폴더)")
    return p.parse_args()


def strip_one(input_path: Path, output_dir: Path) -> Path:
    content = input_path.read_text(encoding="utf-8")
    m = re.search(r"<text>\s*(.*?)\s*</text>", content, re.DOTALL)
    text = m.group(1) if m else content
    out = output_dir / input_path.name
    out.write_text(text, encoding="utf-8")
    return out


def main():
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for inp in args.input:
        out = strip_one(Path(inp), output_dir)
        print(f"{inp} -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
