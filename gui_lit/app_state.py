"""Streamlit 페이지들이 공유하는 상태 헬퍼.

st.session_state 에 project_root / project 를 보관.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# 프로젝트 루트에 lecture-pipeline-light 을 sys.path 에 추가 — harness import 용
_HARNESS_ROOT = Path(__file__).resolve().parent.parent
if str(_HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(_HARNESS_ROOT))

# dotenv 로딩 (MISTRAL_API_KEY, GEMINI_API_KEY)
try:
    from dotenv import load_dotenv
    load_dotenv(_HARNESS_ROOT / ".env", override=False)
except ImportError:
    pass

from gui_lit import project as project_mod  # noqa: E402


HARNESS_ROOT = _HARNESS_ROOT


def current_project() -> project_mod.ProjectInfo | None:
    root = st.session_state.get("project_root")
    if not root:
        return None
    try:
        return project_mod.load_project(Path(root))
    except project_mod.ProjectError:
        st.session_state.pop("project_root", None)
        return None


def set_project(root: Path) -> project_mod.ProjectInfo:
    info = project_mod.load_project(root)
    st.session_state["project_root"] = str(info.root)
    project_mod.push_recent(info.root, info.name)
    return info


def clear_project() -> None:
    st.session_state.pop("project_root", None)


def require_project() -> project_mod.ProjectInfo:
    info = current_project()
    if info is None:
        st.warning("먼저 프로젝트를 열거나 생성하세요. (좌측 메뉴 → Home)")
        st.stop()
    return info


def render_sidebar() -> None:
    """모든 페이지에서 호출 — 좌측 프로젝트 상태 표시."""
    st.sidebar.header("📚 gui_lit")
    info = current_project()
    if info is None:
        st.sidebar.info("프로젝트가 열려있지 않습니다.")
        st.sidebar.page_link("app.py", label="Home 으로")
        return

    st.sidebar.success(f"**{info.name}**")
    st.sidebar.caption(str(info.root))
    if st.sidebar.button("프로젝트 닫기", use_container_width=True):
        clear_project()
        st.rerun()
