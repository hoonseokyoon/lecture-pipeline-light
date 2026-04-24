"""image_generation composite skill entry + 라이브러리 API."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from codex_runner import CodexRunError, current_cancel_event, load_skill

from _lib import cache as cache_mod
from _lib.gemini_image import classify_route, set_rpm
from _lib.model_backend import run_model_backend
from _lib.schema import FigureMeta, FigureSpec
from _lib.script_backend import run_script_backend

logger = logging.getLogger("lecture_pipeline.image_generation")

LogCb = Callable[[str], None]


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
        raise CodexRunError("image_generation: 사용자 취소")


@dataclass
class GenerateResult:
    outputs: dict[str, bytes]
    meta: FigureMeta
    cache_key: str


def _generate_core(
    spec: FigureSpec,
    workspace: Path,
    cfg: dict,
    log_cb: LogCb | None = None,
) -> GenerateResult:
    """한 figure 생성. caching → routing → backend 분기."""
    _raise_if_cancelled()

    # RPM 설정 (있으면)
    set_rpm(int(cfg.get("rpm", 0)))

    # ── 1. cache 조회 ──
    spec_dict = spec.model_dump()
    cache_key = cache_mod.compute_key(spec_dict)
    enable_cache = bool(cfg.get("enable_cache", True))

    expected_files = _expected_files(spec)
    if enable_cache:
        hit = cache_mod.lookup(workspace, cache_key, expected_files)
        if hit is not None:
            _emit(log_cb, f"[image_gen] cache hit: {cache_key}")
            # meta 도 있으면 복원
            meta_bytes = hit.get("meta.json", b"{}")
            try:
                meta_dict = json.loads(meta_bytes.decode("utf-8"))
            except Exception:
                meta_dict = {}
            meta_dict["cache_hit"] = True
            meta_dict["cache_key"] = cache_key
            meta = FigureMeta.model_validate(meta_dict)
            return GenerateResult(outputs=hit, meta=meta, cache_key=cache_key)
        _emit(log_cb, f"[image_gen] cache miss: {cache_key}")

    # ── 2. routing ──
    mode = spec.mode
    router_model = cfg.get("router_model", "gemini-flash-latest")
    if mode == "auto":
        decision = classify_route(
            hint=spec.hint,
            context=spec.context,
            model=router_model,
            log_cb=log_cb,
        )
        mode = decision.route
        _emit(log_cb, f"[image_gen] auto → {mode} (reason: {decision.reason[:80]})")

    # ── 3. backend 호출 ──
    if mode == "script":
        outputs, meta_partial = run_script_backend(spec, cfg, log_cb=log_cb)
    elif mode == "model":
        if spec.format != "png":
            # TikZ/SVG 요청인데 model 로 라우팅됐으면 script 로 강제 fallback
            _emit(log_cb, f"[image_gen] format={spec.format} 이라 model→script 강제 전환")
            mode = "script"
            outputs, meta_partial = run_script_backend(spec, cfg, log_cb=log_cb)
        else:
            outputs, meta_partial = run_model_backend(spec, cfg, log_cb=log_cb)
    else:
        raise CodexRunError(f"알 수 없는 mode: {mode}")

    # ── 4. meta 조립 ──
    meta = FigureMeta(
        mode_used=mode,
        format=spec.format,
        backend=meta_partial.get("backend", ""),
        prompt_snapshot=meta_partial.get("prompt_snapshot", "")[:1000],
        retries=meta_partial.get("retries", 0),
        duration_s=meta_partial.get("duration_s", 0.0),
        cache_hit=False,
        cache_key=cache_key,
    )
    outputs["meta.json"] = meta.model_dump_json(indent=2).encode("utf-8")

    # ── 5. cache 저장 ──
    if enable_cache:
        cache_mod.store(workspace, cache_key, outputs)
        _emit(log_cb, f"[image_gen] cache stored: {cache_key}")

    return GenerateResult(outputs=outputs, meta=meta, cache_key=cache_key)


def _expected_files(spec: FigureSpec) -> list[str]:
    """format·mode 에 따라 산출물 파일명 예상."""
    if spec.format == "png":
        return ["fig.png", "meta.json"]
    elif spec.format == "tex":
        return ["fig.source.tex", "meta.json"]
    return ["meta.json"]


# ─────────────────────────────────────────────────────────────
# Composite skill entry
# ─────────────────────────────────────────────────────────────


def run_image_generation(
    skill_dir: Path,
    input_paths: list[Path],
    log_callback: LogCb | None = None,
) -> dict[str, bytes]:
    """composite skill entry — JSON spec 파일 하나를 입력으로 받는다."""
    log = log_callback

    # JSON spec 파일 탐색
    specs = [p for p in input_paths if p.suffix.lower() == ".json"]
    if not specs:
        raise CodexRunError("image_generation: .json figure spec 파일 필요")
    spec_path = specs[0]
    try:
        spec = FigureSpec.model_validate_json(
            spec_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise CodexRunError(f"spec JSON 파싱 실패 ({spec_path.name}): {exc}") from exc

    # workspace 추정: spec_path.parent (cache 위치 결정용)
    workspace = spec_path.parent
    cfg = load_skill(skill_dir).config

    _emit(log, f"[image_gen] 시작 — hint='{spec.hint[:60]}' format={spec.format} mode={spec.mode}")
    result = _generate_core(spec, workspace, cfg, log_cb=log)

    # composite 출력 규약: dict[str, bytes]. OutputWriter 가 rename_map 으로 저장.
    _emit(log, f"[image_gen] 완료 — {len(result.outputs)}개 파일 반환")
    return result.outputs


# ─────────────────────────────────────────────────────────────
# 라이브러리 API (pick_figures_for_slide 에서 사용)
# ─────────────────────────────────────────────────────────────


def generate_figure(
    *,
    hint: str,
    context: str = "",
    style: str = "",
    format: str = "png",
    mode: str = "auto",
    seed: int = 42,
    workspace: Path,
    skill_dir: Path | None = None,
    log_cb: LogCb | None = None,
) -> GenerateResult:
    """라이브러리 호출용 — pick_figures_for_slide 가 사용.

    workspace: cache 위치 및 generated_figures/ 산출물 루트.
    skill_dir: config.json 로드용. None 이면 기본값 사용.
    """
    spec = FigureSpec(
        hint=hint,
        context=context,
        style=style,
        format=format,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
        seed=seed,
    )
    if skill_dir is not None:
        cfg = load_skill(skill_dir).config
    else:
        cfg = {}

    return _generate_core(spec, workspace, cfg, log_cb=log_cb)
