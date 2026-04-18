"""PIL + bbox 유틸 (호스트 측).

좌표 컨벤션
- 픽셀 좌표: int (x1, y1, x2, y2), 원점 좌상단.
- 정규화 좌표: [0, 1000] float — Gemini 에 줄 때 사용.
scripts/_image.py 는 scripts 전용 사본.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


NORM_MAX = 1000.0


def load_image(path: Path, max_side: int = 2000) -> Image.Image:
    img = Image.open(str(path))
    img.load()
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        s = max_side / max(w, h)
        img = img.resize((round(w * s), round(h * s)), Image.LANCZOS)
    return img


def save_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def norm_to_pixel(box: tuple[float, float, float, float], w: int, h: int
                  ) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    return (
        round(x1 / NORM_MAX * w),
        round(y1 / NORM_MAX * h),
        round(x2 / NORM_MAX * w),
        round(y2 / NORM_MAX * h),
    )


def pixel_to_norm(box: tuple[int, int, int, int], w: int, h: int
                  ) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = box
    return (
        x1 / w * NORM_MAX,
        y1 / h * NORM_MAX,
        x2 / w * NORM_MAX,
        y2 / h * NORM_MAX,
    )


def clip_pixel(box: tuple[int, int, int, int], w: int, h: int
               ) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    x1 = max(0, min(w, x1))
    y1 = max(0, min(h, y1))
    x2 = max(0, min(w, x2))
    y2 = max(0, min(h, y2))
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return x1, y1, x2, y2


def iou(a: tuple[float, float, float, float],
        b: tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    a_area = max(0.0, (ax2 - ax1) * (ay2 - ay1))
    b_area = max(0.0, (bx2 - bx1) * (by2 - by1))
    u = a_area + b_area - inter
    return inter / u if u > 0 else 0.0


def _get_font(size: int = 14) -> ImageFont.ImageFont:
    for name in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


_TYPE_COLORS = {
    "text": "#1976d2",
    "figure": "#388e3c",
    "table": "#f57c00",
    "equation": "#c2185b",
    "code": "#5d4037",
    "form": "#7b1fa2",
    "unknown": "#757575",
}


def draw_annotated(img: Image.Image,
                   objects: list[dict]) -> Image.Image:
    """objects[] 의 bbox + id/type 라벨을 그린 복사본 반환."""
    out = img.copy()
    if out.mode != "RGB":
        out = out.convert("RGB")
    draw = ImageDraw.Draw(out)
    font = _get_font(14)
    for obj in objects:
        bbox = obj.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        x1, y1, x2, y2 = map(int, bbox)
        ann = obj.get("annotation") or {}
        typ = ann.get("type") or obj.get("detected_type") or "unknown"
        color = _TYPE_COLORS.get(typ, _TYPE_COLORS["unknown"])
        draw.rectangle([x1, y1, x2, y2], outline=color, width=3)
        label = f"{obj.get('id', '?')} [{typ}]"
        try:
            tb = draw.textbbox((x1, y1), label, font=font)
            pad = 2
            draw.rectangle(
                (tb[0] - pad, tb[1] - pad, tb[2] + pad, tb[3] + pad),
                fill=color,
            )
        except AttributeError:
            pass
        draw.text((x1, y1), label, fill="white", font=font)
    return out


def crop_bbox(img: Image.Image,
              bbox_px: tuple[int, int, int, int]) -> Image.Image:
    """bbox 영역 crop. 좌표 클램프 포함."""
    w, h = img.size
    x1, y1, x2, y2 = clip_pixel(bbox_px, w, h)
    if x2 <= x1 + 1:
        x2 = x1 + 2
    if y2 <= y1 + 1:
        y2 = y1 + 2
    return img.crop((x1, y1, x2, y2))
