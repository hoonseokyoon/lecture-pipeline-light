"""재사용 가능한 파일 순서 지정 모달 다이얼로그.

사용 예:
    dlg = FileOrderDialog(parent_root, files, title="입력 순서 지정")
    parent_root.wait_window(dlg)
    if dlg.result is not None:
        ordered_files = dlg.result  # list[Path]
    else:
        # 사용자 취소
        pass

확장자별로 파일을 그룹핑하고, 각 그룹을 Listbox 로 표시. 사용자가 ↑↓ 버튼으로
순서 조정. OK 누르면 모든 그룹의 파일이 그룹 순서 → 그룹 내 사용자 순서 로 합쳐져
`self.result` 에 flat list[Path] 로 저장.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable


class FileOrderDialog(tk.Toplevel):
    """확장자 그룹별 Listbox + ↑↓ 버튼으로 파일 순서를 지정하는 모달.

    Attributes:
        result: OK 로 닫히면 정렬된 `list[Path]`, Cancel/ESC/X 면 `None`.
    """

    # 확장자 그룹 정렬 우선순위 (이 순서대로 위에 배치)
    _GROUP_PRIORITY = ["pdf", "txt"]

    def __init__(
        self,
        parent: tk.Misc,
        files: list[Path],
        title: str = "파일 순서 지정",
        explain_text: str | None = None,
        group_by: Callable[[Path], str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.title(title)
        self.result: list[Path] | None = None
        self._group_by = group_by or self._default_group_by

        # 윈도우 속성
        self.resizable(True, True)
        self.minsize(500, 360)

        # 부모 윈도우 중앙
        self.transient(parent)
        try:
            parent.update_idletasks()
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
            ww, wh = 620, 440
            x = px + (pw - ww) // 2
            y = py + (ph - wh) // 2
            self.geometry(f"{ww}x{wh}+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            pass

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.bind("<Escape>", lambda e: self._on_cancel())

        # 상단 설명
        if explain_text:
            ttk.Label(
                self, text=explain_text, wraplength=580,
                foreground="#555",
            ).pack(fill="x", padx=12, pady=(12, 4))
        else:
            ttk.Label(
                self,
                text="각 그룹에서 파일을 선택하고 ↑↓ 버튼으로 순서를 조정하세요.",
                wraplength=580, foreground="#555",
            ).pack(fill="x", padx=12, pady=(12, 4))

        # 그룹 컨테이너 (스크롤 지원)
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True, padx=12, pady=(4, 4))

        canvas = tk.Canvas(outer, borderwidth=0, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self._groups_frame = ttk.Frame(canvas)
        self._groups_frame_id = canvas.create_window(
            (0, 0), window=self._groups_frame, anchor="nw",
        )

        def _on_frame_config(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
        def _on_canvas_config(e):
            canvas.itemconfig(self._groups_frame_id, width=e.width)
        self._groups_frame.bind("<Configure>", _on_frame_config)
        canvas.bind("<Configure>", _on_canvas_config)

        # 그룹별 Listbox 구축
        self._group_listboxes: dict[str, tk.Listbox] = {}
        self._group_files: dict[str, list[Path]] = {}
        self._group_order: list[str] = []

        grouped: dict[str, list[Path]] = {}
        for f in files:
            grouped.setdefault(self._group_by(f), []).append(f)
        for files_in_group in grouped.values():
            files_in_group.sort(key=lambda p: p.name.lower())

        # 그룹 순서: priority 우선, 그 외는 알파벳
        all_keys = list(grouped.keys())
        prioritized = [k for k in self._GROUP_PRIORITY if k in all_keys]
        remainder = sorted(k for k in all_keys if k not in prioritized)
        self._group_order = prioritized + remainder

        for group_key in self._group_order:
            self._build_group_frame(group_key, grouped[group_key])

        # 하단 OK/Cancel
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=12, pady=(4, 12))
        ttk.Button(btn_frame, text="취소", command=self._on_cancel).pack(
            side="right"
        )
        ttk.Button(btn_frame, text="OK", command=self._on_ok).pack(
            side="right", padx=(0, 6)
        )

        # 모달 설정은 show() 에서 수행 — headless 테스트는 이를 skip 가능.
        self._modal_ready = False

    def show_modal(self) -> None:
        """다이얼로그를 모달로 띄움. 호출 전까지 윈도우는 visible 이지만 비-모달."""
        if self._modal_ready:
            return
        try:
            self.wait_visibility()
        except tk.TclError:
            pass
        try:
            self.grab_set()
        except tk.TclError:
            pass
        try:
            self.focus_set()
        except tk.TclError:
            pass
        self._modal_ready = True

    @staticmethod
    def _default_group_by(p: Path) -> str:
        ext = p.suffix.lower().lstrip(".")
        return ext or "other"

    def _build_group_frame(self, group_key: str, files: list[Path]) -> None:
        label = f".{group_key}  ({len(files)}개)"
        frame = ttk.LabelFrame(self._groups_frame, text=label, padding=6)
        frame.pack(fill="x", pady=4)

        inner = ttk.Frame(frame)
        inner.pack(fill="x")

        lb = tk.Listbox(
            inner,
            height=min(max(len(files), 3), 8),
            selectmode="browse",
            activestyle="dotbox",
            exportselection=False,
        )
        lb.pack(side="left", fill="x", expand=True)
        scroll = ttk.Scrollbar(inner, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=scroll.set)
        scroll.pack(side="left", fill="y")

        for f in files:
            lb.insert(tk.END, f.name)

        btns = ttk.Frame(inner)
        btns.pack(side="left", fill="y", padx=(6, 0))
        ttk.Button(
            btns, text="↑", width=3,
            command=lambda g=group_key: self._move(g, -1),
        ).pack(fill="x", pady=1)
        ttk.Button(
            btns, text="↓", width=3,
            command=lambda g=group_key: self._move(g, 1),
        ).pack(fill="x", pady=1)

        self._group_listboxes[group_key] = lb
        self._group_files[group_key] = list(files)

    def _move(self, group_key: str, delta: int) -> None:
        lb = self._group_listboxes[group_key]
        files = self._group_files[group_key]
        sel = lb.curselection()
        if not sel:
            return
        idx = sel[0]
        new_idx = idx + delta
        if not (0 <= new_idx < len(files)):
            return
        # swap in list + listbox
        files[idx], files[new_idx] = files[new_idx], files[idx]
        lb.delete(0, tk.END)
        for f in files:
            lb.insert(tk.END, f.name)
        lb.selection_clear(0, tk.END)
        lb.selection_set(new_idx)
        lb.see(new_idx)

    def _on_ok(self) -> None:
        ordered: list[Path] = []
        for group_key in self._group_order:
            ordered.extend(self._group_files[group_key])
        self.result = ordered
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    def _on_cancel(self) -> None:
        self.result = None
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
