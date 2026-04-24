"""Asset 디렉토리 preprocessing: 캡션 추출 + 임베딩.

흐름:
  1. 이미지 파일 스캔
  2. 캡션 확보 (우선: Mistral MD 에서 regex 회수, 차선: Gemini Flash 캡션)
  3. [image bytes, caption] interleaved 로 gemini-embedding-2 호출 → 벡터
  4. `.captions.json`, `.embeddings.npz`, `.preprocess_meta.json` 으로 캐시

캐시 유효성: assets 디렉토리의 파일 목록 + mtime 해시. 변경 시 재계산.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Callable

import numpy as np

from codex_runner import CodexRunError, current_cancel_event

from _lib.gemini_embed import caption_image, embed_multimodal

logger = logging.getLogger("lecture_pipeline.pick_figures_for_slide.preprocess")

LogCb = Callable[[str], None]

_SUPPORTED_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

# Mistral MD 의 ![](<path>) 또는 ![alt](...) 패턴에서 alt + 주변 Figure caption 추출
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")


def _emit(log, msg):
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass


def _raise_if_cancelled():
    ev = current_cancel_event.get()
    if ev is not None and ev.is_set():
        raise CodexRunError("pick_figures_for_slide (preprocess): 사용자 취소")


def _scan_images(assets_dir: Path) -> list[Path]:
    imgs: list[Path] = []
    for ext in _SUPPORTED_EXTS:
        imgs.extend(assets_dir.rglob(f"*{ext}"))
    imgs.sort()
    return imgs


def _dir_signature(images: list[Path]) -> str:
    """파일 목록 + mtime 기반 해시 — 캐시 무효화 키."""
    parts = []
    for p in images:
        try:
            st = p.stat()
            parts.append(f"{p.name}:{st.st_size}:{int(st.st_mtime)}")
        except OSError:
            continue
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _extract_captions_from_md(md_text: str) -> dict[str, str]:
    """Mistral MD 에서 `![caption](img-id)` 의 alt 와 주변 'Figure N' 줄 회수.

    Mistral OCR 산출 MD 의 실제 패턴 (확인 필요) 을 두 가지 경로로 수집:
      1) alt 텍스트 (`![...](...)` 의 대괄호 내부) — 보통 비어있거나 짧음
      2) 이미지 이전/이후 줄에 있는 "Figure N" 같은 캡션 라인
    반환: {filename (stem 부분): caption}
    """
    out: dict[str, str] = {}
    if not md_text:
        return out

    lines = md_text.splitlines()
    for i, line in enumerate(lines):
        for m in _MD_IMAGE_RE.finditer(line):
            alt = (m.group(1) or "").strip()
            path = m.group(2).strip()
            # path 의 파일명 부분 추출
            fname = path.split("/")[-1].split("\\")[-1]
            # 앞뒤 1~2 줄 텍스트 에서 Figure 표기 찾기
            ctx_lines = []
            for j in (i - 2, i - 1, i + 1, i + 2):
                if 0 <= j < len(lines):
                    ctx_lines.append(lines[j].strip())
            fig_caption = ""
            for cl in ctx_lines:
                if cl.lower().startswith(("figure ", "fig.", "fig ")):
                    fig_caption = cl
                    break
            caption = " | ".join(x for x in [alt, fig_caption] if x)
            if caption and fname:
                out[fname] = caption
    return out


def preprocess_assets(
    *,
    assets_dir: Path,
    md_path: Path | None,
    output_dim: int = 1536,
    force_refresh: bool = False,
    log_cb: LogCb | None = None,
) -> dict:
    """Assets 캡션 + 임베딩 계산, 캐시 관리.

    반환:
        {
          "filenames": [...],              # 파일명 리스트 (assets_dir 기준 상대)
          "captions": {filename: caption},
          "embeddings": np.ndarray shape (N, D),
          "meta": {"signature": ..., "output_dim": D, "source": {...}}
        }
    """
    images = _scan_images(assets_dir)
    if not images:
        _emit(log_cb, f"[preprocess] 이미지 0개: {assets_dir}")
        return {"filenames": [], "captions": {}, "embeddings": np.zeros((0, output_dim), dtype=np.float32), "meta": {}}

    signature = _dir_signature(images)
    cache_captions = assets_dir / ".captions.json"
    cache_embeds = assets_dir / ".embeddings.npz"
    cache_meta = assets_dir / ".preprocess_meta.json"

    # ── 캐시 확인 ──
    if (not force_refresh) and cache_meta.exists() and cache_captions.exists() and cache_embeds.exists():
        try:
            meta = json.loads(cache_meta.read_text(encoding="utf-8"))
            if meta.get("signature") == signature and meta.get("output_dim") == output_dim:
                captions = json.loads(cache_captions.read_text(encoding="utf-8"))
                with np.load(cache_embeds) as npz:
                    embeddings = npz["embeddings"]
                    filenames = list(npz["filenames"])
                _emit(log_cb, f"[preprocess] cache hit: {len(filenames)}개")
                return {
                    "filenames": filenames,
                    "captions": captions,
                    "embeddings": embeddings,
                    "meta": meta,
                }
        except Exception as exc:
            _emit(log_cb, f"[preprocess] cache 무효 — 재계산: {exc}")

    # ── 캡션 1차: MD 에서 회수 ──
    md_captions: dict[str, str] = {}
    if md_path is not None and md_path.exists():
        try:
            md_text = md_path.read_text(encoding="utf-8")
            md_captions = _extract_captions_from_md(md_text)
            _emit(log_cb, f"[preprocess] MD 에서 회수한 캡션: {len(md_captions)}개")
        except Exception as exc:
            _emit(log_cb, f"[preprocess] MD 캡션 회수 실패: {exc}")

    # ── 캡션 2차: Gemini Flash vision (MD 에 없는 것만) ──
    captions: dict[str, str] = {}
    n_flash_called = 0
    for i, img_path in enumerate(images):
        _raise_if_cancelled()
        rel_name = img_path.name  # 단일 depth 기준 매칭 (MD 에도 basename 으로 들어감)
        cap = md_captions.get(rel_name) or md_captions.get(img_path.stem)
        if cap:
            captions[rel_name] = cap
            continue
        # Flash 로 생성
        try:
            mime = _MIME_MAP.get(img_path.suffix.lower(), "image/jpeg")
            img_bytes = img_path.read_bytes()
            cap = caption_image(
                image_bytes=img_bytes,
                mime_type=mime,
                context_hint="academic ML/statistics textbook figure",
            )
            n_flash_called += 1
            if (i + 1) % 10 == 0:
                _emit(log_cb, f"[preprocess] 캡션 진행 {i+1}/{len(images)}")
        except Exception as exc:
            cap = f"(captioning failed: {exc})"
            _emit(log_cb, f"[preprocess] 캡션 실패 {img_path.name}: {exc}")
        captions[rel_name] = cap

    _emit(log_cb, f"[preprocess] 캡션 완료 — Flash 호출 {n_flash_called}/{len(images)}")

    # ── 임베딩 ──
    embeddings: list[list[float]] = []
    for i, img_path in enumerate(images):
        _raise_if_cancelled()
        rel_name = img_path.name
        mime = _MIME_MAP.get(img_path.suffix.lower(), "image/jpeg")
        try:
            vec = embed_multimodal(
                image_bytes=img_path.read_bytes(),
                mime_type=mime,
                caption=captions.get(rel_name, ""),
                output_dim=output_dim,
            )
            embeddings.append(vec)
        except Exception as exc:
            _emit(log_cb, f"[preprocess] 임베딩 실패 {rel_name}: {exc}")
            embeddings.append([0.0] * output_dim)  # zero vector → 매칭 안 됨
        if (i + 1) % 10 == 0:
            _emit(log_cb, f"[preprocess] 임베딩 진행 {i+1}/{len(images)}")

    emb_arr = np.array(embeddings, dtype=np.float32)
    filenames = [p.name for p in images]

    # ── 캐시 저장 ──
    try:
        cache_captions.write_text(
            json.dumps(captions, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        np.savez_compressed(
            cache_embeds,
            embeddings=emb_arr,
            filenames=np.array(filenames, dtype=object),
        )
        meta = {
            "signature": signature,
            "output_dim": output_dim,
            "n_images": len(filenames),
            "source": {"md_path": str(md_path) if md_path else None},
        }
        cache_meta.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _emit(log_cb, f"[preprocess] cache 저장 완료 (signature={signature})")
    except Exception as exc:
        _emit(log_cb, f"[preprocess] cache 저장 실패 (무시하고 진행): {exc}")

    return {
        "filenames": filenames,
        "captions": captions,
        "embeddings": emb_arr,
        "meta": {
            "signature": signature,
            "output_dim": output_dim,
            "n_images": len(filenames),
        },
    }
