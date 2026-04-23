"""프로젝트 Overview — PIR / RFI / git log / 환경."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st

from gui_lit.app_state import render_sidebar, require_project
from gui_lit import ipc


st.set_page_config(page_title="Overview", page_icon="📋", layout="wide")
render_sidebar()

info = require_project()
st.title("📋 Overview")
st.caption(str(info.root))

col_l, col_r = st.columns([3, 2])

with col_l:
    st.subheader("PIR (Priority of Intelligence)")
    if info.pir_path.exists():
        st.markdown(info.pir_path.read_text(encoding="utf-8"))
    else:
        st.warning("PIR 파일 없음")

    st.divider()
    st.subheader("RFI (Request for Information)")
    if info.rfi_path.exists():
        st.markdown(info.rfi_path.read_text(encoding="utf-8"))
    else:
        st.warning("RFI 파일 없음")

with col_r:
    st.subheader("현재 세션 / 브랜치")
    sid = ipc.read_current_session(info.root)
    if sid:
        st.code(f"session: {sid}")
    else:
        st.caption("세션 없음 (첫 Chat 호출 시 생성됨)")

    try:
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=info.root, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        st.code(f"branch: {branch or '(unknown)'}")
    except (subprocess.SubprocessError, FileNotFoundError):
        st.caption("git 실행 불가")

    if ipc.is_halted(info.root):
        st.error("HALT 플래그 설정됨")
        if st.button("Halt 해제"):
            ipc.clear_halt(info.root)
            st.rerun()
    else:
        if st.button("Halt 요청 (에이전트 정지)"):
            ipc.set_halt(info.root, reason="user requested from overview")
            st.rerun()

    st.divider()
    st.subheader("Git log (최근 20)")
    try:
        log = subprocess.run(
            ["git", "log", "--oneline", "-20", "--all", "--decorate"],
            cwd=info.root, capture_output=True, text=True, timeout=5,
        ).stdout
        st.code(log or "(empty)", language="bash")
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        st.caption(f"git log 실패: {exc}")

    st.divider()
    st.subheader("DB Stats")
    try:
        from gui_lit import db
        db.init_schema(info.root)
        with db.connect(info.root) as conn:
            total = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            by_status = conn.execute(
                "SELECT status, COUNT(*) FROM papers GROUP BY status"
            ).fetchall()
        st.metric("전체 논문", total)
        for status, count in by_status:
            st.caption(f"  {status}: {count}")
    except Exception as exc:
        st.caption(f"DB 조회 실패: {exc}")
