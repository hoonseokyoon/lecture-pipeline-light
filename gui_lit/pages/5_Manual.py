"""Manual — 자동 획득 실패 논문의 수동 drop / abstract-only 요약 허브.

자동 다운로드가 실패한 논문을 hybrid 방식으로 처리:
- 사용자가 Chrome 에서 직접 다운로드 → 이 페이지에서 업로드 (drop zone)
- 또는 abstract-only 요약으로 진행 (본문 없이 초록만으로)
- 또는 그냥 제외 (triage drop)

자동으로 후속 파이프라인 연결:
- PDF 업로드 → `originals/papers/<slug>.pdf` 저장 → doc_to_md → 요약 재실행 가능
- abstract-only → lit_summarize (.json 입력 경로) → summaries 생성
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st

from gui_lit.app_state import HARNESS_ROOT, render_sidebar, require_project
from gui_lit import db, ipc


st.set_page_config(page_title="Manual", page_icon="🤝", layout="wide")
render_sidebar()

info = require_project()

st.title("🤝 Manual — Hybrid Workflow")
st.caption(str(info.root))

# ── DB 자동 ingest 새로고침 ──
try:
    db.init_schema(info.root)
    db.ingest_from_filesystem(info.root)
except Exception as exc:
    st.error(f"DB ingest 실패: {exc}")
    st.stop()


# ── Pending 목록 수집 ──
# "pending" = keep/maybe 로 triaged 됐는데 아직 extracted 안 된 것.
#             또는 needs_manual.json 에 기재된 것.

with db.connect(info.root) as conn:
    # triaged 되어 있고 status 가 extracted 이하인 papers
    pending_db_rows = [
        dict(r) for r in conn.execute(
            """
            SELECT p.*,
                   l.decision AS decision,
                   l.rfi_id AS rfi_id,
                   l.reason AS triage_reason
              FROM papers p
         LEFT JOIN paper_rfi_links l ON p.id = l.paper_id
             WHERE p.status IN ('discovered', 'downloaded', 'failed')
               AND (l.decision IN ('keep', 'maybe') OR l.decision IS NULL)
             ORDER BY CASE p.status
                        WHEN 'failed' THEN 0
                        WHEN 'discovered' THEN 1
                        WHEN 'downloaded' THEN 2
                      END,
                      p.year DESC,
                      p.title
            """,
        ).fetchall()
    ]

# needs_manual.json 파일도 병합 (lit_fetch 산출물)
needs_manual_files = list(info.root.rglob("needs_manual.json"))
needs_manual_items: list[dict] = []
for nmf in needs_manual_files:
    if ".git" in nmf.parts:
        continue
    try:
        data = json.loads(nmf.read_text(encoding="utf-8"))
        items = data.get("items", [])
        for it in items:
            if not isinstance(it, dict):
                continue
            it["_source_file"] = str(nmf.relative_to(info.root))
            needs_manual_items.append(it)
    except (OSError, json.JSONDecodeError):
        continue

st.metric("DB pending (keep/maybe, 본문 미확보)", len(pending_db_rows))
if needs_manual_items:
    st.metric("needs_manual.json 누적", len(needs_manual_items))

if not pending_db_rows and not needs_manual_items:
    st.success("✅ 수동 처리 대기 항목이 없습니다.")
    st.caption(
        "lit_search + lit_fetch 실행 결과 모든 keep/maybe 후보가 자동 "
        "다운로드되었거나, 아직 triage 되지 않은 상태입니다."
    )
    st.stop()


# ── Primary action: PDF drop zone (일괄 처리) ──

st.divider()
st.subheader("📥 PDF 일괄 drop zone")
st.caption(
    "Chrome 에서 여러 PDF 를 받아둔 뒤 한꺼번에 드롭하세요. 파일명은 "
    "가능하면 `<year>-<firstauthor>-<keyword>.pdf` 형식 권장 (DB slug 와 매칭)."
)

uploaded = st.file_uploader(
    "PDF 파일 선택 (복수 가능)",
    type=["pdf"],
    accept_multiple_files=True,
    key="bulk_upload",
)

if uploaded:
    originals_dir = info.root / "originals" / "papers"
    originals_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for uf in uploaded:
        dest = originals_dir / uf.name
        dest.write_bytes(uf.getvalue())
        saved.append(str(dest.relative_to(info.root)))

    st.success(f"{len(saved)}개 파일 저장됨:")
    for p in saved:
        st.code(p)
    ipc.append_journal(
        info.root,
        {
            "actor": "user",
            "kind": "manual_pdf_dropped",
            "count": len(saved),
            "files": saved[:20],
        },
    )

    # DB 재ingest (slug 매칭으로 papers.original_path 업데이트)
    with st.spinner("DB ingest 재실행..."):
        stats = db.ingest_from_filesystem(info.root)
        st.info(f"ingest 결과: {stats}")

    if st.button("이제 doc_to_md 로 Mistral OCR 실행", type="primary"):
        pdfs = [info.root / p for p in saved]
        with st.spinner(f"doc_to_md 실행 중 ({len(pdfs)}편)..."):
            cmd = [
                sys.executable,
                str(HARNESS_ROOT / "run_skill.py"),
                "doc_to_md",
                *[str(p) for p in pdfs],
                "--out", str(info.root / "extracted"),
                "--workspace", str(info.root),
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
                )
                if proc.returncode == 0:
                    st.success("doc_to_md 완료 — extracted/ 에 Markdown 생성")
                else:
                    st.error(f"doc_to_md 실패 (exit {proc.returncode})")
                    st.code((proc.stderr or proc.stdout)[-2000:])
            except subprocess.TimeoutExpired:
                st.error("doc_to_md 타임아웃")


# ── Per-item actions ──

st.divider()
st.subheader("📋 개별 논문 처리")
st.caption(
    "각 논문마다 (1) Chrome 에서 열어 직접 다운로드 후 위 drop zone 사용 "
    "또는 (2) abstract-only 요약으로 진행 또는 (3) 제외."
)

# DB 기반 pending 을 우선 렌더 (triaged 된 것이라 우선순위 명확)
for row in pending_db_rows[:50]:  # 일단 상위 50개만
    pid = row.get("id", "?")
    title = row.get("title") or "(untitled)"
    with st.container(border=True):
        cols = st.columns([3, 2, 2])
        with cols[0]:
            st.markdown(f"**[{row.get('year') or '—'}] {title}**")
            st.caption(
                f"ID: `{pid}` · status: `{row.get('status')}` "
                f"· decision: `{row.get('decision') or '-'}`"
            )
            if row.get("venue"):
                st.caption(f"Venue: {row['venue']}")

        with cols[1]:
            # 외부 열기 링크
            link_targets = []
            if row.get("doi"):
                link_targets.append(("DOI", f"https://doi.org/{row['doi']}"))
            if row.get("pdf_url"):
                link_targets.append(("PDF URL", row["pdf_url"]))
            if row.get("pmid"):
                link_targets.append((
                    "PubMed",
                    f"https://pubmed.ncbi.nlm.nih.gov/{row['pmid']}/",
                ))
            for label, url in link_targets:
                st.link_button(
                    f"🌐 {label}",
                    url,
                    use_container_width=True,
                )

        with cols[2]:
            # Abstract-only 요약 트리거
            if row.get("abstract"):
                if st.button(
                    "📝 Abstract-only 요약",
                    key=f"abs-{pid}",
                    use_container_width=True,
                    help="본문 없이 abstract 만으로 요약 생성 (Gemini Flash)",
                ):
                    # candidate JSON 임시 저장 후 lit_summarize 호출
                    tmp_cand = info.root / ".litproj" / "manual_abs" / f"{pid}.json"
                    tmp_cand.parent.mkdir(parents=True, exist_ok=True)
                    cand_dict = {
                        k: row.get(k) for k in (
                            "id", "doi", "arxiv_id", "pmid", "title",
                            "authors", "year", "venue", "abstract", "pdf_url",
                            "source", "citation_count",
                        ) if row.get(k) is not None
                    }
                    tmp_cand.write_text(
                        json.dumps(cand_dict, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    with st.spinner("lit_summarize 실행 중..."):
                        cmd = [
                            sys.executable,
                            str(HARNESS_ROOT / "run_skill.py"),
                            "lit_summarize",
                            str(tmp_cand),
                            "--out", str(info.root / "agent-docs" / "summaries"),
                            "--workspace", str(info.root),
                        ]
                        env = {**__import__("os").environ,
                               "PYTHONIOENCODING": "utf-8",
                               "LIT_RFI_FILE": str(info.rfi_path),
                               "LIT_PIR_FILE": str(info.pir_path)}
                        try:
                            proc = subprocess.run(
                                cmd, capture_output=True, text=True,
                                timeout=300, env=env,
                            )
                            if proc.returncode == 0:
                                st.success("abstract-only 요약 완료")
                                ipc.append_journal(
                                    info.root,
                                    {
                                        "actor": "user",
                                        "kind": "abstract_only_summarized",
                                        "paper_id": pid,
                                    },
                                )
                            else:
                                st.error(f"lit_summarize 실패 (exit {proc.returncode})")
                                st.code((proc.stderr or proc.stdout)[-800:])
                        except subprocess.TimeoutExpired:
                            st.error("lit_summarize 타임아웃")
            else:
                st.caption("_abstract 없음 — 요약 불가_")

            # Drop (triage 에서 제외)
            if st.button("🗑 제외", key=f"drop-{pid}", use_container_width=True):
                rfi_id = row.get("rfi_id") or "unknown"
                with db.connect(info.root) as conn:
                    conn.execute(
                        """
                        INSERT INTO paper_rfi_links(paper_id, rfi_id, decision, reason, ts)
                        VALUES(?, ?, 'drop', 'user manual decline', ?)
                        ON CONFLICT(paper_id, rfi_id) DO UPDATE SET
                            decision='drop', reason=excluded.reason, ts=excluded.ts
                        """,
                        (pid, rfi_id, datetime.now(timezone.utc).isoformat()),
                    )
                ipc.append_journal(
                    info.root,
                    {"actor": "user", "kind": "paper_dropped", "paper_id": pid},
                )
                st.rerun()


# ── needs_manual.json 전용 뷰 (DB 에 아직 ingest 안 된 것 포함) ──

if needs_manual_items:
    st.divider()
    st.subheader("📜 needs_manual.json 원본 목록")
    with st.expander(f"{len(needs_manual_items)}개 항목 전체 보기", expanded=False):
        for it in needs_manual_items[:100]:
            st.markdown(f"**{it.get('title') or '(untitled)'}**")
            st.caption(f"id: `{it.get('id')}` · reason: {it.get('reason')}")
            urls = it.get("url_tried")
            if isinstance(urls, list):
                for u in urls[:3]:
                    st.code(u)
            elif urls:
                st.code(urls)
            st.markdown("---")
