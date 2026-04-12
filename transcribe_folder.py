#!/usr/bin/env python
"""폴더 내 음성 파일 일괄 전사 + 교정 파이프라인.

사용법:
    python transcribe_folder.py /path/to/folder
    python transcribe_folder.py /path/to/folder --recursive
    python transcribe_folder.py . --no-correct
    python transcribe_folder.py /path/to/folder --nb-workers 1 --codex-workers 2

동작:
    1) 폴더(+하위) 탐색 → 폴더별 노트북 생성/로드 → 공유 큐에 등록
    2) NB Worker(2) 병렬 전사 → 완료 즉시 Codex큐로
    3) Codex Worker(4) 병렬 교정
    4) 재실행 시 미완료 단계만 수행

로그: 각 폴더마다 .transcribe_log
"""

import argparse
import queue
import random
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

AUDIO_EXTS = {".m4a", ".mp3", ".mp4", ".wav", ".ogg", ".flac", ".aac"}
SCRIPT_DIR = Path(__file__).parent
LOG_NAME = ".transcribe_log"
SENTINEL = None


# ── CLI ──

def parse_args():
    p = argparse.ArgumentParser(description="폴더 음성 일괄 전사 + 교정")
    p.add_argument("folder", nargs="?", default=".", help="대상 폴더")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="하위 폴더 재귀 탐색")
    p.add_argument("--no-correct", action="store_true", help="교정 단계 건너뛰기")
    p.add_argument("-c", "--config", help="교정 설정 파일 (correct_config.json)")
    p.add_argument("--nb-workers", type=int, default=2,
                   help="NotebookLM 전사 병렬 수 (기본: 2)")
    p.add_argument("--codex-workers", type=int, default=4,
                   help="Codex 교정 병렬 수 (기본: 4)")
    p.add_argument("--jitter", type=float, default=3.0,
                   help="워커 시작 지연 최대값(초) (기본: 3.0)")
    return p.parse_args()


# ── 폴더 컨텍스트 ──

class TranscribeLog:
    """폴더별 로그 (thread-safe)."""

    def __init__(self, path: Path):
        self.path = path
        self.notebook_id = ""
        self.notebook_name = ""
        self.transcribed: set[str] = set()
        self.corrected: set[str] = set()
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("NOTEBOOK_ID="):
                self.notebook_id = line.split("=", 1)[1]
            elif line.startswith("NOTEBOOK_NAME="):
                self.notebook_name = line.split("=", 1)[1]
            elif line.startswith("TRANSCRIBED:"):
                self.transcribed.add(line.split(":", 1)[1])
            elif line.startswith("CORRECTED:"):
                self.corrected.add(line.split(":", 1)[1])
            elif line.startswith("DONE:"):
                self.transcribed.add(line.split(":", 1)[1])

    def _save(self):
        lines = []
        if self.notebook_id:
            lines.append(f"NOTEBOOK_ID={self.notebook_id}")
        if self.notebook_name:
            lines.append(f"NOTEBOOK_NAME={self.notebook_name}")
        for f in sorted(self.transcribed):
            lines.append(f"TRANSCRIBED:{f}")
        for f in sorted(self.corrected):
            lines.append(f"CORRECTED:{f}")
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def set_notebook(self, nb_id: str, nb_name: str):
        with self._lock:
            self.notebook_id = nb_id
            self.notebook_name = nb_name
            self._save()

    def mark_transcribed(self, filename: str):
        with self._lock:
            self.transcribed.add(filename)
            self._save()

    def mark_corrected(self, filename: str):
        with self._lock:
            self.corrected.add(filename)
            self._save()


@dataclass
class FolderCtx:
    """폴더별 독립 컨텍스트."""
    folder: Path
    log: TranscribeLog
    notebook_id: str


@dataclass
class Task:
    """큐 아이템 — 오디오 파일 + 소속 폴더 컨텍스트."""
    audio: Path
    ctx: FolderCtx


# ── 폴더 탐색 ──

def find_audio_in(folder: Path) -> list[Path]:
    return sorted(f for f in folder.iterdir()
                  if f.is_file() and f.suffix.lower() in AUDIO_EXTS)


def discover_folders(root: Path, recursive: bool) -> list[Path]:
    """오디오 파일이 있는 폴더 목록 반환."""
    folders = []
    if find_audio_in(root):
        folders.append(root)
    if recursive:
        for d in sorted(root.rglob("*")):
            if d.is_dir() and find_audio_in(d):
                folders.append(d)
    return folders


# ── 노트북 생성 ──

def create_notebook(name: str) -> str | None:
    code = (
        "import asyncio; from notebooklm import NotebookLMClient\n"
        "async def go():\n"
        "    async with await NotebookLMClient.from_storage() as c:\n"
        f"        nb = await c.notebooks.create({name!r})\n"
        "        print(f'NOTEBOOK_ID={nb.id}')\n"
        "asyncio.run(go())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    )
    for line in (result.stdout or "").splitlines():
        if line.startswith("NOTEBOOK_ID="):
            return line.split("=", 1)[1]
    if result.returncode != 0:
        print(f"  노트북 생성 실패: {(result.stderr or '')[:200]}", flush=True)
    return None


# ── 전사 / 교정 subprocess ──

def run_transcribe(audio: Path, output: Path, notebook_id: str) -> bool:
    cmd = [sys.executable, str(SCRIPT_DIR / "transcribe.py"),
           str(audio), "-o", str(output),
           "--notebook-id", notebook_id, "--timeout", "600"]
    result = subprocess.run(
        cmd, capture_output=True, encoding="utf-8", errors="replace",
        timeout=2400,
    )
    if result.returncode != 0:
        err = result.stderr or ""
        print(f"    [NB] 오류 {audio.name}: {err[:200]}", flush=True)
        return False
    return True


def run_correct(txt_path: Path, config_path: str | None) -> bool:
    cmd = [sys.executable, str(SCRIPT_DIR / "correct.py"), str(txt_path)]
    if config_path:
        cmd.extend(["-c", config_path])
    result = subprocess.run(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace", timeout=2400,
    )
    if result.returncode != 0:
        err = result.stderr or ""
        print(f"    [CX] 오류 {txt_path.name}: {err[:200]}", flush=True)
    return result.returncode == 0


# ── 워커 ──

class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.nb_done = 0
        self.nb_fail = 0
        self.cx_done = 0
        self.cx_fail = 0

    def inc(self, field: str):
        with self._lock:
            setattr(self, field, getattr(self, field) + 1)


def nb_worker(worker_id: int, nb_queue: queue.Queue,
              codex_queue: queue.Queue | None,
              stats: Stats, jitter: float):
    while True:
        item = nb_queue.get()
        if item is SENTINEL:
            nb_queue.task_done()
            break

        task: Task = item
        audio = task.audio
        ctx = task.ctx
        txt = audio.with_suffix(".txt")

        time.sleep(random.uniform(0.5, jitter))

        rel = audio.relative_to(ctx.folder.parent) if ctx.folder.parent != audio.parent else audio.name
        print(f"  [NB-{worker_id}] 전사 시작: {rel}", flush=True)
        ok = run_transcribe(audio, txt, ctx.notebook_id)

        if ok and txt.exists() and txt.stat().st_size > 0:
            ctx.log.mark_transcribed(audio.name)
            stats.inc("nb_done")
            print(f"  [NB-{worker_id}] 전사 완료: {rel}", flush=True)
            if codex_queue is not None:
                codex_queue.put(task)
        else:
            stats.inc("nb_fail")
            print(f"  [NB-{worker_id}] 전사 실패: {rel}", flush=True)

        nb_queue.task_done()


def codex_worker(worker_id: int, codex_queue: queue.Queue,
                 config_path: str | None,
                 stats: Stats, jitter: float):
    while True:
        item = codex_queue.get()
        if item is SENTINEL:
            codex_queue.task_done()
            break

        task: Task = item
        audio = task.audio
        ctx = task.ctx
        txt = audio.with_suffix(".txt")

        time.sleep(random.uniform(0.5, jitter))

        rel = audio.relative_to(ctx.folder.parent) if ctx.folder.parent != audio.parent else audio.name
        print(f"  [CX-{worker_id}] 교정 시작: {rel}", flush=True)
        ok = run_correct(txt, config_path)

        if ok:
            ctx.log.mark_corrected(audio.name)
            stats.inc("cx_done")
            print(f"  [CX-{worker_id}] 교정 완료: {rel}", flush=True)
        else:
            stats.inc("cx_fail")
            print(f"  [CX-{worker_id}] 교정 실패: {rel}", flush=True)

        codex_queue.task_done()


# ── 메인 ──

def main():
    args = parse_args()
    root = Path(args.folder).resolve()

    mode = "재귀" if args.recursive else "단일"
    print(f"=== 전사+교정 파이프라인 ({mode}) ===")
    print(f"    루트: {root}")
    print(f"    NB워커: {args.nb_workers} | Codex워커: {args.codex_workers} "
          f"| Jitter: {args.jitter}s\n")

    # ── 1) 폴더 탐색 + 노트북 확보 + 작업 수집 ──
    folders = discover_folders(root, args.recursive)
    if not folders:
        print("오디오 파일이 있는 폴더 없음.")
        return

    all_nb_tasks: list[Task] = []
    all_cx_tasks: list[Task] = []
    total_files = 0
    total_skip = 0

    for folder in folders:
        audio_files = find_audio_in(folder)
        log = TranscribeLog(folder / LOG_NAME)

        # 노트북 확보
        if not log.notebook_id:
            nb_name = folder.name
            print(f"[{folder.name}] 노트북 생성: {nb_name} ...", end=" ", flush=True)
            nb_id = create_notebook(nb_name)
            if not nb_id:
                print("실패 — 이 폴더 건너뜀.")
                continue
            log.set_notebook(nb_id, nb_name)
            print(f"OK ({nb_id})")
        else:
            print(f"[{folder.name}] 기존 노트북: {log.notebook_id}")

        ctx = FolderCtx(folder=folder, log=log, notebook_id=log.notebook_id)

        # 분류
        need_transcribe = [f for f in audio_files if f.name not in log.transcribed]
        need_correct_only = [f for f in audio_files
                             if f.name in log.transcribed
                             and f.name not in log.corrected]
        skip = len(audio_files) - len(need_transcribe) - len(need_correct_only)

        total_files += len(audio_files)
        total_skip += skip

        for f in need_transcribe:
            all_nb_tasks.append(Task(audio=f, ctx=ctx))
        for f in need_correct_only:
            all_cx_tasks.append(Task(audio=f, ctx=ctx))

        print(f"  파일: {len(audio_files)} | "
              f"전사: {len(need_transcribe)} | "
              f"교정만: {len(need_correct_only)} | "
              f"완료: {skip}")

    if not all_nb_tasks and not all_cx_tasks:
        print(f"\n모두 완료됨. (전체 {total_files}개)")
        return

    print(f"\n총 작업: 전사 {len(all_nb_tasks)} + 교정 {len(all_cx_tasks)} "
          f"(스킵 {total_skip})")

    # ── 2) 큐 구성 ──
    nb_queue: queue.Queue = queue.Queue()
    codex_queue: queue.Queue = queue.Queue()
    stats = Stats()

    for task in all_nb_tasks:
        nb_queue.put(task)
    if not args.no_correct:
        for task in all_cx_tasks:
            codex_queue.put(task)

    # ── 3) 워커 시작 ──
    cx_queue_for_nb = codex_queue if not args.no_correct else None

    nb_threads = []
    for i in range(args.nb_workers):
        t = threading.Thread(
            target=nb_worker,
            args=(i, nb_queue, cx_queue_for_nb, stats, args.jitter),
            daemon=True,
        )
        nb_threads.append(t)
        t.start()
        time.sleep(0.5)

    cx_threads = []
    if not args.no_correct:
        for i in range(args.codex_workers):
            t = threading.Thread(
                target=codex_worker,
                args=(i, codex_queue, args.config, stats, args.jitter),
                daemon=True,
            )
            cx_threads.append(t)
            t.start()
            time.sleep(0.3)

    # ── 4) 완료 대기 ──
    nb_queue.join()
    print("\n--- 전사 전부 완료 ---", flush=True)

    for _ in nb_threads:
        nb_queue.put(SENTINEL)
    for t in nb_threads:
        t.join()

    if not args.no_correct:
        codex_queue.join()
        print("--- 교정 전부 완료 ---", flush=True)

        for _ in cx_threads:
            codex_queue.put(SENTINEL)
        for t in cx_threads:
            t.join()

    # ── 5) 결과 ──
    print(f"\n{'=' * 40}")
    print(f"전사: 성공 {stats.nb_done} / 실패 {stats.nb_fail}")
    print(f"교정: 성공 {stats.cx_done} / 실패 {stats.cx_fail}")
    print(f"폴더: {len(folders)}개 | 파일: {total_files}개")
    for folder in folders:
        log = TranscribeLog(folder / LOG_NAME)
        n = len(find_audio_in(folder))
        print(f"  {folder.name}: 전사 {len(log.transcribed)}/{n} | "
              f"교정 {len(log.corrected)}/{n} | "
              f"노트북 {log.notebook_id[:8]}...")


if __name__ == "__main__":
    main()
