"""outline.figure_refs 의 상대 경로를 workspace 기준 절대 경로로 해석.

분류:
- `.png/.jpg/.jpeg/.webp/.pdf` → image (includegraphics)
- `.tex`                        → tikz (image_generation 산출물; inline \\input)
- 그 외 / 존재하지 않음          → missing

TikZ 파일 상단에 `% requires: \\usetikzlibrary{...}` 주석이 있으면 parse 해서
preamble aggregate 대상으로 반환.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from _lib.schema import Slide

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}
_TIKZ_EXTS = {".tex"}
_TIKZ_REQ = re.compile(r"%\s*requires:\s*\\usetikzlibrary\{([^}]+)\}")


@dataclass
class ResolvedFigure:
    ref: str
    kind: Literal["image", "tikz", "missing"]
    abs_path: Path | None = None
    tikz_libraries: set[str] = field(default_factory=set)
    error: str = ""


def resolve_figures(
    slides: list[Slide],
    workspace: Path,
) -> dict[str, ResolvedFigure]:
    """모든 slide.figure_refs 를 1회씩만 해석해서 dict 로 반환."""
    resolved: dict[str, ResolvedFigure] = {}
    # basename 캐시 — fallback 검색 시 workspace 전수 스캔을 여러 번 하지 않도록.
    basename_cache: dict[str, Path] | None = None
    for slide in slides:
        for ref in slide.figure_refs:
            if ref in resolved:
                continue
            r, basename_cache = _resolve_one(ref, workspace, basename_cache)
            resolved[ref] = r
    return resolved


def _build_basename_index(workspace: Path) -> dict[str, Path]:
    """workspace 전체를 rglob 으로 스캔, basename → path 맵 구축.

    pick_figures_for_slide 가 assets 내부의 subdir (page_001/img-6.jpeg) 을
    벗겨서 ref 를 저장하는 케이스 대비. basename 이 여러 군데 있으면 첫 번째
    매치만 사용 (사용 환경에서는 basename 이 전체 unique 하다고 가정).
    """
    index: dict[str, Path] = {}
    for ext in _IMAGE_EXTS | _TIKZ_EXTS:
        for p in workspace.rglob(f"*{ext}"):
            name = p.name
            if name not in index:
                index[name] = p
    return index


def _resolve_one(
    ref: str,
    workspace: Path,
    basename_cache: dict[str, Path] | None,
) -> tuple[ResolvedFigure, dict[str, Path] | None]:
    abs_path = (workspace / ref).resolve()
    if not abs_path.exists():
        # Fallback: basename 검색.
        if basename_cache is None:
            basename_cache = _build_basename_index(workspace)
        basename = Path(ref).name
        found = basename_cache.get(basename)
        if found is None:
            return ResolvedFigure(
                ref=ref, kind="missing",
                error=f"file not found: {abs_path}",
            ), basename_cache
        abs_path = found.resolve()
    ext = abs_path.suffix.lower()
    if ext in _IMAGE_EXTS:
        return ResolvedFigure(
            ref=ref, kind="image", abs_path=abs_path,
        ), basename_cache
    if ext in _TIKZ_EXTS:
        try:
            source = abs_path.read_text(encoding="utf-8")
        except Exception as exc:
            return ResolvedFigure(
                ref=ref, kind="missing",
                error=f"tex 읽기 실패: {exc}",
            ), basename_cache
        libs: set[str] = set()
        for m in _TIKZ_REQ.finditer(source):
            for lib in m.group(1).split(","):
                lib = lib.strip()
                if lib:
                    libs.add(lib)
        return ResolvedFigure(
            ref=ref, kind="tikz", abs_path=abs_path,
            tikz_libraries=libs,
        ), basename_cache
    return ResolvedFigure(
        ref=ref, kind="missing",
        error=f"unsupported ext: {ext}",
    ), basename_cache


def collect_tikz_libraries(
    resolved: dict[str, ResolvedFigure],
) -> set[str]:
    libs: set[str] = set()
    for r in resolved.values():
        libs |= r.tikz_libraries
    return libs


def collect_missing(
    resolved: dict[str, ResolvedFigure],
) -> list[str]:
    return sorted(r.ref for r in resolved.values() if r.kind == "missing")
