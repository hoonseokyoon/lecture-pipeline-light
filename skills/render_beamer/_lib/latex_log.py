"""latexmk/xelatex 로그 파서.

전략:
- `! ` 로 시작하는 에러 라인을 스캔.
- 이어지는 20줄에서 `l.NNN` 또는 `<file>:<line>:` (file-line-error) 추출.
- `.tex` 의 `% ── slide N ──` 마커 위치 → line → slide_number 맵 생성.
- `\\begin{document}` 이전이면 preamble error, 이후면 가장 가까운 frame 귀속.
"""

from __future__ import annotations

import re

from _lib.schema import LatexError

_ERR_START = re.compile(r"^! ")
_LINE_INLINE = re.compile(r"^l\.(\d+)\s*(.*)")
_FILE_LINE = re.compile(r"^\S+?\.tex:(\d+):\s*(.*)")
_FRAME_MARKER = re.compile(r"%\s*──\s*slide\s+(\d+)")


def _build_frame_line_map(tex_text: str) -> list[tuple[int, int]]:
    """tex 를 줄 단위로 스캔해 (line_no, slide_number) 목록 반환 (정렬됨)."""
    mapping: list[tuple[int, int]] = []
    for i, line in enumerate(tex_text.splitlines(), start=1):
        m = _FRAME_MARKER.search(line)
        if m:
            mapping.append((i, int(m.group(1))))
    return mapping


def _find_doc_begin_line(tex_text: str) -> int:
    for i, line in enumerate(tex_text.splitlines(), start=1):
        if r"\begin{document}" in line:
            return i
    return 1


def _classify(
    line: int | None,
    doc_begin: int,
    frame_map: list[tuple[int, int]],
) -> tuple[str, int | None]:
    if line is None:
        return "unknown", None
    if line < doc_begin:
        return "preamble", None
    # frame_map 에서 line 이하 최대값.
    idx: int | None = None
    for ln, sn in frame_map:
        if ln <= line:
            idx = sn
        else:
            break
    if idx is not None:
        return "frame", idx
    return "unknown", None


def parse_latex_log(log_text: str, tex_text: str) -> list[LatexError]:
    frame_map = _build_frame_line_map(tex_text)
    doc_begin = _find_doc_begin_line(tex_text)

    errors: list[LatexError] = []
    lines = log_text.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        if not _ERR_START.match(line):
            # file-line-error 형태도 체크 (latexmk 의 `-file-line-error`)
            m = _FILE_LINE.match(line)
            if m:
                line_no = int(m.group(1))
                msg = m.group(2).strip()
                context_lines = lines[i : min(i + 6, n)]
                kind, frame_idx = _classify(line_no, doc_begin, frame_map)
                errors.append(LatexError(
                    line=line_no, message=msg,
                    context="\n".join(context_lines)[:600],
                    kind=kind, frame_index=frame_idx,
                ))
                i += 1
                continue
            i += 1
            continue

        msg = line[2:].strip()
        line_no: int | None = None
        consumed_to = i
        context_lines = [line]
        # 다음 20줄에서 `l.NNN` 먼저 찾기.
        for j in range(i + 1, min(i + 20, n)):
            context_lines.append(lines[j])
            m = _LINE_INLINE.match(lines[j])
            if m:
                line_no = int(m.group(1))
                consumed_to = j
                break
        kind, frame_idx = _classify(line_no, doc_begin, frame_map)
        errors.append(LatexError(
            line=line_no, message=msg,
            context="\n".join(context_lines)[:600],
            kind=kind, frame_index=frame_idx,
        ))
        i = consumed_to + 1

    # 중복 제거 (같은 frame 에 같은 메시지가 중복 emit 되는 경우)
    seen: set[tuple[str | None, int | None, str]] = set()
    deduped: list[LatexError] = []
    for e in errors:
        key = (e.kind, e.frame_index, e.message[:100])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    return deduped
