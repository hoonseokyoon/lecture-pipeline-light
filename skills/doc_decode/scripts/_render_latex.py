"""LaTeX 수식 → PNG 렌더. matplotlib.mathtext 사용 (no TeX dist 필요).

복잡한 macro (align, matrix 등)는 mathtext 가 일부만 지원하므로, 실패 시
RenderError 발생 → annotate 쪽에서 "렌더 실패" hint 로 재annotate 유도.
"""

from __future__ import annotations

import io
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from PIL import Image


class RenderError(RuntimeError):
    pass


def _prepare_math(src: str) -> str:
    """LaTeX 원문을 mathtext 에 맞게 경량 정리.

    - 양끝 $ / $$ 있으면 제거
    - \begin{...} / \end{...} 중 mathtext 가 잘 못 다루는 것 경고 없이 그대로 둠.
    """
    s = src.strip()
    # display math $$...$$ → ...
    if s.startswith("$$") and s.endswith("$$"):
        s = s[2:-2].strip()
    elif s.startswith("$") and s.endswith("$"):
        s = s[1:-1].strip()
    return s


def render_latex(src: str, out_path: Path,
                 dpi: int = 200, max_width_px: int = 1200) -> Image.Image:
    """LaTeX 수식을 PNG 로 렌더해 파일로 저장 + PIL.Image 반환.

    Raises:
        RenderError: mathtext 가 파싱 실패 or 결과 이미지가 비었을 때.
    """
    if not src or not src.strip():
        raise RenderError("빈 LaTeX 소스")

    body = _prepare_math(src)
    math = f"${body}$"

    # matplotlib 단일-라인 수식 렌더 — 폰트 크기는 적당히 크게.
    fig = plt.figure(figsize=(0.1, 0.1), dpi=dpi)
    fig.patch.set_facecolor("white")
    try:
        try:
            fig.text(
                0, 0, math,
                fontsize=28, color="black",
                horizontalalignment="left",
                verticalalignment="bottom",
            )
        except Exception as exc:
            raise RenderError(f"mathtext 파싱 실패: {exc}") from exc

        buf = io.BytesIO()
        try:
            fig.savefig(
                buf,
                format="png",
                bbox_inches="tight",
                pad_inches=0.05,
                dpi=dpi,
            )
        except Exception as exc:
            raise RenderError(f"render 실패: {exc}") from exc
    finally:
        plt.close(fig)

    buf.seek(0)
    img = Image.open(buf)
    img.load()
    if img.size[0] == 0 or img.size[1] == 0:
        raise RenderError("렌더 결과 이미지 크기 0")

    # 너무 넓으면 다운스케일 (비교 편의).
    w, h = img.size
    if w > max_width_px:
        s = max_width_px / w
        img = img.resize((round(w * s), round(h * s)), Image.LANCZOS)

    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(out_path, format="PNG")
    return img
