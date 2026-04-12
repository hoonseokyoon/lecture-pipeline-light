#!/usr/bin/env python
"""전사본 교정 CLI (skill 기반).

사용법:
    python correct.py transcript.txt
    python correct.py transcript.txt -o corrected.txt
    python correct.py transcript.txt --skill skills/transcript_correct

입력 파일에 `<text>` 태그가 있으면 그 블록만 교정하고, `<summary>`/`<keywords>`는
원본 그대로 유지한다. 태그가 없으면 전체 내용을 교정 대상으로 취급한다.

사전 준비: WSL Ubuntu + Codex CLI 설치 + `codex auth login`.
"""

import argparse
import re
import sys
import tempfile
from pathlib import Path

from codex_runner import CodexRunError, run_skill


DEFAULT_SKILL = Path(__file__).parent / "skills" / "transcript_correct"


def parse_args():
    p = argparse.ArgumentParser(description="전사본 Codex 교정 (skill 기반)")
    p.add_argument("input", help="전사본 파일 경로")
    p.add_argument("-o", "--output", help="출력 경로 (기본: 파일명_corrected.txt)")
    p.add_argument(
        "--skill",
        default=str(DEFAULT_SKILL),
        help=f"skill 디렉토리 (기본: {DEFAULT_SKILL})",
    )
    # transcribe_folder.py 호환용 — deprecated, 무시됨.
    p.add_argument("-c", "--config", help=argparse.SUPPRESS)
    return p.parse_args()


def extract_tag(content: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", content, re.DOTALL)
    return m.group(1) if m else ""


def main():
    args = parse_args()
    if args.config:
        print(
            "경고: -c/--config는 더 이상 사용되지 않습니다 (무시됨). --skill을 쓰세요.",
            file=sys.stderr,
        )

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"오류: 파일 없음: {input_path}", file=sys.stderr)
        sys.exit(1)

    content = input_path.read_text(encoding="utf-8")
    text = extract_tag(content, "text") or content
    summary = extract_tag(content, "summary")
    keywords = extract_tag(content, "keywords")

    print(f"입력: {input_path.name} ({len(text):,}자)")
    print(f"Codex 실행 중 (skill={args.skill})...")

    # run_skill은 파일 경로 리스트를 받으므로 <text> 블록을 임시 파일로 내보낸다.
    with tempfile.TemporaryDirectory(prefix="correct_") as tmpdir:
        tmp_input = Path(tmpdir) / "transcript.txt"
        tmp_input.write_text(text, encoding="utf-8")
        try:
            outputs = run_skill(
                args.skill,
                [tmp_input],
                log_callback=lambda m: print(f"  {m}", file=sys.stderr),
            )
        except CodexRunError as exc:
            print(f"교정 실패: {exc}", file=sys.stderr)
            sys.exit(1)

    if not outputs:
        print("교정 실패: skill이 출력 파일을 내지 않음", file=sys.stderr)
        sys.exit(1)
    if len(outputs) > 1:
        print(
            f"경고: skill이 {len(outputs)}개 출력을 냈음. 첫 번째만 사용.",
            file=sys.stderr,
        )
    corrected = next(iter(outputs.values())).decode("utf-8").strip()

    parts = [f"<text>\n{corrected}\n</text>"]
    if summary:
        parts.append(f"\n<summary>\n{summary}\n</summary>")
    if keywords:
        parts.append(f"\n<keywords>\n{keywords}\n</keywords>")
    output_content = "\n".join(parts) + "\n"

    out_path = args.output or str(
        input_path.with_stem(input_path.stem + "_corrected")
    )
    Path(out_path).write_text(output_content, encoding="utf-8")

    print(f"\n저장: {out_path}")
    print(f"  원본: {len(text):,}자 → 교정: {len(corrected):,}자")


if __name__ == "__main__":
    main()
