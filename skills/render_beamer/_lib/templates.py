"""Beamer preamble + frame 템플릿 (layout 분기).

결정:
- Jinja2 도입 안 함. f-string 충분.
- 모든 frame 은 `[fragile]` (verbatim/listings 가능성 대비).
- 각 frame 앞에 `% ── slide N ──` 마커 삽입 → latex_log 파서가 frame_index 복원.
- figure 1장: 폭 0.9, 높이 0.7
- figure 2장: columns split, 각 폭 0.5
- figure 3~4장: 2x2 grid. 5장 이상은 앞 4장만.
"""

from __future__ import annotations

from pathlib import Path

from _lib.escape import equation_passthrough, escape_outside_math
from _lib.figure_resolver import ResolvedFigure
from _lib.schema import Slide


# ─── Preamble ────────────────────────────────────────────────


def render_preamble(
    *,
    chapter_title: str,
    workspace_abs: Path,
    hangul_font: str,
    aspect_ratio: str,
    tikz_libraries: set[str],
) -> str:
    ratio = aspect_ratio.replace(":", "")
    # graphicspath 에 workspace + 하위 주요 dir 을 등록.
    graphicspath_dirs: list[str] = []
    for sub in [".", "doc_to_md", "generated_figures", "pick_figures"]:
        p = (workspace_abs / sub).resolve()
        if p.exists():
            graphicspath_dirs.append(p.as_posix() + "/")
    graphicspath = "".join("{" + d + "}" for d in graphicspath_dirs)

    lib_lines = "\n".join(
        f"\\usetikzlibrary{{{lib}}}" for lib in sorted(tikz_libraries)
    )

    # kotex 단독 사용 (xeCJK 는 한글 단어 간 공백을 strip 하는 기본 동작 때문에
    # 혼용 피함). kotex 이 `\setmainhangulfont` 로 Korean 폰트 지정 + 공백 보존.
    # 폰트가 없으면 `\IfFontExistsTF` 로 fallback 을 단계별로 시도.
    if hangul_font:
        hangul_block = (
            f"\\IfFontExistsTF{{{hangul_font}}}{{\n"
            f"  \\setmainhangulfont{{{hangul_font}}}\n"
            f"}}{{\n"
            f"  \\IfFontExistsTF{{Malgun Gothic}}{{\n"
            f"    \\setmainhangulfont{{Malgun Gothic}}\n"
            f"  }}{{\n"
            f"    \\IfFontExistsTF{{NanumGothic}}{{\n"
            f"      \\setmainhangulfont{{NanumGothic}}\n"
            f"    }}{{}}\n"
            f"  }}\n"
            f"}}\n"
        )
    else:
        hangul_block = ""

    title_escaped = escape_outside_math(chapter_title)

    return rf"""\documentclass[aspectratio={ratio}]{{beamer}}
\usepackage{{kotex}}
\usepackage{{fontspec}}
{hangul_block}\usepackage{{graphicx}}
\usepackage{{amsmath,amssymb,amsfonts}}
\usepackage{{booktabs}}
\usepackage{{tikz}}
{lib_lines}
\usepackage{{xcolor}}
\usepackage{{hyperref}}

\graphicspath{{{graphicspath}}}

\title{{{title_escaped}}}
\date{{\today}}

\begin{{document}}

\frame{{\titlepage}}

"""


# ─── Frame 공통 조립 ──────────────────────────────────────────


def render_frame(
    slide: Slide,
    resolved_figures: dict[str, ResolvedFigure],
    emit_notes: bool,
) -> str:
    body = _LAYOUT_DISPATCH[slide.layout](slide, resolved_figures)
    title = escape_outside_math(slide.title)

    note_block = ""
    if emit_notes and slide.speaker_hint:
        note_block = (
            f"\\note{{{escape_outside_math(slide.speaker_hint)}}}\n"
        )

    return (
        f"% ── slide {slide.slide_number} ──\n"
        f"\\begin{{frame}}[fragile]{{{title}}}\n"
        f"{body}\n"
        f"\\end{{frame}}\n"
        f"{note_block}"
    )


def render_placeholder_frame(
    slide_number: int, title: str, reason: str,
) -> str:
    """수리 실패한 frame 을 대체하는 안전한 placeholder."""
    t = escape_outside_math(title) or f"Slide {slide_number}"
    r = escape_outside_math(reason)[:120]
    return (
        f"% ── slide {slide_number} (placeholder) ──\n"
        f"\\begin{{frame}}{{{t}}}\n"
        f"\\centering \\Large 렌더 실패\n"
        f"\\par \\vspace{{1em}}\n"
        f"\\normalsize {r}\n"
        f"\\end{{frame}}\n"
    )


# ─── Layout 별 frame body ────────────────────────────────────


def _bullets_list(slide: Slide) -> str:
    items = "\n".join(
        f"  \\item {escape_outside_math(b.text)}" for b in slide.bullets
    )
    if not items:
        return ""
    return f"\\begin{{itemize}}\n{items}\n\\end{{itemize}}"


def _equations_block(slide: Slide) -> str:
    if not slide.equations:
        return ""
    parts = [
        f"\\begin{{equation*}}\n{equation_passthrough(eq)}\n\\end{{equation*}}"
        for eq in slide.equations
    ]
    return "\n".join(parts)


def _layout_bullets(
    slide: Slide, figures: dict[str, ResolvedFigure],
) -> str:
    blocks: list[str] = []
    bl = _bullets_list(slide)
    if bl:
        blocks.append(bl)
    eqs = _equations_block(slide)
    if eqs:
        blocks.append("\\vspace{0.5em}")
        blocks.append(eqs)
    return "\n".join(blocks) if blocks else "\\centering (내용 없음)"


def _layout_equation_focus(
    slide: Slide, figures: dict[str, ResolvedFigure],
) -> str:
    blocks: list[str] = []
    bl = _bullets_list(slide)
    if bl:
        blocks.append(bl)
        blocks.append("\\vspace{0.8em}")
    eqs = _equations_block(slide)
    if eqs:
        blocks.append(eqs)
    if not blocks:
        return "\\centering (수식 없음)"
    return "\n".join(blocks)


def _layout_title_only(
    slide: Slide, figures: dict[str, ResolvedFigure],
) -> str:
    hint = escape_outside_math(slide.speaker_hint)
    if hint:
        return (
            "\\centering\n"
            "\\vspace{1em}\n"
            f"{{\\Large {hint}}}"
        )
    return "\\centering\n\\vspace{1em}\n{\\Large \\vphantom{X}}"


def _layout_figure_focus(
    slide: Slide, figures: dict[str, ResolvedFigure],
) -> str:
    figs = [
        figures[ref] for ref in slide.figure_refs
        if ref in figures and figures[ref].kind != "missing"
    ]
    bl = _bullets_list(slide)

    if not figs:
        # figure 가 다 missing 이면 bullets 만 (fallback)
        return bl or "\\centering (그림 없음)"

    fig_block = _render_figure_block(figs)

    if not bl:
        return fig_block

    return (
        "\\begin{columns}[T]\n"
        "\\begin{column}{0.50\\textwidth}\n"
        f"{bl}\n"
        "\\end{column}\n"
        "\\begin{column}{0.50\\textwidth}\n"
        "\\centering\n"
        f"{fig_block}\n"
        "\\end{column}\n"
        "\\end{columns}"
    )


def _render_figure_block(figs: list[ResolvedFigure]) -> str:
    if len(figs) == 1:
        return _render_single_figure(
            figs[0], width="0.9\\linewidth",
            height="0.70\\textheight",
        )
    if len(figs) == 2:
        left = _render_single_figure(
            figs[0], width="\\linewidth", height="0.55\\textheight",
        )
        right = _render_single_figure(
            figs[1], width="\\linewidth", height="0.55\\textheight",
        )
        return (
            "\\begin{columns}[T]\n"
            "\\begin{column}{0.5\\textwidth}\\centering "
            f"{left}\\end{{column}}\n"
            "\\begin{column}{0.5\\textwidth}\\centering "
            f"{right}\\end{{column}}\n"
            "\\end{columns}"
        )
    # 3~4 장: 2x2 grid. 5+ 는 앞 4장만.
    figs = figs[:4]
    while len(figs) < 4:
        figs.append(None)  # type: ignore[arg-type]

    def _cell(f):
        if f is None:
            return "\\phantom{x}"
        return _render_single_figure(
            f, width="\\linewidth", height="0.32\\textheight",
        )

    return (
        "\\begin{tabular}{cc}\n"
        f"{_cell(figs[0])} & {_cell(figs[1])} \\\\\n"
        f"{_cell(figs[2])} & {_cell(figs[3])} \\\\\n"
        "\\end{tabular}"
    )


def _render_single_figure(
    fig: ResolvedFigure, *, width: str, height: str,
) -> str:
    if fig.kind == "image":
        path = fig.abs_path.as_posix() if fig.abs_path else ""
        return (
            f"\\includegraphics[width={width},height={height},"
            f"keepaspectratio]{{{path}}}"
        )
    if fig.kind == "tikz":
        path = fig.abs_path.as_posix() if fig.abs_path else ""
        # image_generation 규약: 파일이 tikzpicture 블록만 담음.
        # resizebox 로 폭 맞추고 높이는 aspect ratio 유지.
        return (
            f"\\resizebox{{{width}}}{{!}}{{\\input{{{path}}}}}"
        )
    return "\\textit{figure 누락}"


_LAYOUT_DISPATCH = {
    "bullets": _layout_bullets,
    "figure_focus": _layout_figure_focus,
    "equation_focus": _layout_equation_focus,
    "title_only": _layout_title_only,
}
