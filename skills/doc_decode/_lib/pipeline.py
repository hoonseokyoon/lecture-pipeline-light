"""doc_decode composite orchestrator.

흐름:
  1. 입력 PDF 렌더 → checkpoint 에 페이지별 PNG 저장
  2. 샘플 페이지 기반 profile.md 생성
  3. 각 페이지에 대해 Codex agent 호출 (detect/annotate 두 도구만 줌)
     - 기존 state 가 있으면 inputs 에 seed
     - agent 출력 (objects.json, annotated.png, crops/, renders/) 을 checkpoint 로 복사
  4. 모든 페이지 끝나면 assemble.py 로 doc.md + structure.json 생성

Codex agent 는 scripts/detect.py + scripts/annotate.py 만 호출.
이 둘은 내부에서 Gemini (google-genai) 를 직접 호출한다.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Callable

from codex_runner import (
    CodexRunError,
    current_cancel_event,
    load_skill,
    run_codex_task,
)
from venv_manager import VenvManager

from _lib import image_utils as iu
from _lib import pdf_utils as pu
from _lib.assemble import assemble
from _lib.checkpoint import CheckpointDir, compute_run_id
from _lib.direct import process_page_direct
from _lib.profile import generate_profile
from _lib.prompts import build_page_agent_prompt


_venv_manager = VenvManager()


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
        raise CodexRunError("doc_decode: 사용자 취소")


# ── 페이지 agent 호출 ──

def _stage_page_inputs(
    page_idx: int,
    page_img_bytes: bytes,
    profile_text: str,
    page_ckpt: Path,
) -> dict[str, bytes]:
    """agent 호출용 inputs dict 구성.

    - page.png        (필수)
    - profile.md
    - objects.json    (page 번호를 주입해 항상 stage 한다)

    seed 가 `state["page"]=None` 인 채로 detect.py 에 넘어가면 모든 페이지가
    `p1_NNN` id prefix 를 발급해 충돌하므로 여기서 page 번호를 강제 주입한다.
    crops/ 는 detect/annotate 가 `outputs/crops` 만 참조해 seed 로 넘겨도
    읽히지 않으므로 넘기지 않는다.
    """
    inputs: dict[str, bytes] = {
        "page.png": page_img_bytes,
        "profile.md": profile_text.encode("utf-8"),
    }
    obj_file = page_ckpt / "objects.json"
    seed: dict
    if obj_file.exists():
        try:
            seed = json.loads(obj_file.read_text(encoding="utf-8"))
            if not isinstance(seed, dict):
                seed = {"objects": []}
        except json.JSONDecodeError:
            seed = {"objects": []}
    else:
        seed = {"objects": []}
    seed["page"] = page_idx
    seed.setdefault("objects", [])
    inputs["objects.json"] = json.dumps(
        seed, ensure_ascii=False,
    ).encode("utf-8")
    return inputs


def _save_page_outputs(
    outputs: dict[str, bytes],
    page_ckpt: Path,
) -> dict:
    """agent 가 반환한 outputs 를 checkpoint 에 저장 + parsed state 반환.

    outputs 키 예시:
      - objects.json
      - annotated.png
      - crops/<id>.png
      - renders/<id>.png
    """
    (page_ckpt / "crops").mkdir(parents=True, exist_ok=True)
    (page_ckpt / "renders").mkdir(parents=True, exist_ok=True)

    for name, data in outputs.items():
        dest = page_ckpt / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    state_path = page_ckpt / "objects.json"
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"objects": []}
    return {"objects": []}


def _is_page_complete(page_ckpt: Path) -> bool:
    """해당 페이지가 이미 '충분히' 처리됐는지 (빠른 재실행 판단).

    기준:
    - objects.json 존재 + `page_agent_ran=True` (agent 가 한 번 완주함)
    - 모든 객체 status 가 annotated / verified

    blank 페이지(objects 0개) 도 `page_agent_ran=True` 면 complete 로 간주 —
    이 플래그는 `_run_page_agent` 성공 시 pipeline 이 세팅한다.
    """
    f = page_ckpt / "objects.json"
    if not f.exists():
        return False
    try:
        state = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if not state.get("page_agent_ran", False):
        return False
    objs = state.get("objects", []) or []
    for o in objs:
        status = o.get("status")
        if status not in ("annotated", "verified"):
            return False
    return True


def _run_page_agent(
    skill_dir: Path,
    page_idx: int,
    page_total: int,
    page_ckpt: Path,
    page_img_bytes: bytes,
    profile_text: str,
    cfg: dict,
    log: LogCb,
    venv_bin: Path | None,
) -> dict:
    """페이지 1장에 대해 Codex agent 호출. 결과 state (objects.json 파싱된 dict) 반환.

    실패 시 CodexRunError 를 로그로 삼키고 기존 state 반환 (있으면).
    """
    existing_state = {}
    if (page_ckpt / "objects.json").exists():
        try:
            existing_state = json.loads(
                (page_ckpt / "objects.json").read_text(encoding="utf-8"),
            )
        except json.JSONDecodeError:
            existing_state = {}

    existing_objs = existing_state.get("objects", []) or []

    prompt = build_page_agent_prompt(
        page_idx=page_idx,
        page_total=page_total,
        profile_text=profile_text,
        existing_objects=existing_objs,
        max_attempts=int(cfg.get("annotate_max_attempts", 4)),
        agent_turns=int(cfg.get("agent_turns_per_page", 10)),
    )

    inputs = _stage_page_inputs(page_idx, page_img_bytes, profile_text, page_ckpt)
    scripts_dir = skill_dir / "scripts"

    # strict 로 필수 2개를 agent 에게 알리되, allow_extra_outputs 로 crops/renders
    # 와일드카드 파일도 함께 회수.
    expected = ["objects.json", "annotated.png"]

    _emit(log, f"[page {page_idx}/{page_total}] Codex agent 호출 "
               f"(existing {len(existing_objs)} objs)")

    try:
        outputs = run_codex_task(
            prompt=prompt,
            inputs=inputs,
            scripts_dir=scripts_dir,
            scripts_ignore=("requirements.txt",),
            expected_outputs=expected,
            allow_extra_outputs=True,
            model=cfg.get("model", "gpt-5.4"),
            reasoning_effort=cfg.get("reasoning_effort", "medium"),
            timeout=int(cfg.get("timeout", 1200)),
            venv_bin=venv_bin,
            network_access=True,  # detect/annotate 스크립트가 Gemini 호출
        )
    except CodexRunError as exc:
        if _is_cancelled():
            # 사용자 취소는 per-page fallback 으로 삼키면 안 됨 — 루프 전체 중단.
            _emit(log, f"[page {page_idx}] 취소 감지 — 중단")
            raise
        _emit(log, f"[page {page_idx}] agent 실패 — 기존 state 유지: {exc}")
        return existing_state

    state = _save_page_outputs(outputs, page_ckpt)
    n_obj = len(state.get("objects", []) or [])
    _emit(log, f"[page {page_idx}] 완료 — {n_obj} 객체")
    return state


# ── 메인 ──

_RUN_ID_CFG_KEYS = (
    "gemini_model",
    "render_dpi",
    "max_image_side",
    "iou_threshold",
    "annotate_max_attempts",
    "profile_sample_pages",
    "agent_turns_per_page",
    "backend",
    "model",
    "reasoning_effort",
    "crop_padding_px",
    "review_max_rounds",
    "review_actions_per_round",
    "overlap_warn_px",
    "review_phase2_min_rounds",
    "review_phase2_max_rounds",
)


def _cfg_run_keys(cfg: dict) -> dict:
    return {k: cfg.get(k) for k in _RUN_ID_CFG_KEYS}


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    """PNG bytes 에서 (w, h) 추출. 실패 시 None."""
    import io
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:
        return None


def _collect_page_assets(
    page_idx: int,
    page_ckpt: Path,
    assets_root: str,
) -> dict[str, bytes]:
    """페이지별 crops/ + annotated.png 를 output dict 에 담을 키로 수집."""
    out: dict[str, bytes] = {}
    page_prefix = f"{assets_root}/pages/page_{page_idx:03d}"
    crops_dir = page_ckpt / "crops"
    if crops_dir.is_dir():
        for p in sorted(crops_dir.glob("*.png")):
            out[f"{page_prefix}/crops/{p.name}"] = p.read_bytes()
    annotated = page_ckpt / "annotated.png"
    if annotated.exists():
        out[f"{page_prefix}/annotated.png"] = annotated.read_bytes()
    return out


def _process_pdf(
    pdf_path: Path,
    skill_dir: Path,
    cfg: dict,
    log: LogCb,
) -> tuple[bytes, bytes, dict[str, bytes], dict]:
    """한 PDF 처리. (doc.md bytes, structure.json bytes, assets, diag) 반환."""
    _raise_if_cancelled()
    run_id = compute_run_id(pdf_path, _cfg_run_keys(cfg))
    ckpt = CheckpointDir(run_id)
    _emit(log, f"[doc_decode] {pdf_path.name} run_id={run_id} → {ckpt.path}")

    # 스킬 venv 확보 (scripts/requirements.txt 기반). 재빌드가 없으면 캐시 hit.
    _emit(log, "[venv] 스킬 venv 준비 중...")
    venv_bin = _venv_manager.ensure(skill_dir)
    _emit(log, f"[venv] {venv_bin or 'venv 불필요'}")

    # scripts 측 Gemini 호출에 RPM 제한을 전파 (cross-process file lock 기반).
    rpm = int(cfg.get("gemini_rpm", 0) or 0)
    os.environ["DOC_DECODE_GEMINI_RPM"] = str(max(0, rpm))

    # 자산 디렉토리 — 같은 workspace 에 여러 PDF 가 들어가도 충돌 없게 stem prefix.
    assets_root = f"{pdf_path.stem}-assets"

    # 1) 렌더 (캐시 재사용)
    dpi = int(cfg.get("render_dpi", 200))
    max_side = int(cfg.get("max_image_side", 2000))

    page_count = pu.get_page_count(pdf_path)
    _emit(log, f"[render] {page_count} 페이지")

    pages: list[tuple[int, bytes]] = []
    need_render = False
    for i in range(1, page_count + 1):
        pd = ckpt.page_dir(i)
        png = pd / "page.png"
        if not png.exists():
            need_render = True
            break

    if need_render:
        _emit(log, "[render] 페이지 PNG 생성 중...")
        imgs = pu.render_pages(pdf_path, dpi=dpi, max_side=max_side)
        if len(imgs) != page_count:
            # page_count 와 imgs 수가 다르면 enumerate 가 어긋나 일부 페이지를 놓치거나
            # 과도한 루프를 돌 수 있다 — 실제 렌더 수를 진실로 채택.
            _emit(
                log,
                f"[render] 경고: page_count={page_count} vs imgs={len(imgs)} — "
                f"{len(imgs)} 기준으로 진행",
            )
            page_count = len(imgs)
        for i, img in enumerate(imgs, start=1):
            pd = ckpt.page_dir(i)
            png = pd / "page.png"
            if not png.exists():
                img.save(png, format="PNG")
            pages.append((i, png.read_bytes()))
    else:
        _emit(log, "[render] cache hit — 모든 페이지 PNG 존재")
        for i in range(1, page_count + 1):
            png = ckpt.page_dir(i) / "page.png"
            pages.append((i, png.read_bytes()))

    _raise_if_cancelled()

    # 2) Profile (cache)
    profile_file = ckpt.file("profile.md")
    if profile_file.exists():
        profile_text = profile_file.read_text(encoding="utf-8")
        _emit(log, f"[profile] cache hit ({len(profile_text)} 자)")
    else:
        # 샘플 페이지를 PIL 로 재로딩 (bytes → Image).
        import io
        from PIL import Image
        pil_pages = []
        for _, data in pages:
            img = Image.open(io.BytesIO(data))
            img.load()
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            pil_pages.append(img)
        profile_text = generate_profile(pil_pages, cfg, log)
        profile_file.write_text(profile_text or "", encoding="utf-8")

    # 3) 페이지별 agent 루프
    page_states: list[tuple[int, dict]] = []
    for page_idx, png_bytes in pages:
        _raise_if_cancelled()
        pd = ckpt.page_dir(page_idx)

        if _is_page_complete(pd):
            _emit(log, f"[page {page_idx}/{page_count}] 이미 완료 — skip")
            state = json.loads((pd / "objects.json").read_text(encoding="utf-8"))
        else:
            _emit(log, f"[page {page_idx}/{page_count}] 직접 파이프라인 실행")
            try:
                state = process_page_direct(
                    page_idx=page_idx,
                    page_ckpt=pd,
                    page_img_bytes=png_bytes,
                    profile_text=profile_text,
                    cfg=cfg,
                    log=log,
                )
            except CodexRunError as exc:
                if _is_cancelled():
                    raise
                _emit(log, f"[page {page_idx}] 실패 — 부분 state 유지: {exc}")
                # 부분 진행된 상태 그대로 로드.
                try:
                    state = json.loads(
                        (pd / "objects.json").read_text(encoding="utf-8"),
                    )
                except (OSError, json.JSONDecodeError):
                    state = {"page": page_idx, "objects": []}

            state.setdefault("page", page_idx)
            state["page_agent_ran"] = True
            n_obj = len(state.get("objects", []) or [])
            _emit(log, f"[page {page_idx}] 완료 — {n_obj} 객체")
            (pd / "objects.json").write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        page_states.append((page_idx, state))

    # 4) 조립
    doc_md, structure = assemble(
        page_states, assets_root=assets_root, log=log,
    )
    ckpt.write_text("doc.md", doc_md)
    ckpt.write_text(
        "structure.json",
        json.dumps(structure, ensure_ascii=False, indent=2),
    )

    # 5) assets 수집 — doc.md 의 figure 링크와 매칭.
    assets: dict[str, bytes] = {}
    for page_idx, _ in page_states:
        pd = ckpt.page_dir(page_idx)
        assets.update(_collect_page_assets(page_idx, pd, assets_root))

    diag = {
        "run_id": run_id,
        "page_count": page_count,
        "total_objects": sum(
            len(s.get("objects", []) or []) for _, s in page_states
        ),
        "asset_count": len(assets),
    }
    return (
        doc_md.encode("utf-8"),
        json.dumps(structure, ensure_ascii=False, indent=2).encode("utf-8"),
        assets,
        diag,
    )


def run_doc_decode(
    skill_dir: Path,
    input_paths: list[Path],
    log_callback: LogCb | None = None,
) -> dict[str, bytes]:
    """Composite entry. `batch: false` 이므로 input_paths 는 PDF 1개 단위로 들어옴."""
    log = log_callback or (lambda m: None)

    pdfs = [p for p in input_paths if p.suffix.lower() == ".pdf"]
    if not pdfs:
        raise CodexRunError("doc_decode: PDF 입력 필요")
    if len(pdfs) > 1:
        _emit(log, f"[doc_decode] 경고: PDF {len(pdfs)}개 들어옴 — 첫 번째만 처리 "
                   "(batch=false 로 per-file 호출을 권장)")
    pdf_path = pdfs[0]

    cfg = load_skill(skill_dir).config
    _emit(log, f"[doc_decode] 시작: {pdf_path.name}")

    doc_md_bytes, structure_bytes, assets, diag = _process_pdf(
        pdf_path=pdf_path,
        skill_dir=skill_dir,
        cfg=cfg,
        log=log,
    )
    _emit(
        log,
        f"[doc_decode] 완료 — pages={diag['page_count']}, "
        f"objects={diag['total_objects']}, assets={diag['asset_count']}",
    )
    outputs: dict[str, bytes] = {
        "doc.md": doc_md_bytes,
        "structure.json": structure_bytes,
    }
    outputs.update(assets)
    return outputs
