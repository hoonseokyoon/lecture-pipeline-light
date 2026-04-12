#!/usr/bin/env python
"""NotebookLM 기반 오디오 전사.

사용법:
    python transcribe.py lecture.m4a
    python transcribe.py lecture.m4a -o lecture.txt
    python transcribe.py lecture.m4a --notebook-id abc-123-def

사전 준비: notebooklm login (1회)
"""

import argparse
import asyncio
import sys
from pathlib import Path

from notebooklm import NotebookLMClient


def parse_args():
    p = argparse.ArgumentParser(description="오디오 → 텍스트 전사 (NotebookLM)")
    p.add_argument("audio", help="오디오 파일 경로")
    p.add_argument("-o", "--output", help="출력 파일 경로 (기본: 파일명.txt)")
    p.add_argument("--notebook-id", help="기존 노트북 ID (미지정 시 새로 생성)")
    p.add_argument("--notebook-name", help="새 노트북 이름 (미지정 시 파일명)")
    p.add_argument("--timeout", type=int, default=300,
                   help="전사 대기 시간(초) (기본: 300)")
    return p.parse_args()


def format_output(text: str, summary: str, keywords: list[str]) -> str:
    kw_block = "\n".join(keywords)
    return f"""<text>
{text}
</text>

<summary>
{summary}
</summary>

<keywords>
{kw_block}
</keywords>
"""


async def transcribe(
    audio_path: str,
    output_path: str,
    notebook_id: str | None,
    notebook_name: str | None,
    timeout: int,
):
    audio = Path(audio_path)
    if not audio.exists():
        print(f"오류: 파일 없음: {audio}", file=sys.stderr)
        sys.exit(1)

    async with await NotebookLMClient.from_storage() as client:
        # 노트북
        if notebook_id:
            nb_id = notebook_id
            print(f"기존 노트북: {nb_id}")
        else:
            name = notebook_name or audio.stem
            nb = await client.notebooks.create(name)
            nb_id = nb.id
            print(f"노트북 생성: {name} ({nb_id})")

        # 업로드 + 전사 대기
        size_mb = audio.stat().st_size / 1024 / 1024
        print(f"업로드: {audio.name} ({size_mb:.1f} MB)")
        print(f"전사 대기 중... (최대 {timeout}초)")
        source = await client.sources.add_file(
            nb_id, str(audio), wait=True, wait_timeout=timeout,
        )
        print(f"전사 완료: source={source.id}")

        # 추출
        fulltext = await client.sources.get_fulltext(nb_id, source.id)
        guide = await client.sources.get_guide(nb_id, source.id)

        # 저장
        content = format_output(
            fulltext.content, guide["summary"], guide["keywords"],
        )
        out = Path(output_path)
        out.write_text(content, encoding="utf-8")

        print(f"\n저장: {out.absolute()}")
        print(f"  텍스트: {len(fulltext.content):,}자")
        print(f"  키워드: {', '.join(guide['keywords'])}")
        print(f"  노트북: {nb_id}")
        print(f"NOTEBOOK_ID={nb_id}")
        print(f"SOURCE_ID={source.id}")


def main():
    args = parse_args()
    output = args.output or str(Path(args.audio).with_suffix(".txt"))
    asyncio.run(transcribe(
        args.audio, output, args.notebook_id, args.notebook_name, args.timeout,
    ))


if __name__ == "__main__":
    main()
