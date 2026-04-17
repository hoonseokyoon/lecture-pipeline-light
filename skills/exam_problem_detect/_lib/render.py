"""페이지 annotated PNG 렌더.

각 문제: 빨간 박스 + 좌상단에 "p5-q2" 라벨(작게, 외곽선 포함).
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from _lib import image_utils as iu


Box = tuple[float, float, float, float]


def render_page_annotated(
    img: Image.Image,
    id_box_pairs: list[tuple[str, Box]],
    box_color: str = "red",
) -> Image.Image:
    """원본 이미지에 (id, box) 쌍들을 렌더. 복사본 반환."""
    out = img.copy()
    w, h = out.size

    # 이미지 크기에 비례한 선 두께 / 폰트
    line_width = max(2, round(min(w, h) / 400))
    font_size = max(11, round(min(w, h) / 90))

    for id_str, box_norm in id_box_pairs:
        box_px = iu.norm_to_pixel(box_norm, w, h)
        iu.draw_box(out, box_px, color=box_color, width=line_width)
        # 라벨: 박스 좌상단 위쪽. 외곽선 추가해 가독성 확보.
        _draw_id_label(out, id_str, box_px, font_size=font_size, bg=box_color)

    return out


def _draw_id_label(
    img: Image.Image,
    text: str,
    box_px: tuple[int, int, int, int],
    font_size: int,
    bg: str,
) -> None:
    x1, y1, _, _ = box_px
    draw = ImageDraw.Draw(img)
    font = iu._get_font(font_size)

    try:
        bbox = draw.textbbox((x1, y1), text, font=font)
    except AttributeError:
        bbox = (x1, y1, x1 + len(text) * font_size, y1 + font_size)

    pad = 2
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    # 박스 위쪽에 라벨 띄우기 (박스 상단에서 위로). 박스가 이미지 상단 근처면 안쪽에.
    if y1 - th - 2 * pad >= 0:
        label_y = y1 - th - 2 * pad
    else:
        label_y = y1 + 2
    label_x = x1

    bg_rect = (label_x, label_y, label_x + tw + 2 * pad, label_y + th + 2 * pad)
    draw.rectangle(bg_rect, fill=bg)
    draw.text((label_x + pad, label_y + pad), text, fill="white", font=font)
