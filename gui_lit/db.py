"""프로젝트별 sqlite DB — 논문 메타데이터 + FTS + summary/citation.

사용처:
- `lit_search` 이후 candidate 저장
- `lit_fetch` 이후 다운로드 상태 갱신
- `doc_to_md` 이후 extracted 경로 갱신
- `lit_summarize` 이후 summary 저장
- Head agent 가 `sqlite3 .litproj/state.sqlite "SELECT ..."` 로 조회

DB 는 `.gitignore` 이므로 언제든 재빌드 가능. 원본 데이터는 `originals/`
`extracted/` `agent-docs/summaries/*` 에 그대로 있음.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- meta
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 논문 메타데이터
CREATE TABLE IF NOT EXISTS papers (
    id              TEXT PRIMARY KEY,     -- 내부 slug (DOI 기반 or arxiv id)
    doi             TEXT,
    arxiv_id        TEXT,
    pmid            TEXT,
    pmc_id          TEXT,
    title           TEXT NOT NULL,
    authors         TEXT,                 -- JSON array
    year            INTEGER,
    venue           TEXT,
    abstract        TEXT,
    pdf_url         TEXT,
    source          TEXT,                 -- semantic_scholar | arxiv | openalex | manual
    citation_count  INTEGER,
    original_path   TEXT,                 -- originals/papers/<slug>.pdf
    extracted_path  TEXT,                 -- extracted/<slug>/doc.md
    status          TEXT DEFAULT 'discovered',  -- discovered|downloaded|extracted|summarized|done|failed
    notes           TEXT,
    added_ts        TEXT NOT NULL,
    updated_ts      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi) WHERE doi IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_arxiv ON papers(arxiv_id) WHERE arxiv_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(status);

-- 제목/초록 FTS
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
    id UNINDEXED,
    title,
    abstract,
    tokenize = 'unicode61'
);

-- 검색 실행 이력
CREATE TABLE IF NOT EXISTS search_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rfi_id      TEXT,
    query_json  TEXT,
    source      TEXT,
    hit_count   INTEGER,
    ts          TEXT NOT NULL,
    raw_path    TEXT                      -- originals/search/<ts>-<src>.json
);

-- 논문 ↔ RFI 연결 (triage 결과)
CREATE TABLE IF NOT EXISTS paper_rfi_links (
    paper_id   TEXT NOT NULL,
    rfi_id     TEXT NOT NULL,
    decision   TEXT NOT NULL,             -- keep|maybe|drop
    reason     TEXT,
    ts         TEXT NOT NULL,
    PRIMARY KEY (paper_id, rfi_id),
    FOREIGN KEY (paper_id) REFERENCES papers(id)
);

-- per-paper 요약
CREATE TABLE IF NOT EXISTS summaries (
    paper_id    TEXT NOT NULL,
    rfi_id      TEXT NOT NULL,
    content_md  TEXT NOT NULL,            -- structured markdown
    model       TEXT,
    ts          TEXT NOT NULL,
    PRIMARY KEY (paper_id, rfi_id),
    FOREIGN KEY (paper_id) REFERENCES papers(id)
);

-- 인용 관계 (snowball 용)
CREATE TABLE IF NOT EXISTS citations (
    src_id  TEXT NOT NULL,
    dst_id  TEXT NOT NULL,                -- 외부(미보유) 도 허용 → FK 없음
    context TEXT,
    PRIMARY KEY (src_id, dst_id)
);
"""


class DbError(Exception):
    pass


def db_path(project_root: Path) -> Path:
    return Path(project_root) / ".litproj" / "state.sqlite"


@contextmanager
def connect(project_root: Path) -> sqlite3.Connection:
    path = db_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_schema(project_root: Path) -> None:
    with connect(project_root) as conn:
        conn.executescript(SCHEMA_SQL)
        _ensure_schema_columns(conn)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )


def _ensure_schema_columns(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
    for name, decl in (("pmid", "TEXT"), ("pmc_id", "TEXT")):
        if name not in cols:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {name} {decl}")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_pmid "
        "ON papers(pmid) WHERE pmid IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_pmc_id "
        "ON papers(pmc_id) WHERE pmc_id IS NOT NULL"
    )


def upsert_paper(project_root: Path, row: dict) -> None:
    """papers + papers_fts 동시 업서트.

    필수 키: id, title, added_ts, updated_ts
    """
    required = {"id", "title", "added_ts", "updated_ts"}
    missing = required - row.keys()
    if missing:
        raise DbError(f"upsert_paper: 누락 키 {missing}")

    cols = [
        "id", "doi", "arxiv_id", "pmid", "pmc_id", "title", "authors", "year", "venue",
        "abstract", "pdf_url", "source", "citation_count",
        "original_path", "extracted_path", "status", "notes",
        "added_ts", "updated_ts",
    ]
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")

    values = [row.get(c) for c in cols]

    with connect(project_root) as conn:
        conn.execute(
            f"""
            INSERT INTO papers ({",".join(cols)}) VALUES ({placeholders})
            ON CONFLICT(id) DO UPDATE SET {updates}
            """,
            values,
        )
        # FTS 동기화
        conn.execute("DELETE FROM papers_fts WHERE id = ?", (row["id"],))
        conn.execute(
            "INSERT INTO papers_fts(id, title, abstract) VALUES (?, ?, ?)",
            (row["id"], row.get("title", ""), row.get("abstract", "") or ""),
        )


def upsert_papers(project_root: Path, rows: Iterable[dict]) -> int:
    n = 0
    for r in rows:
        upsert_paper(project_root, r)
        n += 1
    return n


def search_papers(
    project_root: Path, query: str, limit: int = 20,
) -> list[dict]:
    with connect(project_root) as conn:
        cur = conn.execute(
            """
            SELECT p.*
              FROM papers_fts
              JOIN papers p ON p.id = papers_fts.id
             WHERE papers_fts MATCH ?
             ORDER BY rank
             LIMIT ?
            """,
            (query, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def list_papers_by_status(
    project_root: Path, status: str, limit: int = 200,
) -> list[dict]:
    with connect(project_root) as conn:
        cur = conn.execute(
            "SELECT * FROM papers WHERE status = ? ORDER BY year DESC LIMIT ?",
            (status, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def record_search_run(
    project_root: Path,
    rfi_id: str | None,
    query_json: str,
    source: str,
    hit_count: int,
    raw_path: str | None,
    ts: str,
) -> int:
    with connect(project_root) as conn:
        cur = conn.execute(
            """
            INSERT INTO search_runs(rfi_id, query_json, source, hit_count, ts, raw_path)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (rfi_id, query_json, source, hit_count, ts, raw_path),
        )
        return cur.lastrowid


# ── 파일시스템 스캔 기반 ingest ──


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _normalize_candidate(c: dict) -> dict:
    """lit_search candidate dict → papers row dict."""
    now = _iso_now()
    return {
        "id": c.get("id") or "unknown",
        "doi": c.get("doi"),
        "arxiv_id": c.get("arxiv_id"),
        "pmid": c.get("pmid"),
        "pmc_id": c.get("pmc_id") or c.get("pmcid"),
        "title": (c.get("title") or "").strip() or "(untitled)",
        "authors": (
            __import__("json").dumps(c.get("authors") or [], ensure_ascii=False)
            if isinstance(c.get("authors"), list)
            else (c.get("authors") or "")
        ),
        "year": c.get("year") if isinstance(c.get("year"), int) else None,
        "venue": c.get("venue"),
        "abstract": c.get("abstract"),
        "pdf_url": c.get("pdf_url"),
        "source": c.get("source") or "unknown",
        "citation_count": (
            c.get("citation_count")
            if isinstance(c.get("citation_count"), int) else None
        ),
        "original_path": None,
        "extracted_path": None,
        "status": "discovered",
        "notes": None,
        "added_ts": now,
        "updated_ts": now,
    }


def _slug_from_title(title: str, year: int | None) -> str:
    import re
    text = re.sub(r"[^\w\s-]", "", (title or "").lower())
    parts = text.split()[:6]
    stem = "-".join(parts) or "untitled"
    if year:
        stem = f"{year}-{stem}"
    return stem[:60].rstrip("-")


def _strip_doc_suffix(stem: str) -> str:
    return stem[:-4] if stem.endswith("-doc") else stem


def _aliases_for_candidate(c: dict) -> set[str]:
    aliases: set[str] = set()
    cid = c.get("id")
    if cid:
        aliases.add(str(cid))
    title = c.get("title") or ""
    year = c.get("year") if isinstance(c.get("year"), int) else None
    if title:
        aliases.add(_slug_from_title(title, year))
    for key in ("local_path", "original_path"):
        val = c.get(key)
        if val:
            aliases.add(Path(str(val)).stem)
            aliases.add(_strip_doc_suffix(Path(str(val)).stem))
    return {a for a in aliases if a}


def _candidate_list(data) -> list[dict]:
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = (
            data.get("entries")
            or data.get("items")
            or data.get("candidates")
            or []
        )
    else:
        raw = []
    return [c for c in raw if isinstance(c, dict)]


def _triage_entries(data) -> list[dict]:
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = (
            data.get("entries")
            or data.get("items")
            or data.get("candidates")
            or []
        )
    else:
        raw = []
    return [c for c in raw if isinstance(c, dict)]


def _rfi_id_from_path(path: Path, data: dict | None = None) -> str:
    if data and data.get("rfi"):
        return str(data["rfi"]).zfill(4) if str(data["rfi"]).isdigit() else str(data["rfi"])
    import re
    s = str(path).replace("\\", "/")
    for pat in (r"rfi[-_](\d{3,})", r"/(\d{3,})-[^/]+/"):
        m = re.search(pat, s)
        if m:
            return m.group(1)
    return "unknown"


def _delete_cache_rows(project_root: Path) -> None:
    init_schema(project_root)
    with connect(project_root) as conn:
        conn.execute("BEGIN")
        try:
            for table in (
                "citations",
                "summaries",
                "paper_rfi_links",
                "search_runs",
                "papers_fts",
                "papers",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def ingest_from_filesystem(
    project_root: Path,
    *,
    verbose: bool = False,
    rebuild: bool = True,
) -> dict[str, int]:
    """프로젝트 파일시스템 스캔 → state.sqlite 재생성/업서트.

    스캔 대상:
      - **/candidates.json         lit_search 산출 → papers 기본 메타
      - **/triaged.json            triage 결정 → paper_rfi_links
      - agent-docs/summaries/**/*.json  lit_summarize → summaries + status 승격
      - originals/papers/*.pdf      다운로드 여부 → status / original_path
      - extracted/<slug>-doc.md 또는 extracted/<slug>/doc.md → OCR 완료

    기본값 rebuild=True. state.sqlite 는 gitignore 된 캐시이므로 stale row 를
    남기지 않기 위해 캐시 테이블을 비운 뒤 파일시스템 기준으로 재생성한다.
    반환: {'papers': N, 'triaged': M, 'summaries': K, 'originals': P, 'extracted': Q}
    """
    project_root = Path(project_root).resolve()
    init_schema(project_root)
    if rebuild:
        _delete_cache_rows(project_root)

    stats = {
        "papers": 0,
        "triaged": 0,
        "summaries": 0,
        "originals": 0,
        "extracted": 0,
    }
    aliases: dict[str, str] = {}

    def _remember_aliases(pid: str, new_aliases: set[str]) -> None:
        for alias in new_aliases:
            aliases.setdefault(alias, pid)

    def _find_paper_id(conn: sqlite3.Connection, *candidates: str | None) -> str | None:
        for raw in candidates:
            if not raw:
                continue
            stem = _strip_doc_suffix(str(raw))
            for key in (str(raw), stem):
                if key in aliases:
                    return aliases[key]
                row = conn.execute("SELECT id FROM papers WHERE id = ?", (key,)).fetchone()
                if row:
                    return row["id"]
        return None

    # 1) candidates.json → papers upsert
    for cand_path in project_root.rglob("candidates.json"):
        # .git 안은 skip
        if ".git" in cand_path.parts:
            continue
        try:
            data = json.loads(cand_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        candidates = _candidate_list(data)
        for c in candidates:
            try:
                row = _normalize_candidate(c)
                upsert_paper(project_root, row)
                _remember_aliases(row["id"], _aliases_for_candidate(c))
                stats["papers"] += 1
            except (DbError, sqlite3.Error) as exc:
                if verbose:
                    print(f"  skip {c.get('id')}: {exc}")

    # 2) triaged.json → paper_rfi_links
    for tr_path in project_root.rglob("triaged.json"):
        if ".git" in tr_path.parts:
            continue
        try:
            data = json.loads(tr_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rfi_id = _rfi_id_from_path(tr_path, data if isinstance(data, dict) else None)
        items = _triage_entries(data)
        now = _iso_now()
        with connect(project_root) as conn:
            for it in items:
                pid = it.get("id")
                dec = it.get("decision")
                if not pid or not dec:
                    continue
                db_pid = _find_paper_id(conn, str(pid)) or str(pid)
                try:
                    conn.execute(
                        """
                        INSERT INTO paper_rfi_links(paper_id, rfi_id, decision, reason, ts)
                        VALUES(?, ?, ?, ?, ?)
                        ON CONFLICT(paper_id, rfi_id) DO UPDATE SET
                            decision=excluded.decision,
                            reason=excluded.reason,
                            ts=excluded.ts
                        """,
                        (db_pid, rfi_id, dec, it.get("reason"), now),
                    )
                    stats["triaged"] += 1
                except sqlite3.Error:
                    pass

    # 3) originals/papers/*.pdf → status = downloaded, original_path 기록
    originals_dir = project_root / "originals" / "papers"
    if originals_dir.is_dir():
        with connect(project_root) as conn:
            for pdf in originals_dir.glob("*.pdf"):
                rel = str(pdf.relative_to(project_root)).replace("\\", "/")
                slug = pdf.stem
                pid = _find_paper_id(conn, slug)
                if pid is None:
                    try:
                        cur = conn.execute("SELECT id, title, year FROM papers")
                        for r in cur.fetchall():
                            if _slug_from_title(r["title"], r["year"]) == slug:
                                pid = r["id"]
                                break
                    except sqlite3.Error:
                        pass
                if pid:
                    try:
                        conn.execute(
                            """
                            UPDATE papers
                               SET original_path = ?,
                                   status = CASE
                                       WHEN status IN ('discovered', 'downloaded') THEN 'downloaded'
                                       ELSE status
                                   END,
                                   updated_ts = ?
                             WHERE id = ?
                            """,
                            (rel, _iso_now(), pid),
                        )
                        _remember_aliases(pid, {slug})
                        stats["originals"] += 1
                    except sqlite3.Error:
                        pass

    # 4) extracted/<slug>-doc.md 및 extracted/<slug>/doc.md → status 승격
    extracted_dir = project_root / "extracted"
    if extracted_dir.is_dir():
        with connect(project_root) as conn:
            docs: list[tuple[Path, str]] = []
            for md in extracted_dir.glob("*-doc.md"):
                docs.append((md, _strip_doc_suffix(md.stem)))
            for sub in extracted_dir.iterdir():
                if sub.is_dir() and (sub / "doc.md").exists():
                    docs.append((sub / "doc.md", sub.name))
            for doc, slug in docs:
                rel = str(doc.relative_to(project_root)).replace("\\", "/")
                pid = _find_paper_id(conn, slug, doc.stem)
                if pid is None:
                    try:
                        cur = conn.execute("SELECT id, title, year FROM papers")
                        for r in cur.fetchall():
                            if _slug_from_title(r["title"], r["year"]) == slug:
                                pid = r["id"]
                                break
                    except sqlite3.Error:
                        pass
                if pid:
                    try:
                        conn.execute(
                            """
                            UPDATE papers
                               SET extracted_path = ?,
                                   status = CASE
                                       WHEN status IN ('discovered', 'downloaded', 'extracted')
                                           THEN 'extracted'
                                       ELSE status
                                   END,
                                   updated_ts = ?
                             WHERE id = ?
                            """,
                            (rel, _iso_now(), pid),
                        )
                        _remember_aliases(pid, {slug, doc.stem})
                        stats["extracted"] += 1
                    except sqlite3.Error:
                        pass

    # 5) agent-docs/summaries/**/*.json → summaries 테이블 + status=summarized
    sum_dir = project_root / "agent-docs" / "summaries"
    if sum_dir.is_dir():
        with connect(project_root) as conn:
            for sj in sum_dir.rglob("*.json"):
                try:
                    s = json.loads(sj.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(s, dict):
                    continue
                paper_id = s.get("paper_id") or sj.stem
                pid = _find_paper_id(conn, str(paper_id), sj.stem)
                if pid is None:
                    try:
                        cur = conn.execute("SELECT id, title, year FROM papers")
                        for r in cur.fetchall():
                            if _slug_from_title(r["title"], r["year"]) in {
                                sj.stem,
                                _strip_doc_suffix(sj.stem),
                            }:
                                pid = r["id"]
                                break
                    except sqlite3.Error:
                        pass
                if not pid:
                    continue
                content_md = s.get("summary_md") or json.dumps(s, ensure_ascii=False)[:5000]
                rfi_id = str(s.get("rfi_id") or _rfi_id_from_path(sj))
                try:
                    conn.execute(
                        """
                        INSERT INTO summaries(paper_id, rfi_id, content_md, model, ts)
                        VALUES(?, ?, ?, ?, ?)
                        ON CONFLICT(paper_id, rfi_id) DO UPDATE SET
                            content_md=excluded.content_md,
                            model=excluded.model,
                            ts=excluded.ts
                        """,
                        (pid, rfi_id, content_md, s.get("model"), _iso_now()),
                    )
                    conn.execute(
                        """
                        UPDATE papers SET status = 'summarized',
                                          updated_ts = ?
                         WHERE id = ?
                           AND status NOT IN ('done')
                        """,
                        (_iso_now(), pid),
                    )
                    _remember_aliases(pid, {str(paper_id), sj.stem, _strip_doc_suffix(sj.stem)})
                    stats["summaries"] += 1
                except sqlite3.Error:
                    pass

    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="gui_lit SQLite cache utilities")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ingest = sub.add_parser("ingest", help="파일시스템에서 state.sqlite 재생성")
    ingest.add_argument("project_root", type=Path)
    ingest.add_argument("--rebuild", action="store_true", default=True)
    ingest.add_argument("--no-rebuild", dest="rebuild", action="store_false")
    ingest.add_argument("--verbose", action="store_true")

    args = ap.parse_args(argv)
    if args.cmd == "ingest":
        stats = ingest_from_filesystem(
            args.project_root,
            rebuild=args.rebuild,
            verbose=args.verbose,
        )
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
