"""자연어 지시어(referring expression) 입력용 모달 다이얼로그.

사용 예:
    dlg = ConditioningDialog(parent_root, input_file, title="지시어 입력")
    dlg.show_modal()
    parent_root.wait_window(dlg)
    if dlg.result is not None:
        text = dlg.result["text"]
    else:
        # 사용자 취소
        pass

config.json에 `ask_conditioning: true`가 있는 skill(예: zero_shot_rec)에서
gui.py의 on_skill이 호출. 취소/빈 텍스트 → result=None.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk


class ConditioningDialog(tk.Toplevel):
    """자연어 지시어 입력 모달.

    Attributes:
        result: OK로 닫히면 {"text": str}, Cancel/ESC/X면 None.
    """

    def __init__(
        self,
        parent: tk.Misc,
        input_file: Path,
        title: str = "지시어 입력",
        explain_text: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.title(title)
        self.result: dict | None = None

        self.resizable(True, True)
        self.minsize(480, 260)

        self.transient(parent)
        try:
            parent.update_idletasks()
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
            ww, wh = 560, 300
            x = px + (pw - ww) // 2
            y = py + (ph - wh) // 2
            self.geometry(f"{ww}x{wh}+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            pass

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.bind("<Escape>", lambda e: self._on_cancel())

        # 안내
        ttk.Label(
            self,
            text=f"대상 이미지: {input_file.name}",
            foreground="#333",
        ).pack(fill="x", padx=12, pady=(12, 2))

        explain = explain_text or (
            "이미지 속 찾고 싶은 객체를 자연어로 기술하세요.\n"
            "예: \"the red car on the left\", \"왼쪽에서 두 번째 사람이 들고 있는 가방\""
        )
        ttk.Label(
            self, text=explain, wraplength=520, foreground="#555",
            justify="left",
        ).pack(fill="x", padx=12, pady=(0, 8))

        # 텍스트 입력 (multi-line)
        self._text = tk.Text(self, height=4, wrap="word", font=("Segoe UI", 11))
        self._text.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        self._text.focus_set()

        # Ctrl+Enter로 제출, Enter는 일반 개행 유지
        self._text.bind("<Control-Return>", lambda e: self._on_ok())

        ttk.Label(
            self, text="Ctrl+Enter로 제출 · Esc로 취소",
            foreground="#888",
        ).pack(fill="x", padx=12, pady=(0, 4))

        # 버튼
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=12, pady=(4, 12))
        ttk.Button(btn_frame, text="취소", command=self._on_cancel).pack(
            side="right"
        )
        ttk.Button(btn_frame, text="OK", command=self._on_ok).pack(
            side="right", padx=(0, 6)
        )

        self._modal_ready = False

    def show_modal(self) -> None:
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
            self._text.focus_set()
        except tk.TclError:
            pass
        self._modal_ready = True

    def _on_ok(self) -> None:
        text = self._text.get("1.0", "end").strip()
        if not text:
            # 빈 입력은 거부 — 다이얼로그 유지, 사용자에게 피드백
            try:
                self.bell()
            except tk.TclError:
                pass
            return
        self.result = {"text": text}
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
