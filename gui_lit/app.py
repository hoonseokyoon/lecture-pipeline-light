"""gui_lit — Streamlit entrypoint.

실행:
    streamlit run gui_lit/app.py

기능:
- 프로젝트 생성 (init)
- 기존 프로젝트 열기 (open)
- 최근 프로젝트 목록
- 환경 점검 (claude CLI, API keys)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Streamlit 은 script dir 만 sys.path 에 넣으므로, gui_lit 패키지 import 를 위해
# 부모(repo 루트) 를 먼저 추가. 이 블록은 아래 `from gui_lit...` 보다 앞에 와야 함.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st

from gui_lit.app_state import (
    HARNESS_ROOT,
    clear_project,
    current_project,
    render_sidebar,
    set_project,
)
from gui_lit import head_agent, project as project_mod


st.set_page_config(
    page_title="gui_lit — Literature Research",
    page_icon="📚",
    layout="wide",
)


def _env_check() -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []

    ok_claude, msg_claude = head_agent.check_cli_available()
    checks.append(("Claude Code CLI", ok_claude, msg_claude))

    checks.append(
        (
            "MISTRAL_API_KEY (.env)",
            bool(os.environ.get("MISTRAL_API_KEY", "").strip()),
            "doc_to_md 스킬용",
        )
    )
    checks.append(
        (
            "GEMINI_API_KEY (.env)",
            bool(os.environ.get("GEMINI_API_KEY", "").strip()),
            "lit_summarize 스킬용",
        )
    )
    return checks


def _recent_projects_ui() -> None:
    st.subheader("최근 프로젝트")
    recent = project_mod.load_recent()
    if not recent:
        st.caption("아직 프로젝트가 없습니다.")
        return
    for item in recent:
        root = Path(item["root"])
        col1, col2, col3 = st.columns([4, 3, 2])
        col1.markdown(f"**{item['name']}**")
        col2.caption(str(root))
        if col3.button("Open", key=f"open-{root}"):
            try:
                set_project(root)
                st.rerun()
            except project_mod.ProjectError as exc:
                st.error(f"열기 실패: {exc}")


def _open_project_ui() -> None:
    st.subheader("기존 프로젝트 열기")
    path = st.text_input(
        "프로젝트 경로",
        placeholder="/path/to/existing-project",
        key="open_path",
    )
    if st.button("Open", key="btn-open-existing", disabled=not path):
        try:
            set_project(Path(path))
            st.rerun()
        except project_mod.ProjectError as exc:
            st.error(f"{exc}")


def _new_project_ui() -> None:
    st.subheader("새 프로젝트 생성")
    with st.form("new-project-form"):
        root = st.text_input(
            "프로젝트 경로 (신규)",
            placeholder="/path/to/new-project",
        )
        name = st.text_input(
            "프로젝트 이름 (옵션, 미지정 시 폴더명)", placeholder="",
        )
        git_name = st.text_input("Git user.name (선택)", value="")
        git_email = st.text_input("Git user.email (선택)", value="")
        overwrite = st.checkbox(
            "경로에 기존 파일 있어도 강제 덮어쓰기", value=False,
        )
        submitted = st.form_submit_button("Create", type="primary")
    if submitted:
        if not root.strip():
            st.error("경로를 입력하세요.")
            return
        try:
            info = project_mod.init_project(
                Path(root.strip()),
                name=name.strip() or None,
                harness_root=HARNESS_ROOT,
                git_user_name=git_name.strip() or None,
                git_user_email=git_email.strip() or None,
                overwrite=overwrite,
            )
            st.success(f"생성됨: {info.root}")
            set_project(info.root)
            st.rerun()
        except project_mod.ProjectError as exc:
            st.error(f"생성 실패: {exc}")
        except OSError as exc:
            st.error(f"경로 오류: {exc}")


def main() -> None:
    render_sidebar()
    st.title("📚 gui_lit — Literature Research")

    info = current_project()
    if info is not None:
        st.success(f"현재 열린 프로젝트: **{info.name}**")
        st.caption(str(info.root))
        st.markdown(
            "좌측 네비게이션의 **Overview / Chat / Observer / Papers** 로 진행하세요."
        )
        if st.button("프로젝트 닫기"):
            clear_project()
            st.rerun()
        return

    # 환경 점검
    with st.expander("환경 점검", expanded=False):
        for label, ok, msg in _env_check():
            icon = "✅" if ok else "⚠️"
            st.write(f"{icon} **{label}** — {msg}")

    tab_recent, tab_open, tab_new = st.tabs(
        ["최근 프로젝트", "기존 프로젝트 열기", "새 프로젝트 생성"]
    )
    with tab_recent:
        _recent_projects_ui()
    with tab_open:
        _open_project_ui()
    with tab_new:
        _new_project_ui()


if __name__ == "__main__":
    main()
