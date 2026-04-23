"""Mistral OCR backend for doc_to_md skill.

흐름:
  1. PDF 를 `POST /v1/files` (purpose=ocr) 로 업로드 → file_id
  2. `GET /v1/files/{id}/url` 로 signed URL 취득
  3. `POST /v1/ocr` 호출 → 페이지별 {index, markdown, images[]}
  4. 페이지 markdown 을 연결하고, 이미지 base64 를 assets bytes 로 추출.
     markdown 내부의 `](<img-id>)` 참조는 `](<assets_root>/page_NNN/<img-id>)`
     로 rewrite.

왜 업로드 경로?  data URI 인라인은 작은 파일만 안정적이라, 단일 코드 경로로
안정성 우선. 추가 왕복은 파일 1개당 수백 ms 수준이라 무시 가능.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path
from typing import Callable

import requests

from codex_runner import CodexRunError, current_cancel_event, load_skill

logger = logging.getLogger("lecture_pipeline.doc_to_md")

_API_BASE = "https://api.mistral.ai/v1"
_MD_IMG_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")


LogCb = Callable[[str], None]


def _emit(log: LogCb | None, msg: str) -> None:
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass


def _is_cancelled() -> bool:
    ev = current_cancel_event.get()
    return ev is not None and ev.is_set()


def _raise_if_cancelled() -> None:
    if _is_cancelled():
        raise CodexRunError("doc_to_md: 사용자 취소")


# ── Mistral REST 래퍼 ──


def _upload_pdf(api_key: str, pdf_path: Path, timeout: int) -> str:
    url = f"{_API_BASE}/files"
    with pdf_path.open("rb") as f:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (pdf_path.name, f, "application/pdf")},
            data={"purpose": "ocr"},
            timeout=timeout,
        )
    if resp.status_code >= 300:
        raise CodexRunError(
            f"Mistral /files 업로드 실패 ({resp.status_code}): "
            f"{resp.text[:400]}"
        )
    data = resp.json()
    file_id = data.get("id")
    if not file_id:
        raise CodexRunError(f"Mistral /files 응답에 id 없음: {data}")
    return file_id


def _get_signed_url(api_key: str, file_id: str, timeout: int) -> str:
    url = f"{_API_BASE}/files/{file_id}/url"
    resp = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        params={"expiry": 24},
        timeout=timeout,
    )
    if resp.status_code >= 300:
        raise CodexRunError(
            f"Mistral /files/{file_id}/url 실패 ({resp.status_code}): "
            f"{resp.text[:400]}"
        )
    data = resp.json()
    signed = data.get("url")
    if not signed:
        raise CodexRunError(f"Mistral /files/url 응답에 url 없음: {data}")
    return signed


def _delete_file(api_key: str, file_id: str, timeout: int) -> None:
    try:
        requests.delete(
            f"{_API_BASE}/files/{file_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.warning("mistral file 삭제 실패 (%s): %s — 무시", file_id, exc)


def _run_ocr(
    api_key: str,
    model: str,
    signed_url: str,
    include_images: bool,
    timeout: int,
) -> dict:
    url = f"{_API_BASE}/ocr"
    payload = {
        "model": model,
        "document": {"type": "document_url", "document_url": signed_url},
        "include_image_base64": include_images,
    }
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    if resp.status_code >= 300:
        raise CodexRunError(
            f"Mistral /ocr 실패 ({resp.status_code}): {resp.text[:400]}"
        )
    return resp.json()


# ── 응답 → Markdown 조립 ──


def _rewrite_markdown_images(
    page_md: str,
    image_ids: set[str],
    rel_prefix: str,
) -> str:
    """페이지 markdown 의 ![alt](id) 를 ![alt](rel_prefix/id) 로 rewrite.

    Mistral 은 id (예: `img-0.jpeg`) 를 src 로 그대로 쓴다. 절대 URL / data URL
    은 건드리지 않는다.
    """
    def _sub(m: re.Match) -> str:
        alt, src = m.group(1), m.group(2)
        if src in image_ids:
            return f"![{alt}]({rel_prefix}/{src})"
        return m.group(0)

    return _MD_IMG_PATTERN.sub(_sub, page_md)


def _assemble(
    ocr_response: dict,
    assets_root: str,
    include_images: bool,
) -> tuple[str, dict[str, bytes], int]:
    """페이지별 markdown 을 연결하고 이미지 bytes 를 수집."""
    pages = ocr_response.get("pages", []) or []
    parts: list[str] = []
    assets: dict[str, bytes] = {}
    img_count = 0

    for page in pages:
        idx = int(page.get("index", 0))
        md = page.get("markdown", "") or ""
        page_prefix = f"{assets_root}/page_{idx + 1:03d}"
        image_ids: set[str] = set()

        if include_images:
            for img in page.get("images", []) or []:
                img_id = img.get("id")
                b64 = img.get("image_base64")
                if not img_id or not b64:
                    continue
                if isinstance(b64, str) and b64.startswith("data:") and "," in b64:
                    b64 = b64.split(",", 1)[1]
                try:
                    raw = base64.b64decode(b64)
                except (ValueError, TypeError):
                    continue
                assets[f"{page_prefix}/{img_id}"] = raw
                image_ids.add(img_id)
                img_count += 1

        if image_ids:
            md = _rewrite_markdown_images(md, image_ids, page_prefix)

        parts.append(md)

    doc_md = "\n\n".join(p.rstrip() for p in parts if p)
    if doc_md and not doc_md.endswith("\n"):
        doc_md += "\n"
    return doc_md, assets, img_count


# ── Entry ──


def run_doc_to_md(
    skill_dir: Path,
    input_paths: list[Path],
    log_callback: LogCb | None = None,
) -> dict[str, bytes]:
    """Composite skill entry. batch=false 이므로 PDF 1개 단위로 호출됨."""
    log = log_callback or (lambda m: None)

    pdfs = [p for p in input_paths if p.suffix.lower() == ".pdf"]
    if not pdfs:
        raise CodexRunError("doc_to_md: PDF 입력 필요")
    if len(pdfs) > 1:
        _emit(log, f"[doc_to_md] 경고: PDF {len(pdfs)}개 — 첫 번째만 처리")
    pdf_path = pdfs[0]

    api_key = os.environ.get("MISTRAL_API_KEY", "").strip()
    if not api_key:
        raise CodexRunError(
            "MISTRAL_API_KEY 환경변수 미설정. .env 에 추가하거나 "
            "프로세스 환경에 설정하세요."
        )

    cfg = load_skill(skill_dir).config
    model = cfg.get("mistral_model", "mistral-ocr-latest")
    include_images = bool(cfg.get("include_images", True))
    timeout = int(cfg.get("timeout", 600))

    size_mb = pdf_path.stat().st_size / 1024 / 1024
    _emit(log, f"[doc_to_md] 시작: {pdf_path.name} ({size_mb:.2f} MB)")
    _raise_if_cancelled()

    _emit(log, "[doc_to_md] 1/3 PDF 업로드 중...")
    file_id = _upload_pdf(api_key, pdf_path, timeout)
    _emit(log, f"[doc_to_md] 업로드 완료: {file_id}")

    try:
        _raise_if_cancelled()
        _emit(log, "[doc_to_md] 2/3 서명 URL 취득 중...")
        signed_url = _get_signed_url(api_key, file_id, timeout)

        _raise_if_cancelled()
        _emit(log, f"[doc_to_md] 3/3 OCR 호출 중 (model={model})...")
        resp = _run_ocr(api_key, model, signed_url, include_images, timeout)
    finally:
        _delete_file(api_key, file_id, timeout)

    pages = resp.get("pages", []) or []
    _emit(log, f"[doc_to_md] OCR 응답 수신 — {len(pages)} 페이지")

    assets_root = f"{pdf_path.stem}-assets"
    doc_md, assets, img_count = _assemble(resp, assets_root, include_images)

    usage = resp.get("usage_info", {}) or {}
    _emit(
        log,
        f"[doc_to_md] 완료 — pages={len(pages)}, images={img_count}, "
        f"usage={usage}",
    )

    outputs: dict[str, bytes] = {"doc.md": doc_md.encode("utf-8")}
    outputs.update(assets)
    return outputs
