"""zero_shot_rec의 PIL 기반 이미지 유틸.

좌표 컨벤션
- 정규화 좌표: [0, 1000] float (x1, y1, x2, y2). 저장/로직/LLM 출력에 사용.
- 픽셀 좌표: int (x1, y1, x2, y2). PIL 드로잉 시점에만 변환.
- 원점은 좌상단.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont


NORM_MAX = 1000.0


# ───────────────────────── 로드 / 저장 ─────────────────────────

def load_image(path: Path, max_side: int = 1600) -> Image.Image:
    """PNG/JPG 로드. 긴 변이 max_side 초과면 LANCZOS로 다운스케일."""
    img = Image.open(str(path))
    img.load()
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    w, h = img.size
    long_side = max(w, h)
    if long_side > max_side:
        scale = max_side / long_side
        img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
    return img


def save_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ───────────────────────── 좌표 변환 ─────────────────────────

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


def clip_box_norm(box: tuple[float, float, float, float]
                  ) -> tuple[float, float, float, float]:
    """정규화 박스를 [0, NORM_MAX]로 클램프하고 x1<x2, y1<y2 보장."""
    x1, y1, x2, y2 = box
    x1 = max(0.0, min(NORM_MAX, x1))
    y1 = max(0.0, min(NORM_MAX, y1))
    x2 = max(0.0, min(NORM_MAX, x2))
    y2 = max(0.0, min(NORM_MAX, y2))
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return x1, y1, x2, y2


# ───────────────────────── IoU ─────────────────────────

def iou(a: tuple[float, float, float, float],
        b: tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area_a = max(0.0, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(0.0, (bx2 - bx1) * (by2 - by1))
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union


# ───────────────────────── 점 매트릭스 ─────────────────────────

def dot_matrix_norm(bounds_norm: tuple[float, float, float, float],
                    rows: int, cols: int,
                    margin_frac: float = 0.08
                    ) -> list[tuple[float, float, str]]:
    """주어진 정규화 영역 내 점 매트릭스. Row-major P1..P<n>."""
    x1, y1, x2, y2 = bounds_norm
    w = x2 - x1
    h = y2 - y1
    mx = w * margin_frac
    my = h * margin_frac
    ix1, iy1 = x1 + mx, y1 + my
    ix2, iy2 = x2 - mx, y2 - my
    dots: list[tuple[float, float, str]] = []
    for r in range(rows):
        for c in range(cols):
            fx = c / (cols - 1) if cols > 1 else 0.5
            fy = r / (rows - 1) if rows > 1 else 0.5
            x = ix1 + fx * (ix2 - ix1)
            y = iy1 + fy * (iy2 - iy1)
            idx = r * cols + c + 1
            dots.append((x, y, f"P{idx}"))
    return dots


# ─────────────────── ROI ↔ 원본 좌표 변환 ───────────────────

def orig_norm_to_roi_pixel(point_orig_norm: tuple[float, float],
                           crop_box_norm: tuple[float, float, float, float],
                           roi_size: tuple[int, int]
                           ) -> tuple[int, int]:
    """원본 정규화 좌표 한 점 → ROI 이미지 픽셀 좌표."""
    ox, oy = point_orig_norm
    cx1, cy1, cx2, cy2 = crop_box_norm
    rw, rh = roi_size
    rx = (ox - cx1) / (cx2 - cx1) * rw if cx2 != cx1 else 0.0
    ry = (oy - cy1) / (cy2 - cy1) * rh if cy2 != cy1 else 0.0
    return round(rx), round(ry)


def box_norm_to_roi_pixel(box_norm: tuple[float, float, float, float],
                          crop_box_norm: tuple[float, float, float, float],
                          roi_size: tuple[int, int]
                          ) -> tuple[int, int, int, int]:
    """원본 정규화 박스 → ROI 이미지 픽셀 좌표 박스."""
    x1, y1, x2, y2 = box_norm
    p1 = orig_norm_to_roi_pixel((x1, y1), crop_box_norm, roi_size)
    p2 = orig_norm_to_roi_pixel((x2, y2), crop_box_norm, roi_size)
    return p1[0], p1[1], p2[0], p2[1]


# ───────────────────────── 드로잉 ─────────────────────────

def _get_font(size: int = 14) -> ImageFont.ImageFont:
    for name in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _rgb_tuple(color) -> tuple[int, int, int]:
    """color name/hex/tuple → RGB 3-tuple."""
    if isinstance(color, (tuple, list)):
        return tuple(int(c) for c in color[:3])
    return ImageColor.getrgb(color)[:3]


def draw_box(img: Image.Image,
             box_px: tuple[int, int, int, int],
             color: str = "red", width: int = 3,
             label: str | None = None,
             label_bg: str = "red") -> Image.Image:
    """박스 + 옵션 라벨 그림. 원본 수정 후 반환."""
    draw = ImageDraw.Draw(img)
    draw.rectangle(box_px, outline=color, width=width)
    if label:
        font = _get_font(22)
        x, y = box_px[0], box_px[1]
        try:
            bbox = draw.textbbox((x, y), label, font=font)
            pad = 2
            bg = (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
            draw.rectangle(bg, fill=label_bg)
        except AttributeError:
            pass
        draw.text((x, y), label, fill="white", font=font)
    return img


def draw_dots(img: Image.Image,
              dots_px: list[tuple[int, int, str]],
              radius: int = 6,
              dot_color: str = "yellow",
              outline_color: str = "black",
              label: bool = True,
              font_size: int = 11,
              alpha: int = 255) -> Image.Image:
    """노란 점 + 외곽 + 라벨. alpha<255면 점 fill에만 반투명 적용.

    라벨과 외곽선은 가독성을 위해 항상 불투명(또는 거의 불투명) 유지.
    원본 이미지를 in-place 수정 후 반환.
    """
    if alpha >= 255:
        draw = ImageDraw.Draw(img)
        font = _get_font(font_size)
        for x, y, name in dots_px:
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                fill=dot_color, outline=outline_color, width=2,
            )
            if label:
                lx, ly = x + radius + 2, y - radius - font_size
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    draw.text((lx + dx, ly + dy), name, fill="black", font=font)
                draw.text((lx, ly), name, fill="yellow", font=font)
        return img

    # 반투명 경로: 점 원을 RGBA overlay에 그린 후 composite.
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d_over = ImageDraw.Draw(overlay)
    dot_rgb = _rgb_tuple(dot_color)
    outline_rgb = _rgb_tuple(outline_color)
    dot_fill = (*dot_rgb, alpha)
    outline_fill = (*outline_rgb, min(255, alpha + 50))
    for x, y, _ in dots_px:
        d_over.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=dot_fill, outline=outline_fill, width=2,
        )
    base = img.convert("RGBA")
    blended = Image.alpha_composite(base, overlay).convert("RGB")
    img.paste(blended)

    # 라벨은 composite 이후 불투명하게 다시 그림 (가독성).
    if label:
        draw = ImageDraw.Draw(img)
        font = _get_font(font_size)
        for x, y, name in dots_px:
            lx, ly = x + radius + 2, y - radius - font_size
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                draw.text((lx + dx, ly + dy), name, fill="black", font=font)
            draw.text((lx, ly), name, fill="yellow", font=font)
    return img


def draw_edge_line(img: Image.Image,
                   edge: str,
                   edge_px_value: int,
                   color: str = "red", width: int = 2,
                   alpha: int = 255) -> Image.Image:
    """edge 위치를 수평/수직 선으로 그리기. alpha<255면 반투명 composite."""
    w, h = img.size
    if alpha >= 255:
        draw = ImageDraw.Draw(img)
        if edge in ("top", "bottom"):
            draw.line([(0, edge_px_value), (w, edge_px_value)],
                      fill=color, width=width)
        elif edge in ("left", "right"):
            draw.line([(edge_px_value, 0), (edge_px_value, h)],
                      fill=color, width=width)
        else:
            raise ValueError(f"invalid edge: {edge}")
        return img

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    fill = (*_rgb_tuple(color), alpha)
    if edge in ("top", "bottom"):
        d.line([(0, edge_px_value), (w, edge_px_value)], fill=fill, width=width)
    elif edge in ("left", "right"):
        d.line([(edge_px_value, 0), (edge_px_value, h)], fill=fill, width=width)
    else:
        raise ValueError(f"invalid edge: {edge}")
    base = img.convert("RGBA")
    blended = Image.alpha_composite(base, overlay).convert("RGB")
    img.paste(blended)
    return img


def annotate_stage0_candidates(img: Image.Image,
                               boxes_norm: list[tuple[float, float, float, float]],
                               ids: list[int]) -> Image.Image:
    """Stage 0 후보 박스에 번호 매김. 복사본 반환."""
    out = img.copy()
    colors = ["red", "blue", "green", "orange", "purple"]
    w, h = out.size
    for i, (box, num) in enumerate(zip(boxes_norm, ids)):
        color = colors[i % len(colors)]
        box_px = norm_to_pixel(box, w, h)
        draw_box(out, box_px, color=color, width=4, label=str(num), label_bg=color)
    return out


# ───────────────────────── Edge ROI 크롭 ─────────────────────────

def crop_edge_roi(img: Image.Image,
                  box_norm: tuple[float, float, float, float],
                  edge: str,
                  outward: float = 0.25,
                  inward: float = 0.25,
                  lateral: float = 0.20,
                  min_roi_px: int = 400
                  ) -> tuple[Image.Image, tuple[float, float, float, float], float]:
    """edge 주변 ROI 크롭. (roi_img_pixel, crop_box_orig_norm, scale) 반환.

    scale: 업스케일 배율 (1.0 = 업스케일 없음, >1.0 = 업스케일됨).

    Args:
        outward: 박스 **바깥쪽** 방향으로 확장할 비율 (변 길이 대비). target이
            박스 밖으로 나갔다고 의심될 때 크게.
        inward:  박스 **안쪽** 방향으로 확장할 비율. target이 박스 안쪽에
            있다고 의심될 때 크게.
        lateral: edge에 수직이 아닌 평행 방향 context 비율 (대칭). 기본 0.20.
        min_roi_px: ROI 짧은 변이 이 픽셀 미만이면 LANCZOS 업스케일.

    예 (top edge): 수직 범위 = [y1 - outward*bh, y1 + inward*bh].
                   수평 범위 = [x1 - lateral*bw, x2 + lateral*bw].
    """
    x1, y1, x2, y2 = box_norm
    bw, bh = x2 - x1, y2 - y1
    lat_x = bw * lateral
    lat_y = bh * lateral
    out_x = bw * outward
    out_y = bh * outward
    in_x = bw * inward
    in_y = bh * inward

    if edge == "top":
        cy1, cy2 = y1 - out_y, y1 + in_y
        cx1, cx2 = x1 - lat_x, x2 + lat_x
    elif edge == "bottom":
        cy1, cy2 = y2 - in_y, y2 + out_y
        cx1, cx2 = x1 - lat_x, x2 + lat_x
    elif edge == "left":
        cx1, cx2 = x1 - out_x, x1 + in_x
        cy1, cy2 = y1 - lat_y, y2 + lat_y
    elif edge == "right":
        cx1, cx2 = x2 - in_x, x2 + out_x
        cy1, cy2 = y1 - lat_y, y2 + lat_y
    else:
        raise ValueError(f"invalid edge: {edge}")

    cx1 = max(0.0, cx1)
    cy1 = max(0.0, cy1)
    cx2 = min(NORM_MAX, cx2)
    cy2 = min(NORM_MAX, cy2)
    if cx2 <= cx1:
        cx2 = cx1 + 1.0
    if cy2 <= cy1:
        cy2 = cy1 + 1.0

    w, h = img.size
    px1, py1, px2, py2 = norm_to_pixel((cx1, cy1, cx2, cy2), w, h)
    px1 = max(0, px1); py1 = max(0, py1)
    px2 = min(w, max(px1 + 1, px2)); py2 = min(h, max(py1 + 1, py2))
    roi = img.crop((px1, py1, px2, py2))

    rw, rh = roi.size
    short = min(rw, rh)
    scale = 1.0
    if short > 0 and short < min_roi_px:
        scale = min_roi_px / short
        # 긴 변이 2000px 초과하는 극단 케이스(납작한 edge ROI) 방지.
        max_scale_by_long = 2000 / max(rw, rh) if max(rw, rh) > 0 else scale
        if max_scale_by_long < scale:
            scale = max_scale_by_long
        roi = roi.resize((round(rw * scale), round(rh * scale)), Image.LANCZOS)

    return roi, (cx1, cy1, cx2, cy2), scale
