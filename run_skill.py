"""Headless CLI runner — GUI 없이 스킬을 실행한다.

사용 예:
    python run_skill.py doc_decode path/to/doc.pdf
    python run_skill.py doc_decode a.pdf b.pdf --out ./out --claude-only
    python run_skill.py lecture_note slides.pdf transcript.txt -w ./workspace

- `skills/<name>/` 이 `config.json` 을 갖고 있다고 가정.
- `batch: true` 스킬은 한 번에 모든 입력을 호출, false 면 파일당 1회.
- `--out` 미지정 시 입력 파일의 부모 / 워크스페이스 아래 `config.output_dir` 로 저장.
- Ctrl+C 로 취소 가능 — 실행 중 Codex subprocess 를 kill 하고 Claude fallback 차단.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

# Windows 콘솔(cp949) 에서 한글 로그가 깨지지 않도록 stdout/stderr 를 UTF-8 로.
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass

import codex_runner
from codex_runner import CodexRunError, load_skill, run_skill
from output_writer import OutputWriter


logger = logging.getLogger("lecture_pipeline.run_skill")


def _resolve_skill_dir(name_or_path: str) -> Path:
    p = Path(name_or_path)
    if p.is_dir():
        return p.resolve()
    candidate = Path(__file__).parent / "skills" / name_or_path
    if candidate.is_dir():
        return candidate.resolve()
    raise CodexRunError(f"skill 을 찾을 수 없음: {name_or_path}")


def _derive_primary_stem(files: list[Path]) -> str:
    for f in files:
        if f.suffix.lower() == ".pdf":
            return f.stem
    return files[0].stem if files else "untitled"


def _install_sigint_handler(cancel_event: threading.Event) -> None:
    """Ctrl+C 한 번: cancel 신호 + 실행 중 subprocess kill.
    두 번째 Ctrl+C 는 기본 동작(즉시 종료) 로 복원."""

    def _handler(signum, frame):
        logger.warning("SIGINT 감지 — 작업 취소 중 (한 번 더 누르면 강제 종료)")
        cancel_event.set()
        codex_runner.terminate_all_active()
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    signal.signal(signal.SIGINT, _handler)


def run(
    *,
    skill_dir: Path,
    files: list[Path],
    out_root: Path,
    claude_only: bool,
    workspace: Path | None,
) -> int:
    skill = load_skill(skill_dir)
    cfg = skill.config
    batch = bool(cfg.get("batch", False))
    rename_map: dict[str, str] = cfg.get("output_rename", {}) or {}
    skill_name = skill_dir.name

    writer = OutputWriter(msg_queue=None)

    cancel_event = threading.Event()
    codex_runner.register_cancel_event(cancel_event)
    _install_sigint_handler(cancel_event)

    def _log(msg: str) -> None:
        print(f"[{skill_name}] {msg}", flush=True)

    fail = 0
    try:
        if batch:
            stem = _derive_primary_stem(files)
            _log(f"▶ batch 실행 ({len(files)} 파일) stem={stem}")
            try:
                outputs = run_skill(
                    skill_dir,
                    files,
                    claude_only=claude_only,
                    log_callback=_log,
                    cancel_event=cancel_event,
                )
                writer.write(
                    outputs=outputs,
                    out_root=out_root,
                    rename_map=rename_map,
                    stem=stem,
                    skill_name=skill_name,
                    rel=None,
                )
            except CodexRunError as exc:
                _log(f"FAIL: {exc}")
                fail += 1
        else:
            for f in files:
                if cancel_event.is_set():
                    _log("취소됨 — 남은 파일 건너뜀")
                    fail += 1
                    break
                try:
                    rel = f.relative_to(workspace) if workspace else Path(f.name)
                except ValueError:
                    rel = Path(f.name)
                _log(f"▶ {rel}")
                try:
                    outputs = run_skill(
                        skill_dir,
                        [f],
                        claude_only=claude_only,
                        log_callback=_log,
                        cancel_event=cancel_event,
                    )
                    writer.write(
                        outputs=outputs,
                        out_root=out_root,
                        rename_map=rename_map,
                        stem=f.stem,
                        skill_name=skill_name,
                        rel=rel,
                    )
                except CodexRunError as exc:
                    _log(f"FAIL {rel}: {exc}")
                    fail += 1
    finally:
        codex_runner.unregister_cancel_event(cancel_event)

    return 0 if fail == 0 else 1


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="GUI 없이 스킬 실행. codex_runner.run_skill + OutputWriter 래퍼.",
    )
    ap.add_argument("skill", help="스킬 이름 (skills/<name>) 또는 절대/상대 경로")
    ap.add_argument("inputs", nargs="+", type=Path, help="입력 파일 1개 이상")
    ap.add_argument("--out", "-o", type=Path, default=None,
                    help="출력 루트. 미지정 시 workspace/config.output_dir "
                         "또는 첫 입력 파일의 부모/config.output_dir.")
    ap.add_argument("--workspace", "-w", type=Path, default=None,
                    help="워크스페이스 루트. rel 경로 계산 기준 + 기본 out 위치.")
    ap.add_argument("--claude-only", action="store_true",
                    help="Codex 건너뛰고 Claude CLI (WSL) 로 실행.")
    ap.add_argument("--max-concurrent", type=int, default=None,
                    help="Codex subprocess 동시 실행 한계 (기본 12).")
    ap.add_argument("-v", "--verbose", action="count", default=0,
                    help="-v: INFO, -vv: DEBUG.")
    return ap.parse_args()


def main() -> int:
    args = _parse_args()
    level = logging.WARNING - 10 * args.verbose
    logging.basicConfig(
        level=max(logging.DEBUG, level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        skill_dir = _resolve_skill_dir(args.skill)
    except CodexRunError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    files = [Path(p).resolve() for p in args.inputs]
    missing = [p for p in files if not p.exists()]
    if missing:
        print(f"error: 입력 파일 없음: {missing}", file=sys.stderr)
        return 2

    cfg = load_skill(skill_dir).config
    output_rel = cfg.get("output_dir", "out")

    workspace = args.workspace.resolve() if args.workspace else None
    if args.out is not None:
        out_root = args.out.resolve()
    else:
        base = workspace if workspace else files[0].parent
        out_root = (base / output_rel).resolve()

    if args.max_concurrent is not None:
        codex_runner.set_max_concurrent(args.max_concurrent)

    return run(
        skill_dir=skill_dir,
        files=files,
        out_root=out_root,
        claude_only=args.claude_only,
        workspace=workspace,
    )


if __name__ == "__main__":
    sys.exit(main())
