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

구조:
- `App` 은 Tk View 레이어: 위젯 빌드, 버튼 핸들러, msg_queue 폴링만 담당.
- Skill 큐 로직은 `TaskQueueController`(task_queue.py) 가 소유.
- 출력 파일 쓰기는 `OutputWriter`(output_writer.py) 가 담당.
- Controller/Writer 는 tkinter 를 import 하지 않는다. 둘은 msg_queue 를 통해
  App 에 이벤트를 올린다.
"""

import json
import logging
import logging.handlers
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, scrolledtext, ttk

# .env 파일에서 환경변수 로드 (GEMINI_API_KEY 등)
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass  # python-dotenv 미설치 시 무시 — 환경변수 직접 설정 필요


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
from codex_runner import CodexRunError, load_skill  # noqa: E402
from output_writer import OutputWriter  # noqa: E402
from task_queue import TaskQueueController  # noqa: E402


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


class TkQueueHandler(logging.Handler):
    """logging.Handler: 로그 레코드를 GUI의 msg_queue로 전달.

    Tk 메인 스레드가 `msg_queue`를 `_poll_queue`로 소비하므로, 어느 스레드에서
    `logger.info()`를 호출해도 안전하게 GUI에 도달한다.
    """

    def __init__(self, msg_queue: "queue.Queue[tuple[str, object]]"):
        super().__init__()
        self._q = msg_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._q.put(("log", self.format(record)))
        except Exception:
            self.handleError(record)


def _setup_logging(msg_queue: "queue.Queue[tuple[str, object]]") -> None:
    """프로세스 전체 logging 구성. GUI 핸들러 + (가능하면) 회전 파일 핸들러.

    `lecture_pipeline` 네임스페이스에만 핸들러를 붙인다. codex_runner 등이
    `logging.getLogger("lecture_pipeline.codex_runner")`를 쓰므로 전부 여기로
    라우팅. root 로거는 건드리지 않아 외부 라이브러리 로그는 흡수하지 않음.
    """
    root = logging.getLogger("lecture_pipeline")
    root.setLevel(logging.DEBUG)
    # root 로거까지 bubble up 방지 (이미 자체 핸들러로 소비 완료).
    root.propagate = False
    # 재구성 시 중복 부착 방지.
    for h in list(root.handlers):
        root.removeHandler(h)

    gui_handler = TkQueueHandler(msg_queue)
    gui_handler.setLevel(logging.INFO)
    gui_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(gui_handler)

    # 파일 로그(DEBUG 포함). 실패해도 GUI는 계속 동작.
    try:
        log_dir = SCRIPT_DIR / ".logs"
        log_dir.mkdir(exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "lecture_pipeline.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        ))
        root.addHandler(file_handler)
    except OSError:
        pass


class App:
    """Tk View. 큐/실행/출력 로직은 controller/writer 에 위임."""

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

        # Codex/Claude subprocess 전역 동시 실행 한도.
        # 단순 task 는 task 1개 = subprocess 1개이지만, composite skill 이
        # 내부에서 fan-out 할 때는 task 수 < subprocess 수 가 되므로 별도 설정이
        # 합리적. `gui_config.json` 에 명시 안 하면 task 한도를 따라감.
        try:
            self.max_concurrent_codex = int(
                gui_cfg.get("max_concurrent_codex", self.max_concurrent_tasks)
            )
        except (TypeError, ValueError):
            self.max_concurrent_codex = self.max_concurrent_tasks
        if self.max_concurrent_codex < 1:
            self.max_concurrent_codex = 1
        codex_runner.set_max_concurrent(self.max_concurrent_codex)

        self.workspace: Path | None = None
        self.transcribe_busy = False
        self.msg_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()

        # logging: codex_runner 등이 사용하는 `lecture_pipeline` 네임스페이스
        # 로거에 GUI 핸들러 + 회전 파일 핸들러 부착.
        _setup_logging(self.msg_queue)

        # 출력 쓰기 + 큐 컨트롤러. controller 생성과 동시에 디스패처 스레드 가동.
        self.writer = OutputWriter(self.msg_queue)
        self.controller = TaskQueueController(
            max_concurrent_tasks=self.max_concurrent_tasks,
            msg_queue=self.msg_queue,
            output_writer=self.writer,
        )

        self._build_ui()
        self._poll_queue()
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

        # Status bar 를 `top` 다음에 side=bottom 으로 먼저 확보해서 최하단 고정.
        # 이후 pack 되는 mid/queue/log 는 top ~ status 사이 cavity 를 채운다.
        self._build_status_bar()

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

        self._claude_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            right,
            text="Claude Only 모드",
            variable=self._claude_only_var,
        ).pack(anchor="w", pady=(2, 4))

        ttk.Label(
            right, text="Skills (파일 선택 후 클릭):"
        ).pack(anchor="w", pady=(4, 2))

        # Refresh files 를 먼저 side=bottom 으로 확보해서 하단 고정.
        # 그 다음 skills 스크롤 영역이 중간 cavity 를 채운다.
        ttk.Button(
            right, text="Refresh files", command=self._refresh_tree
        ).pack(side="bottom", fill="x", pady=(8, 0))

        self._build_skills_scroller(right)

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

    def _build_skills_scroller(self, parent: ttk.Frame) -> None:
        """Skills 버튼 목록을 Canvas 기반 스크롤 영역에 배치.

        고정폭 `right` 패널 안에서 버튼이 창 높이를 넘을 때 세로 스크롤을
        제공한다. `parent.pack_propagate(False)` 전제 하에 작동 — 캔버스는
        남은 수직 cavity 를 전부 채운다.
        """
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True, pady=(2, 0))

        canvas = tk.Canvas(wrap, highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_configure(_event: object) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event: tk.Event) -> None:
            canvas.itemconfigure(inner_id, width=event.width)

        inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        # Windows 휠 — Canvas 위에 커서가 있을 때만 바인딩해서 로그창 스크롤과
        # 충돌하지 않게 한다.
        def _on_mousewheel(event: tk.Event) -> None:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind(
            "<Enter>",
            lambda _e: canvas.bind_all("<MouseWheel>", _on_mousewheel),
        )
        canvas.bind(
            "<Leave>",
            lambda _e: canvas.unbind_all("<MouseWheel>"),
        )

        self.skill_btns: list[ttk.Button] = []
        for skill_dir in self._discover_skills():
            btn = ttk.Button(
                inner,
                text=skill_dir.name,
                command=lambda s=skill_dir: self.on_skill(s),
                state="disabled",
            )
            btn.pack(fill="x", pady=2)
            self.skill_btns.append(btn)

        if not self.skill_btns:
            ttk.Label(
                inner, text="(no skills found)", foreground="#999"
            ).pack(pady=4)

    def _build_status_bar(self) -> None:
        """하단 성능 표시 바.

        - Agent worker 가동률: Codex + Goose subprocess 동시 실행 수 / 한도
          (progressbar + 숫자). Goose 도 codex_runner 의 semaphore 를 공유하므로
          한 지표로 충분하다.
        - Tasks: controller snapshot 의 running / pending 수.
        - Waiting: semaphore 에 막혀 acquire 대기 중인 스레드 수 (composite skill
          fan-out 시 증가).
        """
        bar = ttk.Frame(self.root, relief="sunken", padding=(8, 3))
        bar.pack(side="bottom", fill="x")

        ttk.Label(bar, text="Agent:").pack(side="left")
        self.status_bar_pb = ttk.Progressbar(
            bar,
            mode="determinate",
            length=140,
            maximum=max(1, self.max_concurrent_codex),
        )
        self.status_bar_pb.pack(side="left", padx=(6, 6))
        self.status_worker_label = ttk.Label(
            bar, text=f"0/{self.max_concurrent_codex}", width=8
        )
        self.status_worker_label.pack(side="left")

        ttk.Separator(bar, orient="vertical").pack(
            side="left", fill="y", padx=10
        )

        self.status_task_label = ttk.Label(
            bar, text="Tasks: 0 running, 0 pending"
        )
        self.status_task_label.pack(side="left")

        ttk.Separator(bar, orient="vertical").pack(
            side="left", fill="y", padx=10
        )

        self.status_wait_label = ttk.Label(
            bar, text="Waiting: 0", foreground="#888"
        )
        self.status_wait_label.pack(side="left")

        # 주기적 업데이트 시작.
        self._poll_stats()

    def _poll_stats(self) -> None:
        try:
            cs = codex_runner.stats()
            running, pending = self.controller.snapshot()
        except Exception:
            self.root.after(1000, self._poll_stats)
            return

        capacity = max(1, int(cs.get("capacity", 1)))
        in_flight = int(cs.get("in_flight", 0))
        waiters = int(cs.get("waiters", 0))

        # set_max_concurrent 로 한도가 바뀌었을 가능성 대비.
        if int(self.status_bar_pb["maximum"]) != capacity:
            self.status_bar_pb.configure(maximum=capacity)
        self.status_bar_pb["value"] = in_flight
        self.status_worker_label.config(text=f"{in_flight}/{capacity}")
        self.status_task_label.config(
            text=f"Tasks: {len(running)} running, {len(pending)} pending"
        )
        wait_fg = "#c62828" if waiters > 0 else "#888"
        self.status_wait_label.config(
            text=f"Waiting: {waiters}", foreground=wait_fg
        )

        self.root.after(500, self._poll_stats)

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
    # Skill 실행 — 큐 예약 방식 (위임)
    # ─────────────────────────────────────────────────────────

    def on_skill(self, skill_dir: Path):
        """버튼 클릭: 큐에 추가. 스킬 config 의 `ask_file_order` / `ask_conditioning`
        플래그를 보고 필요한 입력 모달 실행 후 controller.enqueue 로 전달."""
        if not self.workspace:
            return
        files = self._get_selected_files()
        if not files:
            self._log(
                f"[{skill_dir.name}] 선택된 파일 없음. 먼저 파일을 선택하세요."
            )
            return

        # config 로드해서 ask_* 플래그 확인
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

        conditioning_text: str | None = None
        if bool(skill.config.get("ask_conditioning", False)):
            from conditioning_dialog import ConditioningDialog
            cdlg = ConditioningDialog(
                self.root, files[0],
                title=f"{skill_dir.name}: 지시어 입력",
            )
            cdlg.show_modal()
            self.root.wait_window(cdlg)
            if cdlg.result is None:
                self._log(
                    f"[{skill_dir.name}] 지시어 입력 취소 — 큐 추가 안 함"
                )
                return
            conditioning_text = cdlg.result["text"]

        self.controller.enqueue(
            skill_dir=skill_dir,
            files=files,
            workspace=self.workspace,
            claude_only=self._claude_only_var.get(),
            conditioning_text=conditioning_text,
        )

    # ─────────────────────────────────────────────────────────
    # Queue 조작 버튼 핸들러 (controller 위임)
    # ─────────────────────────────────────────────────────────

    def _queue_index_to_pending(self, listbox_idx: int) -> int:
        """listbox 인덱스를 pending queue 인덱스로 변환. 음수면 running 구간."""
        running, _ = self.controller.snapshot()
        return listbox_idx - len(running)

    def on_move_up(self) -> None:
        sel = self.queue_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        q_idx = self._queue_index_to_pending(idx)
        if self.controller.move_pending_up(q_idx):
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
        q_idx = self._queue_index_to_pending(idx)
        if self.controller.move_pending_down(q_idx):
            self._update_queue_listbox()
            new_idx = idx + 1
            self.queue_listbox.selection_clear(0, tk.END)
            self.queue_listbox.selection_set(new_idx)

    def on_cancel_selected(self) -> None:
        sel = self.queue_listbox.curselection()
        if not sel:
            return
        q_idx = self._queue_index_to_pending(sel[0])
        if q_idx < 0:
            # 실행 중인 항목 선택 → 현재 중단과 동일
            self.on_stop_current()
            return
        if self.controller.remove_pending(q_idx) is not None:
            self._update_queue_listbox()

    def on_stop_current(self) -> None:
        self.controller.cancel_current()

    def on_clear_queue(self) -> None:
        self.controller.clear_pending()
        self._update_queue_listbox()

    def _update_queue_listbox(self) -> None:
        prev_sel = self.queue_listbox.curselection()
        prev_idx = prev_sel[0] if prev_sel else None
        self.queue_listbox.delete(0, tk.END)
        running, pending = self.controller.snapshot()
        for task in running:
            self.queue_listbox.insert(
                tk.END, f"▶ {task['label']}  (실행 중)"
            )
        for task in pending:
            self.queue_listbox.insert(tk.END, f"   {task['label']}")
        if prev_idx is not None:
            size = self.queue_listbox.size()
            if 0 <= prev_idx < size:
                self.queue_listbox.selection_set(prev_idx)

    def _on_close(self) -> None:
        self.controller.shutdown()
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
