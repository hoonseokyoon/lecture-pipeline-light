"""Skill 작업 큐 오케스트레이터. GUI 와 분리되어 tkinter 의존 없음.

- `skill_queue`: 대기 task 리스트
- `running_tasks`: 실행 중 task 리스트 (최대 `max_concurrent_tasks`)
- 디스패처 스레드가 `_worker_sem` 이 허용하는 만큼 task executor 스레드를 spawn
- 각 task 는 per-task cancel Event 를 할당받아 `codex_runner.register_cancel_event`
  로 등록됨. GUI 의 "중단" 버튼은 `cancel_current()` → `terminate_all_active()`
  경유로 모든 등록된 Event 를 set 하고 subprocess kill.
- 소비자(GUI) 와는 `msg_queue` 로만 통신 — `("log", text)`, `("queue_update", None)`,
  `("task_done", None)` 이벤트를 올린다.
"""

import json
import queue
import random
import threading
from pathlib import Path

import codex_runner
from codex_runner import CodexRunError, load_skill, run_skill


_ADJECTIVES = [
    "amber", "blue", "bold", "calm", "cool", "crisp", "dark", "dawn",
    "deep", "dusk", "fair", "fast", "firm", "gold", "gray", "haze",
    "keen", "kind", "late", "lean", "live", "loud", "mild", "neat",
    "pale", "pine", "pure", "rare", "rich", "rust", "sage", "silk",
    "slim", "soft", "tall", "teal", "thin", "true", "vast", "warm",
    "wild", "wise", "young", "zen",
]
_NOUNS = [
    "arc", "ash", "bay", "bee", "bow", "cap", "cub", "dew", "elk",
    "elm", "fin", "fog", "fox", "gem", "hawk", "hill", "ivy", "jade",
    "jay", "kit", "lake", "lark", "leaf", "lynx", "mist", "moon",
    "moss", "oak", "ore", "owl", "peak", "pine", "rain", "reef",
    "ridge", "rock", "sage", "seal", "snow", "star", "stone", "tide",
    "vale", "vine", "wave", "wing", "wolf", "wren", "yew",
]


def _generate_task_tag() -> str:
    return f"{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}"


def _derive_primary_stem(files: list[Path]) -> str:
    """Batch 모드의 {stem} 추출 기준.

    - PDF 가 있으면 첫 PDF 의 stem
    - 없으면 첫 입력 파일의 stem
    - 입력 없으면 "untitled"
    """
    for f in files:
        if f.suffix.lower() == ".pdf":
            return f.stem
    return files[0].stem if files else "untitled"


class TaskQueueController:
    """Skill task 큐의 dispatch/execute 로직.

    생성자에서 디스패처 스레드를 자동으로 시작한다. `shutdown()` 으로 명시
    정리. 소비자는 `msg_queue` 에 올라오는 이벤트를 polling 해서 UI 갱신.
    """

    def __init__(
        self,
        *,
        max_concurrent_tasks: int,
        msg_queue: "queue.Queue[tuple[str, object]]",
        output_writer,
    ):
        self.msg_queue = msg_queue
        self.writer = output_writer

        self.skill_queue: list[dict] = []
        self.running_tasks: list[dict] = []
        self._worker_sem = threading.Semaphore(max_concurrent_tasks)
        self.queue_lock = threading.Lock()
        self.queue_event = threading.Event()
        self._task_id_counter = 0
        self.shutting_down = False

        self._worker_thread = threading.Thread(
            target=self._queue_worker, daemon=True,
        )
        self._worker_thread.start()

    # ------------------------------------------------------------------
    # 공개 API — UI 에서 호출
    # ------------------------------------------------------------------

    def enqueue(
        self,
        *,
        skill_dir: Path,
        files: list[Path],
        workspace: Path | None,
        claude_only: bool,
        conditioning_text: str | None = None,
    ) -> str:
        """새 task 를 대기열 끝에 추가. 반환: 생성된 label (UI 표시용)."""
        names = [p.name for p in files[:2]]
        suffix = f" (+{len(files) - 2})" if len(files) > 2 else ""
        tag = _generate_task_tag()
        label = f"[{tag}] {skill_dir.name}: {', '.join(names)}{suffix}"
        backend = "Claude" if claude_only else "Codex"
        task = {
            "id": self._next_task_id(),
            "tag": tag,
            "skill_dir": skill_dir,
            "files": list(files),
            "workspace": workspace,
            "label": label,
            "status": "pending",
            "claude_only": claude_only,
            "conditioning_text": conditioning_text,
        }
        with self.queue_lock:
            self.skill_queue.append(task)
        self.queue_event.set()
        self.msg_queue.put(("log", f"[queue] + {label} ({backend})"))
        self.msg_queue.put(("queue_update", None))
        return label

    def snapshot(self) -> tuple[list[dict], list[dict]]:
        """(running_tasks, skill_queue) 얕은 복사 반환. UI 렌더링용."""
        with self.queue_lock:
            return list(self.running_tasks), list(self.skill_queue)

    def move_pending_up(self, q_idx: int) -> bool:
        with self.queue_lock:
            if 0 < q_idx < len(self.skill_queue):
                self.skill_queue[q_idx - 1], self.skill_queue[q_idx] = (
                    self.skill_queue[q_idx], self.skill_queue[q_idx - 1],
                )
                return True
        return False

    def move_pending_down(self, q_idx: int) -> bool:
        with self.queue_lock:
            if 0 <= q_idx < len(self.skill_queue) - 1:
                self.skill_queue[q_idx], self.skill_queue[q_idx + 1] = (
                    self.skill_queue[q_idx + 1], self.skill_queue[q_idx],
                )
                return True
        return False

    def remove_pending(self, q_idx: int) -> dict | None:
        with self.queue_lock:
            if 0 <= q_idx < len(self.skill_queue):
                removed = self.skill_queue.pop(q_idx)
                self.msg_queue.put(("log", f"[queue] - {removed['label']}"))
                self.msg_queue.put(("queue_update", None))
                return removed
        return None

    def clear_pending(self) -> int:
        with self.queue_lock:
            cleared = len(self.skill_queue)
            self.skill_queue.clear()
        if cleared:
            self.msg_queue.put(("log", f"[queue] 대기열 {cleared}개 제거"))
            self.msg_queue.put(("queue_update", None))
        return cleared

    def cancel_current(self) -> int:
        """실행 중인 subprocess 전부 kill + 등록된 cancel Event 전부 set."""
        n = codex_runner.terminate_all_active()
        self.msg_queue.put(
            ("log", f"[queue] 현재 작업 중단 시도 — {n}개 subprocess kill")
        )
        return n

    def shutdown(self) -> None:
        """디스패처 루프 종료. daemon 스레드이므로 명시 join 불필요."""
        self.shutting_down = True
        self.queue_event.set()

    # ------------------------------------------------------------------
    # 내부 — 디스패처 & executor
    # ------------------------------------------------------------------

    def _next_task_id(self) -> int:
        self._task_id_counter += 1
        return self._task_id_counter

    def _queue_worker(self) -> None:
        """큐 디스패처 daemon 스레드.

        두 계층의 동시성:
        - `_worker_sem`: 동시에 살아있는 task executor 수.
        - `codex_runner._CODEX_SEMAPHORE`: 실제 Codex/Claude subprocess 동시
          실행 수. composite skill 이 내부 fan-out 하는 경우 task 수보다 커짐.
        """
        while not self.shutting_down:
            self.queue_event.wait(timeout=0.5)
            if self.shutting_down:
                return
            while not self.shutting_down:
                if not self._worker_sem.acquire(timeout=0):
                    break  # 실행 슬롯 없음
                task = None
                with self.queue_lock:
                    if self.skill_queue:
                        task = self.skill_queue.pop(0)
                        self.running_tasks.append(task)
                    else:
                        self.queue_event.clear()
                if task is None:
                    self._worker_sem.release()
                    break
                self.msg_queue.put(("queue_update", None))
                threading.Thread(
                    target=self._task_executor, args=(task,), daemon=True,
                ).start()

    def _task_executor(self, task: dict) -> None:
        """개별 task 실행 스레드. 끝나면 세마포어 반환 + 디스패처 깨움."""
        # task_id 가 낮을수록 우선순위 높음.
        codex_runner.codex_priority.set(task["id"])
        codex_runner.claude_only_mode.set(task.get("claude_only", False))

        # per-task cancel Event — terminate_all_active() 가 set 하면 이 task 의
        # Claude fallback 만 차단. ContextVar 로도 반영해 composite skill 내부
        # fan-out 과 run_codex_task 호출이 자동 감지.
        cancel_event = threading.Event()
        task["cancel_event"] = cancel_event
        codex_runner.register_cancel_event(cancel_event)
        codex_runner.current_cancel_event.set(cancel_event)

        try:
            self._run_skill_task(task)
        except Exception as exc:
            tag = task.get("tag", "?")
            self.msg_queue.put(
                ("log", f"[{tag}/{task['skill_dir'].name}] worker 예외: {exc}")
            )
        finally:
            codex_runner.unregister_cancel_event(cancel_event)
            with self.queue_lock:
                if task in self.running_tasks:
                    self.running_tasks.remove(task)
            self._worker_sem.release()
            self.msg_queue.put(("queue_update", None))
            self.msg_queue.put(("task_done", None))
            self.queue_event.set()  # 디스패처 깨우기

    def _run_skill_task(self, task: dict) -> None:
        skill_dir: Path = task["skill_dir"]
        files: list[Path] = task["files"]
        ws: Path | None = task["workspace"]
        skill_name = skill_dir.name
        tag = task.get("tag", "?")
        claude_only = task.get("claude_only", False)
        prefix = f"{tag}/{skill_name}"

        try:
            skill = load_skill(skill_dir)
        except CodexRunError as exc:
            self.msg_queue.put(("log", f"[{prefix}] skill 로드 실패: {exc}"))
            return

        # 백엔드 표시 결정: config.backend="goose" 면 Goose, 아니면 Codex/Claude
        config_backend = str(skill.config.get("backend", "codex")).lower()
        if config_backend == "goose":
            backend = "Goose"
            display_model = (
                skill.config.get("goose_model")
                or skill.config.get("goose_provider")
                or "(goose default)"
            )
        elif claude_only:
            backend = "Claude"
            display_model = skill.config.get(
                "claude_model", codex_runner.DEFAULT_CLAUDE_MODEL,
            )
        else:
            backend = "Codex"
            display_model = skill.config.get("model", codex_runner.DEFAULT_MODEL)

        output_rel = skill.config.get("output_dir", "out")
        batch = bool(skill.config.get("batch", False))
        rename_map: dict[str, str] = skill.config.get("output_rename", {}) or {}
        base = ws if ws else files[0].parent
        out_root = base / output_rel

        # conditioning 텍스트가 있으면 임시 JSON 파일로 저장해 files 에 append.
        # composite skill(run_rec) 이 input_paths 에서 conditioning_*.txt 를 찾아 읽음.
        cond_text = task.get("conditioning_text")
        if cond_text:
            tmp_dir = base / ".zsrec_tmp"
            try:
                tmp_dir.mkdir(exist_ok=True)
                cond_file = tmp_dir / f"conditioning_{task['id']}.txt"
                cond_file.write_text(
                    json.dumps({"text": cond_text}, ensure_ascii=False),
                    encoding="utf-8",
                )
                files = files + [cond_file]
            except OSError as exc:
                self.msg_queue.put(
                    ("log", f"[{prefix}] conditioning 임시파일 쓰기 실패: {exc}")
                )
                return

        self.msg_queue.put(
            ("log", f"[{prefix}] ▶ 실행 시작: {len(files)}개 파일 "
             f"| {backend} ({display_model})")
        )

        if batch:
            stem = _derive_primary_stem(files)
            try:
                outputs = run_skill(
                    skill_dir,
                    files,
                    claude_only=claude_only,
                    log_callback=lambda m, p=prefix: self.msg_queue.put(
                        ("log", f"[{p}] {m}")
                    ),
                )
                self.writer.write(
                    outputs=outputs,
                    out_root=out_root,
                    rename_map=rename_map,
                    stem=stem,
                    skill_name=skill_name,
                    rel=None,
                )
            except CodexRunError as exc:
                self.msg_queue.put(("log", f"[{prefix}] FAIL: {exc}"))
            except Exception as exc:
                self.msg_queue.put(("log", f"[{prefix}] 예외: {exc}"))
            return

        for f in files:
            try:
                rel = f.relative_to(ws) if ws else Path(f.name)
            except ValueError:
                rel = Path(f.name)
            try:
                self.msg_queue.put(("log", f"[{prefix}] {rel} 실행 중..."))
                outputs = run_skill(
                    skill_dir,
                    [f],
                    claude_only=claude_only,
                    log_callback=lambda m, p=prefix, r=rel: self.msg_queue.put(
                        ("log", f"[{p}] {r} {m}")
                    ),
                )
                self.writer.write(
                    outputs=outputs,
                    out_root=out_root,
                    rename_map=rename_map,
                    stem=f.stem,
                    skill_name=skill_name,
                    rel=rel,
                )
            except CodexRunError as exc:
                self.msg_queue.put(("log", f"[{prefix}] FAIL {rel}: {exc}"))
            except Exception as exc:
                self.msg_queue.put(("log", f"[{prefix}] 예외 {rel}: {exc}"))
