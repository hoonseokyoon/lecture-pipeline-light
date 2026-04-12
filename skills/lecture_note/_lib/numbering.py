"""녹취록에 절대 라인 번호 prefix 부여.

transcript_correct 스킬이 출력하는 파일은 `<text>...</text>` wrapper 로 실제
강의 발화를 감싸고, 그 바깥에 `<summary>`/`<keywords>` 같은 metadata 블록을
포함할 수 있음. 이 metadata 는 **강의 녹취의 일부가 아니므로** 라인 번호
부여 전에 제거해야 함 (align/compose LLM 이 혼동하지 않게).
"""

import re
from pathlib import Path


_TEXT_BLOCK_RE = re.compile(r"<text>\s*(.*?)\s*</text>", re.DOTALL)


def _strip_metadata_wrapper(content: str) -> str:
    """`<text>...</text>` 블록만 남기고 나머지 전부 제거.

    매칭이 없으면 (tag 없는 raw transcript) 원본 그대로 반환.
    """
    m = _TEXT_BLOCK_RE.search(content)
    if m is None:
        return content
    body = m.group(1)
    # 원본이 개행으로 끝났으면 유지
    return body + ("\n" if not body.endswith("\n") else "")


def number_file(src: Path, dest: Path) -> int:
    """src의 각 줄에 `[NNNN] ` prefix를 붙여 dest에 저장.

    transcript_correct 의 `<text>...</text>` wrapper 가 있으면 그 안쪽만
    번호 부여 대상. `<summary>`/`<keywords>` 같은 metadata 는 제거됨.

    반환: 번호가 부여된 (stripping 이후의) 전체 라인 수. 1-based.
    """
    raw = src.read_text(encoding="utf-8")
    content = _strip_metadata_wrapper(raw)
    lines = content.splitlines()
    numbered = [f"[{i:04d}] {line}" for i, line in enumerate(lines, start=1)]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        "\n".join(numbered) + ("\n" if content.endswith("\n") else ""),
        encoding="utf-8",
    )
    return len(lines)


def number_transcripts(txts: list[Path], dest_dir: Path) -> dict[str, dict]:
    """여러 녹취록에 라인번호 부여해 dest_dir에 저장.

    반환: {source_name: {"path": 저장된 파일 Path, "line_count": int}}

    source_name은 원본 파일의 basename. 이후 alignment 결과의 source 필드와
    매칭하는 데 사용됨.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, dict] = {}
    for src in txts:
        name = src.name
        dest = dest_dir / name
        line_count = number_file(src, dest)
        result[name] = {"path": dest, "line_count": line_count}
    return result
