"""Step 0.5: 이미지 중심 슬라이드의 텍스트 보강 (Gemini Vision).

pdfplumber 텍스트 추출 결과가 threshold 미만인 페이지를 식별하고,
해당 페이지를 이미지로 렌더한 뒤 Gemini API로 슬라이드 내용을 텍스트화.

결과는 slides_textify의 _raw/ 대체 파일로 사용되어
anchors/brief 품질을 높인다.
"""

import base64
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Callable

import fitz  # pymupdf
import pdfplumber

# google-genai SDK (google.genai)
from google import genai
from google.genai import types

DEFAULT_CHAR_THRESHOLD = 40
DEFAULT_GEMINI_MODEL = "gemma-4-31b-it"
DEFAULT_GEMINI_RPM = 0  # 0 = 제한 없음
RENDER_DPI = 200

VISION_PROMPT = """\
이 슬라이드 이미지의 내용을 텍스트로 변환하세요.

규칙:
- 이미지에 보이는 모든 텍스트, 라벨, 수식, 범례를 그대로 옮기세요.
- 도표/다이어그램이 있으면 구조를 간결하게 서술하세요 (노드, 화살표 방향, 관계).
- 그래프가 있으면 축 이름, 범례, 주요 데이터 포인트를 기술하세요.
- 언어는 슬라이드 원본 언어를 따르세요.
- 마크다운이나 특수 포맷 없이 plain text로 출력하세요.
- 슬라이드 내용만 출력하세요. 부가 설명이나 인사말 불필요."""


class _RateLimiter:
    """Sliding window RPM 제한기. rpm=0이면 제한 없음."""

    def __init__(self, rpm: int):
        self._rpm = rpm
        self._timestamps: deque[float] = deque()

    def wait(self) -> None:
        if self._rpm <= 0:
            return
        now = time.monotonic()
        # 1분 이상 된 타임스탬프 제거
        while self._timestamps and now - self._timestamps[0] >= 60.0:
            self._timestamps.popleft()
        # 현재 윈도우에서 rpm 초과 시 대기
        if len(self._timestamps) >= self._rpm:
            sleep_for = 60.0 - (now - self._timestamps[0])
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._timestamps.popleft()
        self._timestamps.append(time.monotonic())


def _emit(log_callback: Callable[[str], None] | None, msg: str) -> None:
    if log_callback is None:
        return
    try:
        log_callback(msg)
    except Exception:
        pass


def _load_gemini_client() -> genai.Client:
    """GEMINI_API_KEY 환경변수로 클라이언트 생성."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY 환경변수가 설정되지 않음. "
            ".env 파일에 GEMINI_API_KEY=... 를 추가하세요."
        )
    return genai.Client(api_key=api_key)


def extract_page_texts(pdf_path: Path) -> list[str]:
    """pdfplumber로 각 페이지 텍스트 추출. 인덱스 0 = 페이지 1."""
    texts: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            texts.append(page.extract_text() or "")
    return texts


def render_page_image(pdf_path: Path, page_index: int, dpi: int = RENDER_DPI) -> bytes:
    """pymupdf로 특정 페이지를 PNG 바이트로 렌더."""
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[page_index]
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)
        return pix.tobytes("png")
    finally:
        doc.close()


def describe_slide_image(
    client: genai.Client,
    image_bytes: bytes,
    model: str = DEFAULT_GEMINI_MODEL,
) -> str:
    """Gemini에 슬라이드 이미지를 보내 텍스트 설명을 받는다."""
    response = client.models.generate_content(
        model=model,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                    types.Part.from_text(text=VISION_PROMPT),
                ],
            )
        ],
    )
    return response.text or ""


def enrich_pdf_pages(
    pdf_path: Path,
    output_dir: Path,
    *,
    char_threshold: int = DEFAULT_CHAR_THRESHOLD,
    gemini_model: str = DEFAULT_GEMINI_MODEL,
    gemini_rpm: int = DEFAULT_GEMINI_RPM,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    """PDF의 각 페이지 텍스트를 추출하고, 부족한 페이지는 Gemini로 보강.

    Args:
        pdf_path: PDF 파일 경로.
        output_dir: enriched txt 파일들을 저장할 디렉토리.
        char_threshold: 이 글자 수 미만이면 Gemini 호출 대상.
        gemini_model: Gemini 모델명.
        gemini_rpm: 분당 최대 요청 수. 0이면 제한 없음.
        log_callback: 로그 콜백.

    Returns:
        {
            "pages": [
                {"page": 1, "chars": 120, "enriched": False},
                {"page": 2, "chars": 5,   "enriched": True},
                ...
            ],
            "total": 전체 페이지 수,
            "enriched_count": Gemini 보강된 페이지 수,
        }
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    page_texts = extract_page_texts(pdf_path)
    total = len(page_texts)

    # threshold 미만인 페이지 식별
    low_text_indices: list[int] = []
    for i, text in enumerate(page_texts):
        stripped = text.strip()
        if len(stripped) < char_threshold:
            low_text_indices.append(i)

    _emit(
        log_callback,
        f"[vision] {pdf_path.name}: {total}페이지 중 "
        f"{len(low_text_indices)}페이지 텍스트 부족 (threshold={char_threshold}자)",
    )

    # Gemini 클라이언트 생성 (보강 대상이 있을 때만)
    client: genai.Client | None = None
    rate_limiter = _RateLimiter(gemini_rpm)
    if low_text_indices:
        try:
            client = _load_gemini_client()
        except RuntimeError as exc:
            _emit(log_callback, f"[vision] Gemini 사용 불가 — skip: {exc}")
            low_text_indices = []  # 보강 대상 없음으로 처리

    if gemini_rpm > 0 and low_text_indices:
        _emit(log_callback, f"[vision] RPM 제한: {gemini_rpm} req/min")

    pages_meta: list[dict] = []
    enriched_count = 0

    for i, text in enumerate(page_texts):
        page_num = i + 1  # 1-based
        out_file = output_dir / f"page_{page_num:03d}.txt"
        stripped = text.strip()
        is_low = i in low_text_indices

        if is_low and client is not None:
            _emit(log_callback, f"[vision] page {page_num} — Gemini 호출 중...")
            try:
                rate_limiter.wait()
                img_bytes = render_page_image(pdf_path, i)
                description = describe_slide_image(client, img_bytes, gemini_model)
                # pdfplumber 텍스트가 조금이라도 있으면 병합
                if stripped:
                    final_text = f"{stripped}\n\n---\n[이미지 내용]\n{description}"
                else:
                    final_text = description
                out_file.write_text(final_text, encoding="utf-8")
                enriched_count += 1
                pages_meta.append({
                    "page": page_num,
                    "chars": len(stripped),
                    "enriched": True,
                })
                _emit(log_callback, f"[vision] page {page_num} — 보강 완료 ({len(description)}자)")
                continue
            except Exception as exc:
                _emit(
                    log_callback,
                    f"[vision] page {page_num} — Gemini 실패, 원본 사용: {exc}",
                )
                # fallthrough: 원본 텍스트 사용

        out_file.write_text(text, encoding="utf-8")
        pages_meta.append({
            "page": page_num,
            "chars": len(stripped),
            "enriched": False,
        })

    _emit(
        log_callback,
        f"[vision] 완료 — {enriched_count}/{total}페이지 보강",
    )

    return {
        "pages": pages_meta,
        "total": total,
        "enriched_count": enriched_count,
    }
