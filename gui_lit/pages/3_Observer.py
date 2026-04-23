"""Observer — 실시간 journal + 파일 트리 + git 변경분.

에이전트 작업을 방해하지 않고 관찰하는 용도. 주기적 자동 새로고침.
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st

from gui_lit.app_state import render_sidebar, require_project
from gui_lit import ipc


st.set_page_config(page_title="Observer", page_icon="🔭", layout="wide")
render_sidebar()

info = require_project()

st.title("🔭 Observer")
st.caption(str(info.root))

ctrl_col1, ctrl_col2, ctrl_col3 = st.columns(3)
with ctrl_col1:
    auto_refresh = st.toggle("자동 새로고침 (2s)", value=True)
with ctrl_col2:
    limit = st.number_input(
        "Journal 표시 개수", min_value=10, max_value=2000, value=100, step=10,
    )
with ctrl_col3:
    if st.button("수동 새로고침"):
        st.rerun()

st.divider()

# ── Journal ──

st.subheader("Journal (.litproj/journal.jsonl)")
events = ipc.read_journal(info.root, limit=int(limit))
if not events:
    st.caption("(이벤트 없음)")
else:
    rows = []
    for ev in events[::-1]:  # 최신 먼저
        ts = ev.get("ts", "")
        try:
            ts_short = datetime.fromisoformat(
                ts.replace("Z", "+00:00")
            ).strftime("%H:%M:%S")
        except ValueError:
            ts_short = ts[:19]
        content = ev.get("content", "")
        if not content:
            # 요약 필드 모으기
            parts = []
            for k, v in ev.items():
                if k in ("ts", "kind", "actor"):
                    continue
                if isinstance(v, (str, int, float)):
                    parts.append(f"{k}={v}")
            content = ", ".join(parts[:4])
        rows.append(
            {
                "ts": ts_short,
                "actor": ev.get("actor", ""),
                "kind": ev.get("kind", ""),
                "content": (content or "")[:200],
            }
        )
    st.dataframe(rows, hide_index=True, use_container_width=True)

st.divider()

col_g, col_f = st.columns([1, 1])

with col_g:
    st.subheader("Git status")
    try:
        out = subprocess.run(
            ["git", "status", "--short"],
            cwd=info.root, capture_output=True, text=True, timeout=5,
        ).stdout
        st.code(out or "(clean)", language="bash")
    except Exception as exc:
        st.caption(f"git status 실패: {exc}")

    st.subheader("Git log (최근 15, all branches)")
    try:
        out = subprocess.run(
            ["git", "log", "--oneline", "-15", "--all", "--decorate"],
            cwd=info.root, capture_output=True, text=True, timeout=5,
        ).stdout
        st.code(out or "(empty)", language="bash")
    except Exception as exc:
        st.caption(f"git log 실패: {exc}")

with col_f:
    st.subheader("Inbox 대기 메시지")
    pending = ipc.list_pending_inbox(info.root)
    if not pending:
        st.caption("(비어있음)")
    else:
        for p in pending:
            st.code(f"{p.name}")

    st.subheader("주요 디렉토리")
    for sub in ("originals/papers", "extracted", "agent-docs/reviews",
                "agent-docs/summaries", "agent-docs/rfi"):
        d = info.root / sub
        if d.is_dir():
            try:
                n = sum(1 for _ in d.iterdir())
            except OSError:
                n = 0
            st.caption(f"`{sub}`: {n}개")

if auto_refresh:
    time.sleep(2.0)
    st.rerun()
