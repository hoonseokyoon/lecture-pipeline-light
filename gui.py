#!/usr/bin/env python
"""lecture-pipeline 가벼운 tkinter GUI.

기능:
- Open Workspace: 작업 폴더 선택
- 파일 트리: workspace의 파일/폴더 (다중 선택, Ctrl/Shift)
- TRANSCRIBE: workspace에 대해 recursive transcribe + correct (transcribe_folder.py 실행)
- Skill 버튼: `skills/` 폴더에서 동적 로드. 선택된 파일들에 대해 per-file 실행.

출력:
- 스킬 결과는 `<workspace>/out/<skill_name>/<입력 상대경로 stem>/<output_name>`.
- transcribe 결과는 transcribe_folder.py 관례대로 입력 파일 옆에 저장됨.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, scrolledtext, ttk


IS_FROZEN = getattr(sys, "frozen", False)

if IS_FROZEN:
    # PyInstaller 번들 실행. .exe 옆 폴더를 외부 파일(skills, transcribe_folder.py 등)
    # 기준점으로 쓴다. sys.executable은 .exe 자체라 subprocess용으로는 부적합해서
    # PATH의 python을 호출한다.
    SCRIPT_DIR = Path(sys.executable).parent.resolve()
    PYTHON_EXE = "python"
else:
    SCRIPT_DIR = Path(__file__).parent.resolve()
    PYTHON_EXE = sys.executable

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import codex_runner  # noqa: E402
from codex_runner import CodexRunError, load_skill, run_skill  # noqa: E402


SKILLS_DIR = SCRIPT_DIR / "skills"
TRANSCRIBE_SCRIPT = SCRIPT_DIR / "transcribe_folder.py"
GUI_CONFIG_PATH = SCRIPT_DIR / "gui_config.json"
_SUBPROCESS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULT_LOG_MAX_LINES = 5000


def _load_gui_config() -> dict:
    """`gui_config.json` 읽기. 없거나 파싱 실패 시 빈 dict."""
    if not GUI_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(GUI_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _derive_primary_stem(files: list[Path]) -> str:
    """Batch 모드의 {stem} 추출 기준.

    - PDF가 있으면 **첫 PDF** 의 stem
    - 없으면 첫 입력 파일의 stem
    - 입력 없으면 "untitled"
    """
    for f in files:
        if f.suffix.lower() == ".pdf":
            return f.stem
    return files[0].stem if files else "untitled"


def _with_increment(path: Path, n: int) -> Path:
    """n==0이면 원본 path, n>0이면 `<stem>-<n><suffix>` 형태."""
    if n <= 0:
        return path
    return path.with_name(f"{path.stem}-{n}{path.suffix}")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Lecture Pipeline")
        self.root.geometry("920x660")

        # ── GUI config ──
        gui_cfg = _load_gui_config()
        try:
            self.log_max_lines = int(gui_cfg.get("log_max_lines", DEFAULT_LOG_MAX_LINES))
        except (TypeError, ValueError):
            self.log_max_lines = DEFAULT_LOG_MAX_LINES
        if self.log_max_lines < 0:
            self.log_max_lines = 0  # 0 = 무제한
        self._log_line_count = 0

        try:
            self.max_concurrent_tasks = int(
                gui_cfg.get("max_concurrent_tasks", codex_runner.DEFAULT_MAX_CONCURRENT)
            )
        except (TypeError, ValueError):
            self.max_concurrent_tasks = codex_runner.DEFAULT_MAX_CONCURRENT
        if self.max_concurrent_tasks < 1:
            self.max_concurrent_tasks = 1

        self.workspace: Path | None = None
        self.transcribe_busy = False
        self.msg_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()

        # ── Skill 작업 큐 (Transcribe와 독립) ──
        self.skill_queue: list[dict] = []
        self.running_tasks: list[dict] = []
        self._worker_sem = threading.Semaphore(self.max_concurrent_tasks)
        self._write_lock = threading.Lock()
        self.queue_lock = threading.Lock()
        self.queue_event = threading.Event()
        self._task_id_counter = 0
        self.shutting_down = False

        self._build_ui()
        self._poll_queue()

        # 큐 디스패처 스레드 시작
        self.worker_thread = threading.Thread(
            target=self._queue_worker, daemon=True,
        )
        self.worker_thread.start()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ─────────────────────────────────────────────────────────
    # UI 구성
    # ─────────────────────────────────────────────────────────

    def _build_ui(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=8)
        self.open_btn = ttk.Button(
            top, text="Open Workspace", command=self.open_workspace
        )
        self.open_btn.pack(side="left")
        self.ws_label = ttk.Label(top, text="(no workspace)", foreground="#666")
        self.ws_label.pack(side="left", padx=10)

        mid = ttk.Frame(self.root)
        mid.pack(fill="both", expand=True, padx=10, pady=4)

        # 좌측: 파일 트리
        left = ttk.Frame(mid)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(
            left, text="Files (Ctrl/Shift 다중 선택):"
        ).pack(anchor="w")
        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(
            tree_frame,
            selectmode="extended",
            show="tree",
            columns=("path",),
            displaycolumns=(),
        )
        scroll = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.tree.yview
        )
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # 우측: 액션 패널
        right = ttk.Frame(mid, width=220)
        right.pack(side="right", fill="y", padx=(10, 0))
        right.pack_propagate(False)

        self.transcribe_btn = tk.Button(
            right,
            text="TRANSCRIBE\n(recursive)",
            command=self.on_transcribe,
            state="disabled",
            height=3,
            font=("TkDefaultFont", 11, "bold"),
            bg="#2e7d32",
            fg="white",
            activebackground="#1b5e20",
            activeforeground="white",
            relief="raised",
            bd=2,
        )
        self.transcribe_btn.pack(fill="x", pady=(0, 12))

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=4)
        ttk.Label(
            right, text="Skills (파일 선택 후 클릭):"
        ).pack(anchor="w", pady=(4, 2))

        self.skill_btns: list[ttk.Button] = []
        for skill_dir in self._discover_skills():
            btn = ttk.Button(
                right,
                text=skill_dir.name,
                command=lambda s=skill_dir: self.on_skill(s),
                state="disabled",
            )
            btn.pack(fill="x", pady=2)
            self.skill_btns.append(btn)

        if not self.skill_btns:
            ttk.Label(
                right, text="(no skills found)", foreground="#999"
            ).pack(pady=4)

        ttk.Button(
            right, text="Refresh files", command=self._refresh_tree
        ).pack(fill="x", pady=(12, 0))

        # ── Queue 패널 (skill 예약) ──
        queue_frame = ttk.LabelFrame(
            self.root, text="Skill Queue (예약)", padding=6,
        )
        queue_frame.pack(fill="x", padx=10, pady=(4, 4))

        q_inner = ttk.Frame(queue_frame)
        q_inner.pack(fill="x")
        self.queue_listbox = tk.Listbox(
            q_inner,
            height=5,
            selectmode="browse",
            activestyle="dotbox",
            font=("TkDefaultFont", 9),
        )
        self.queue_listbox.pack(side="left", fill="x", expand=True)
        q_scroll = ttk.Scrollbar(
            q_inner, orient="vertical", command=self.queue_listbox.yview,
        )
        self.queue_listbox.configure(yscrollcommand=q_scroll.set)
        q_scroll.pack(side="right", fill="y")

        q_btns = ttk.Frame(queue_frame)
        q_btns.pack(fill="x", pady=(5, 0))
        ttk.Button(q_btns, text="↑", command=self.on_move_up, width=3).pack(
            side="left"
        )
        ttk.Button(q_btns, text="↓", command=self.on_move_down, width=3).pack(
            side="left", padx=(2, 0)
        )
        ttk.Button(
            q_btns, text="선택 취소", command=self.on_cancel_selected,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            q_btns, text="현재 중단", command=self.on_stop_current,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            q_btns, text="큐 비우기", command=self.on_clear_queue,
        ).pack(side="right")

        # 하단: 로그
        ttk.Label(self.root, text="Log:").pack(
            anchor="w", padx=10, pady=(6, 0)
        )
        self.log = scrolledtext.ScrolledText(
            self.root, height=10, state="disabled", wrap="word"
        )
        self.log.pack(fill="both", expand=False, padx=10, pady=(0, 10))

    def _discover_skills(self) -> list[Path]:
        if not SKILLS_DIR.is_dir():
            return []
        return sorted(
            d for d in SKILLS_DIR.iterdir()
            if d.is_dir() and (d / "prompt.txt").exists()
        )

    # ─────────────────────────────────────────────────────────
    # Workspace / 파일 트리
    # ─────────────────────────────────────────────────────────

    def open_workspace(self):
        path = filedialog.askdirectory(title="Select workspace folder")
        if not path:
            return
        self.workspace = Path(path)
        self.ws_label.config(text=str(self.workspace), foreground="black")
        self._refresh_tree()
        self._update_button_states()
        self._log(f"Opened workspace: {self.workspace}")

    def _refresh_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        if not self.workspace or not self.workspace.is_dir():
            return
        self._insert_dir("", self.workspace)

    def _insert_dir(self, parent_id: str, path: Path):
        try:
            entries = sorted(
                path.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except (PermissionError, OSError):
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            label = entry.name + ("/" if entry.is_dir() else "")
            try:
                iid = self.tree.insert(
                    parent_id, "end", text=label, values=[str(entry)]
                )
            except tk.TclError:
                continue
            if entry.is_dir():
                self._insert_dir(iid, entry)

    def _get_selected_files(self) -> list[Path]:
        files = []
        for iid in self.tree.selection():
            vals = self.tree.item(iid, "values")
            if not vals:
                continue
            p = Path(vals[0])
            if p.is_file():
                files.append(p)
        return files

    # ─────────────────────────────────────────────────────────
    # Transcribe (subprocess로 transcribe_folder.py 실행)
    # ─────────────────────────────────────────────────────────

    def on_transcribe(self):
        if not self.workspace or self.transcribe_busy:
            return
        self._log(f"[transcribe] start (recursive) on {self.workspace}")
        self._set_transcribe_busy(True)
        threading.Thread(target=self._run_transcribe, daemon=True).start()

    def _run_transcribe(self):
        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            proc = subprocess.Popen(
                [
                    PYTHON_EXE,
                    str(TRANSCRIBE_SCRIPT),
                    str(self.workspace),
                    "--recursive",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=_SUBPROCESS_FLAGS,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                self.msg_queue.put(("log", line.rstrip()))
            proc.wait()
            self.msg_queue.put(
                ("log", f"[transcribe] exit={proc.returncode}")
            )
        except Exception as exc:
            self.msg_queue.put(("log", f"[transcribe] error: {exc}"))
        finally:
            self.msg_queue.put(("transcribe_done", None))

    # ─────────────────────────────────────────────────────────
    # Skill 실행 — 큐 예약 방식
    # ─────────────────────────────────────────────────────────

    def on_skill(self, skill_dir: Path):
        """버튼 클릭: 큐에 추가. 스킬 config 의 `ask_file_order` 가 true 면
        FileOrderDialog 팝업으로 사용자가 순서 지정."""
        if not self.workspace:
            return
        files = self._get_selected_files()
        if not files:
            self._log(
                f"[{skill_dir.name}] 선택된 파일 없음. 먼저 파일을 선택하세요."
            )
            return

        # config 로드해서 ask_file_order 플래그 확인
        try:
            skill = load_skill(skill_dir)
        except CodexRunError as exc:
            self._log(f"[{skill_dir.name}] config 로드 실패: {exc}")
            return

        if bool(skill.config.get("ask_file_order", False)) and len(files) > 1:
            from file_order_dialog import FileOrderDialog
            dlg = FileOrderDialog(
                self.root,
                files,
                title=f"{skill_dir.name}: 입력 순서 지정",
                explain_text=(
                    "PDF는 강의 진행 순서, 녹취록은 수업 시간 순서로 정렬하세요. "
                    "기본값은 파일명 알파벳 순."
                ),
            )
            dlg.show_modal()
            self.root.wait_window(dlg)
            if dlg.result is None:
                self._log(f"[{skill_dir.name}] 순서 지정 취소 — 큐 추가 안 함")
                return
            files = dlg.result

        self._enqueue_skill_task(skill_dir, files)

    def _next_task_id(self) -> int:
        self._task_id_counter += 1
        return self._task_id_counter

    def _enqueue_skill_task(self, skill_dir: Path, files: list[Path]) -> None:
        names = [p.name for p in files[:2]]
        suffix = f" (+{len(files) - 2})" if len(files) > 2 else ""
        label = f"{skill_dir.name}: {', '.join(names)}{suffix}"
        task = {
            "id": self._next_task_id(),
            "skill_dir": skill_dir,
            "files": list(files),
            "workspace": self.workspace,
            "label": label,
            "status": "pending",
        }
        with self.queue_lock:
            self.skill_queue.append(task)
        self.queue_event.set()
        self._log(f"[queue] + {label}")
        self.msg_queue.put(("queue_update", None))

    def _queue_worker(self) -> None:
        """큐의 task를 병렬 디스패치하는 daemon 스레드.

        _worker_sem이 허용하는 만큼 동시에 실행하고,
        Codex 레벨 동시성은 codex_runner._CODEX_SEMAPHORE가 제어."""
        while not self.shutting_down:
            self.queue_event.wait(timeout=0.5)
            if self.shutting_down:
                return
            # 세마포어 여유분만큼 큐에서 꺼내 실행
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
        """개별 task 실행 스레드. 완료 후 세마포어를 반환하고 디스패처를 깨운다."""
        # 첫 번째(task_id가 낮은) 작업이 Codex 워커를 우선 확보하도록 설정.
        codex_runner.codex_priority.set(task["id"])
        try:
            self._run_skill_task(task)
        except Exception as exc:
            self.msg_queue.put(
                ("log", f"[{task['skill_dir'].name}] worker 예외: {exc}")
            )
        finally:
            with self.queue_lock:
                if task in self.running_tasks:
                    self.running_tasks.remove(task)
            self._worker_sem.release()
            self.msg_queue.put(("queue_update", None))
            self.msg_queue.put(("task_done", None))
            self.queue_event.set()  # 디스패처 깨우기 — 다음 task 시작 가능

    def _run_skill_task(self, task: dict) -> None:
        skill_dir: Path = task["skill_dir"]
        files: list[Path] = task["files"]
        ws: Path | None = task["workspace"]
        skill_name = skill_dir.name

        try:
            skill = load_skill(skill_dir)
        except CodexRunError as exc:
            self.msg_queue.put(
                ("log", f"[{skill_name}] skill 로드 실패: {exc}")
            )
            return

        output_rel = skill.config.get("output_dir", "out")
        batch = bool(skill.config.get("batch", False))
        rename_map: dict[str, str] = skill.config.get("output_rename", {}) or {}
        base = ws if ws else files[0].parent
        out_root = base / output_rel

        self.msg_queue.put(
            ("log", f"[{skill_name}] ▶ 실행 시작: {len(files)}개 파일")
        )

        if batch:
            stem = _derive_primary_stem(files)
            try:
                outputs = run_skill(
                    skill_dir,
                    files,
                    log_callback=lambda m, n=skill_name: self.msg_queue.put(
                        ("log", f"[{n}] {m}")
                    ),
                )
                self._write_outputs(
                    outputs=outputs,
                    out_root=out_root,
                    rename_map=rename_map,
                    stem=stem,
                    skill_name=skill_name,
                    rel=None,
                )
            except CodexRunError as exc:
                self.msg_queue.put(("log", f"[{skill_name}] FAIL: {exc}"))
            except Exception as exc:
                self.msg_queue.put(("log", f"[{skill_name}] 예외: {exc}"))
            return

        for f in files:
            try:
                rel = f.relative_to(ws) if ws else Path(f.name)
            except ValueError:
                rel = Path(f.name)
            try:
                self.msg_queue.put(
                    ("log", f"[{skill_name}] {rel} 실행 중...")
                )
                outputs = run_skill(
                    skill_dir,
                    [f],
                    log_callback=lambda m, n=skill_name, r=rel: self.msg_queue.put(
                        ("log", f"[{n}] {r} {m}")
                    ),
                )
                self._write_outputs(
                    outputs=outputs,
                    out_root=out_root,
                    rename_map=rename_map,
                    stem=f.stem,
                    skill_name=skill_name,
                    rel=rel,
                )
            except CodexRunError as exc:
                self.msg_queue.put(
                    ("log", f"[{skill_name}] FAIL {rel}: {exc}")
                )
            except Exception as exc:
                self.msg_queue.put(
                    ("log", f"[{skill_name}] 예외 {rel}: {exc}")
                )

    def _write_outputs(
        self,
        outputs: dict[str, bytes],
        out_root: Path,
        rename_map: dict[str, str],
        stem: str,
        skill_name: str,
        rel: Path | None,
    ) -> None:
        """출력 파일들을 `out_root` 아래(플랫)에 저장.

        - `rename_map`의 템플릿 ({stem} 치환)으로 파일명 변환
        - 여러 출력이 있으면 **공통 -N suffix**를 계산해 충돌 회피
          (lecture_note의 note.md/json/html이 같은 접미사 공유)
        """
        if not outputs:
            return
        out_root.mkdir(parents=True, exist_ok=True)

        # Step 1: rename 적용해 target path 결정 (increment 전)
        base_targets: dict[str, Path] = {}
        for name in outputs.keys():
            template = rename_map.get(name, name)
            try:
                renamed = template.format(stem=stem)
            except (KeyError, IndexError, ValueError):
                renamed = template
            base_targets[name] = out_root / renamed

        # Step 2+3: increment 계산과 ���일 쓰기를 원자적으로 수행
        # (병렬 task가 동일 경로에 동시 접근하는 race condition 방지)
        prefix = f"{rel} → " if rel else ""
        with self._write_lock:
            increment = 0
            while True:
                conflict = any(
                    _with_increment(p, increment).exists()
                    for p in base_targets.values()
                )
                if not conflict:
                    break
                increment += 1

            for name, content in outputs.items():
                final = _with_increment(base_targets[name], increment)
                final.parent.mkdir(parents=True, exist_ok=True)
                final.write_bytes(content)
                self.msg_queue.put(
                    ("log", f"[{skill_name}] OK {prefix}{final.name}")
                )

    # ─────────────────────────────────────────────────────────
    # Queue 조작 버튼 핸들러
    # ─────────────────────────────────────────────────────────

    def on_move_up(self) -> None:
        sel = self.queue_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        with self.queue_lock:
            offset = len(self.running_tasks)
            q_idx = idx - offset
            if 0 < q_idx < len(self.skill_queue):
                self.skill_queue[q_idx - 1], self.skill_queue[q_idx] = (
                    self.skill_queue[q_idx],
                    self.skill_queue[q_idx - 1],
                )
            else:
                return
        self._update_queue_listbox()
        new_idx = idx - 1
        if new_idx >= 0:
            self.queue_listbox.selection_clear(0, tk.END)
            self.queue_listbox.selection_set(new_idx)

    def on_move_down(self) -> None:
        sel = self.queue_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        with self.queue_lock:
            offset = len(self.running_tasks)
            q_idx = idx - offset
            if 0 <= q_idx < len(self.skill_queue) - 1:
                self.skill_queue[q_idx], self.skill_queue[q_idx + 1] = (
                    self.skill_queue[q_idx + 1],
                    self.skill_queue[q_idx],
                )
            else:
                return
        self._update_queue_listbox()
        new_idx = idx + 1
        self.queue_listbox.selection_clear(0, tk.END)
        self.queue_listbox.selection_set(new_idx)

    def on_cancel_selected(self) -> None:
        sel = self.queue_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        with self.queue_lock:
            offset = len(self.running_tasks)
            q_idx = idx - offset
            if q_idx < 0:
                # 실행 중인 항목 선택 → 현재 중단과 동일
                pass
            elif 0 <= q_idx < len(self.skill_queue):
                removed = self.skill_queue.pop(q_idx)
                self._log(f"[queue] - {removed['label']}")
                self._update_queue_listbox()
                return
            else:
                return
        self.on_stop_current()

    def on_stop_current(self) -> None:
        n = codex_runner.terminate_all_active()
        self._log(f"[queue] 현재 작업 중단 시도 — {n}개 subprocess kill")

    def on_clear_queue(self) -> None:
        with self.queue_lock:
            cleared = len(self.skill_queue)
            self.skill_queue.clear()
        if cleared:
            self._log(f"[queue] 대기열 {cleared}개 제거")
        self._update_queue_listbox()

    def _update_queue_listbox(self) -> None:
        prev_sel = self.queue_listbox.curselection()
        prev_idx = prev_sel[0] if prev_sel else None
        self.queue_listbox.delete(0, tk.END)
        with self.queue_lock:
            for task in self.running_tasks:
                self.queue_listbox.insert(
                    tk.END, f"▶ {task['label']}  (실행 중)"
                )
            for task in self.skill_queue:
                self.queue_listbox.insert(tk.END, f"   {task['label']}")
        # 선택 복원 (가능한 경우)
        if prev_idx is not None:
            size = self.queue_listbox.size()
            if 0 <= prev_idx < size:
                self.queue_listbox.selection_set(prev_idx)

    def _on_close(self) -> None:
        self.shutting_down = True
        self.queue_event.set()
        try:
            self.root.destroy()
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────
    # busy / 로그 / 큐 폴링
    # ─────────────────────────────────────────────────────────

    def _set_transcribe_busy(self, busy: bool) -> None:
        """Transcribe 버튼만 on/off. Skill 버튼은 큐에 넣을 수 있어 항상 활성."""
        self.transcribe_busy = busy
        if not self.workspace:
            self.transcribe_btn.config(state="disabled")
        else:
            self.transcribe_btn.config(state="disabled" if busy else "normal")

    def _update_button_states(self) -> None:
        """workspace/busy 상태에 따른 버튼 state 재계산."""
        has_ws = self.workspace is not None
        self.transcribe_btn.config(
            state="disabled" if (self.transcribe_busy or not has_ws) else "normal"
        )
        for btn in self.skill_btns:
            btn.config(state="normal" if has_ws else "disabled")

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log.config(state="normal")
        self.log.insert("end", f"[{ts}] {msg}\n")
        # msg가 내부 개행을 포함할 수 있으므로 그만큼 카운트 증가.
        self._log_line_count += 1 + msg.count("\n")
        if self.log_max_lines > 0 and self._log_line_count > self.log_max_lines:
            excess = self._log_line_count - self.log_max_lines
            # 앞에서 `excess` 줄 삭제 (tkinter Text 인덱스는 1-based).
            self.log.delete("1.0", f"{excess + 1}.0")
            self._log_line_count = self.log_max_lines
        self.log.see("end")
        self.log.config(state="disabled")

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    assert isinstance(payload, str)
                    self._log(payload)
                elif kind == "transcribe_done":
                    self._set_transcribe_busy(False)
                    self._refresh_tree()
                elif kind == "task_done":
                    self._refresh_tree()
                elif kind == "queue_update":
                    self._update_queue_listbox()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
