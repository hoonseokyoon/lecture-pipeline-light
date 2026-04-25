"""Chat — Head agent 와의 대화 UI.

구조:
1. 상단 고정 RFI 배너 (id/status/branch/session/progress)
2. 과거 이벤트 타임라인 — user/assistant 메시지 + 중간 tool 이벤트(collapsed)
3. 마지막 assistant 아래 outcome pill (duration/tokens/cost)
4. 작업 중일 때: st.status 블록에 journal 새 이벤트 live tail
5. Chat 입력 + 빠른 액션 (작업 중 disabled)

진실의 원천: `.litproj/journal.jsonl` — streaming 전환 시 자동으로 실시간화됨.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st

from gui_lit.app_state import render_sidebar, require_project
from gui_lit import head_agent, ipc, db
from gui_lit.chat_widgets import (
    render_chat_timeline,
    render_live_tail,
    render_outcome_pill,
    render_rfi_banner,
    render_session_manager,
)


st.set_page_config(page_title="Chat", page_icon="💬", layout="wide")
render_sidebar()

info = require_project()

st.title("💬 Chat with Head Agent")

# ── 상태 초기화 ──
if "agent_run" not in st.session_state:
    st.session_state["agent_run"] = None
if "last_agent_outcome" not in st.session_state:
    st.session_state["last_agent_outcome"] = None
if "last_seen_ts" not in st.session_state:
    # live tail 기준선 — 작업 시작 직전 ts
    st.session_state["last_seen_ts"] = None


def _get_or_create_agent() -> head_agent.HeadAgent:
    """프로젝트 루트별 HeadAgent 를 session_state 에 캐시.

    Streamlit 은 rerun 마다 스크립트를 다시 실행하지만 session_state 는 유지
    된다. 매번 새 HeadAgent 를 만들면 cancel_active 가 **전혀 다른 인스턴스**
    에서 동작해 실제 subprocess 를 죽이지 못함 (치명적 버그).
    """
    cache = st.session_state.setdefault("head_agents", {})
    key = str(info.root)
    existing = cache.get(key)
    if existing is None:
        existing = head_agent.HeadAgent(info.root)
        cache[key] = existing
    return existing


# ── DB 통계 (배너용) ──

def _db_stats() -> dict[str, int]:
    try:
        db.init_schema(info.root)
        stats = {"total": 0}
        with db.connect(info.root) as conn:
            total = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            stats["total"] = total
            cur = conn.execute(
                "SELECT status, COUNT(*) FROM papers GROUP BY status"
            )
            for row in cur.fetchall():
                stats[row["status"]] = row[1]
        return stats
    except Exception:
        return {"total": 0}


# ── Done 처리 (events 렌더 전에 선행 — journal 재읽기 보장) ──

run_state = st.session_state.get("agent_run")

if run_state is not None and run_state.is_done():
    if run_state.error is not None:
        st.session_state["last_agent_outcome"] = {
            "kind": "error",
            "message": str(run_state.error),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    else:
        result = run_state.result
        if result is not None:
            raw = result.raw_json or {}
            usage = raw.get("usage") or {}
            st.session_state["last_agent_outcome"] = {
                "kind": "ok",
                "duration_s": result.duration_s,
                "session": result.session_id,
                "cost_usd": raw.get("total_cost_usd"),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "num_turns": raw.get("num_turns"),
                "response_preview": (result.response_text or "")[:800],
                "response_empty": not bool(result.response_text),
                "raw_keys": list(raw.keys()),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
    st.session_state["agent_run"] = None
    st.session_state["last_seen_ts"] = None
    # 완료 직후 rerun 해 journal 최신 상태로 다시 진입
    st.rerun()


# ── 상단 RFI 배너 ──

render_rfi_banner(
    info,
    session_id=ipc.read_current_session(info.root),
    db_stats=_db_stats(),
)


# ── 세션 관리 ──

def _on_resume(sid: str) -> None:
    ipc.write_current_session(info.root, sid)
    ipc.append_journal(
        info.root,
        {"actor": "user", "kind": "session_resumed", "session": sid},
    )
    ipc.record_session_event(info.root, sid, "used")
    st.session_state["last_agent_outcome"] = None
    st.rerun()


def _on_fresh() -> None:
    prev = ipc.read_current_session(info.root)
    ipc.clear_current_session(info.root)
    if prev:
        ipc.record_session_event(info.root, prev, "abandoned")
    ipc.append_journal(
        info.root,
        {"actor": "user", "kind": "session_fresh_requested", "prev": prev},
    )
    st.session_state["last_agent_outcome"] = None
    st.rerun()


# is_running 계산은 아래에서 다시 하지만, 세션 UI 는 이 시점에 필요
_early_run_state = st.session_state.get("agent_run")
_early_is_running = _early_run_state is not None and not _early_run_state.is_done()

render_session_manager(
    ipc.list_known_sessions(info.root),
    current_sid=ipc.read_current_session(info.root),
    on_resume=_on_resume,
    on_fresh=_on_fresh,
    is_running=_early_is_running,
)


# ── 타임라인 (과거 이벤트) ──

events = ipc.read_journal(info.root, limit=500)
if not events:
    st.info(
        "아직 대화가 없습니다. 아래 어떤 형태든 가능합니다:\n\n"
        "**💬 대화 / 의견 요청**\n"
        "- `PIR 초안 봐주고 개선점 알려줘`\n"
        "- `diffusion TTS artifact 주제가 너무 좁을까?`\n"
        "- `지금 어느 단계에 있지? 다음에 뭘 해야 할까?`\n\n"
        "**🔧 작업 지시 (슬래시 또는 자연어)**\n"
        "- `/rfi-open Diffusion TTS artifact survey`\n"
        "- `PIR-1 에 대한 첫 RFI 로 X 주제를 열어줘`\n"
        "- `/compile-review`\n\n"
        "에이전트가 메시지를 보고 **대화 모드 / 작업 모드** 를 판단해서 "
        "적절히 답하거나 파이프라인을 돌립니다."
    )
else:
    outcome = st.session_state.get("last_agent_outcome")
    render_chat_timeline(events, outcome=outcome)


# ── 작업 중이면 live tail ──

run_state = st.session_state.get("agent_run")
is_running = run_state is not None and not run_state.is_done()

if is_running:
    # live tail 기준선 설정 — 첫 진입 시만
    if st.session_state["last_seen_ts"] is None and events:
        st.session_state["last_seen_ts"] = events[-1].get("ts", "")

    with st.status(
        "🔄 에이전트 작업 중…", state="running", expanded=True,
    ) as status:
        since = st.session_state["last_seen_ts"] or ""
        new_events = ipc.read_journal(info.root, since_ts=since)
        # user_message 는 본 대화 트랙에 보이므로 live tail 에서는 제외
        middle_events = [
            e for e in new_events
            if e.get("kind") not in ("user_message",)
        ]
        render_live_tail(middle_events)

        st.markdown("---")
        cancel_col, hint_col = st.columns([1, 3])
        with cancel_col:
            if st.button("⏹ 취소", type="secondary"):
                _get_or_create_agent().cancel_active()
        with hint_col:
            st.caption("Observer 페이지에서 전체 journal 흐름 확인 가능.")

    # 3초 후 재렌더 — 너무 공격적이지 않게
    time.sleep(3.0)
    st.rerun()


# ── 에러·완료 pill (chat timeline 에서 이미 마지막 assistant 아래 붙지만,
#    만약 마지막이 assistant 가 아니라 에러였다면 별도 표시) ──

outcome = st.session_state.get("last_agent_outcome")
if outcome is not None and outcome.get("kind") == "error":
    render_outcome_pill(outcome)


# ── 입력 ──

msg = st.chat_input(
    "Head agent 에게 메시지 입력…",
    disabled=is_running,
)
if msg:
    try:
        agent = _get_or_create_agent()
    except head_agent.HeadAgentError as exc:
        st.error(f"Head agent 초기화 실패: {exc}")
    else:
        # live tail 기준선 리셋
        current_events = ipc.read_journal(info.root, limit=1)
        st.session_state["last_seen_ts"] = (
            current_events[-1].get("ts", "") if current_events else ""
        )
        st.session_state["agent_run"] = agent.send_async(msg)
        st.session_state["last_agent_outcome"] = None
        st.rerun()


# ── 빠른 액션 ──

with st.expander("빠른 액션 (작업 모드 전용)", expanded=False):
    st.caption(
        "이 버튼들은 즉시 파이프라인을 가동시킵니다. 단순 질문·의견은 "
        "위 입력창에 자연어로 입력하세요 — 에이전트가 대화 모드로 처리합니다."
    )
    quick_actions = [
        ("/rfi-open", "새 RFI 시작 (제목 제시가 필요하면 말미에 덧붙일 것)"),
        ("/rfi-followup", "기존 RFI에 조건부 추가조사/addendum 생성"),
        ("/rfi-close", "현재 RFI 마무리"),
        ("/lit-triage", "최근 검색 결과 triage"),
        ("/compile-review", "리뷰 문서 생성"),
    ]
    cols = st.columns(len(quick_actions))
    for idx, (cmd, desc) in enumerate(quick_actions):
        if cols[idx].button(
            cmd, help=desc, use_container_width=True, disabled=is_running,
        ):
            try:
                agent = _get_or_create_agent()
            except head_agent.HeadAgentError as exc:
                st.error(f"{exc}")
            else:
                current_events = ipc.read_journal(info.root, limit=1)
                st.session_state["last_seen_ts"] = (
                    current_events[-1].get("ts", "") if current_events else ""
                )
                st.session_state["agent_run"] = agent.send_async(cmd)
                st.session_state["last_agent_outcome"] = None
                st.rerun()
