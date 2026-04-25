from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_runner import CodexRunError
from gui_lit import db, doctor, followup, ipc


class GuiLitStabilizationTests(unittest.TestCase):
    def test_append_journal_cli_newline_and_ts_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.mkdir(exist_ok=True)
            with redirect_stdout(StringIO()):
                rc = ipc.main(
                    [
                        "append-journal",
                        str(root),
                        "--event-json",
                        '{"actor":"head","kind":"test","ts":"2000-01-01T00:00:00Z"}',
                    ]
                )
            self.assertEqual(rc, 0)
            raw = ipc.journal_path(root).read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            row = json.loads(raw.decode("utf-8").splitlines()[0])
            self.assertEqual(row["kind"], "test")
            self.assertNotEqual(row["ts"], "2000-01-01T00:00:00Z")

    def test_doctor_detects_and_repairs_concatenated_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".litproj").mkdir()
            one = {"ts": datetime.now(timezone.utc).isoformat(), "kind": "a"}
            two = {"ts": datetime.now(timezone.utc).isoformat(), "kind": "b"}
            path = root / ".litproj" / "journal.jsonl"
            path.write_text(
                json.dumps(one) + json.dumps(two) + "\n",
                encoding="utf-8",
            )
            findings = doctor.run_checks(root)
            self.assertTrue(any(f.code == "journal_concatenated" for f in findings))

            doctor.repair_journal(root, backup=True)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            findings = doctor.run_checks(root)
            self.assertFalse(any(f.code == "journal_concatenated" for f in findings))
            self.assertTrue(list((root / ".litproj").glob("journal.jsonl.*.bak")))

    def test_db_rebuild_entries_nested_summaries_and_flat_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".litproj" / "runs" / "rfi-0001").mkdir(parents=True)
            (root / "agent-docs" / "summaries" / "rfi-0001").mkdir(parents=True)
            (root / "extracted").mkdir()

            cand_path = root / ".litproj" / "runs" / "rfi-0001" / "candidates.json"
            cand_path.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "id": "p1",
                                "title": "Promoter biology benchmark",
                                "year": 2024,
                                "abstract": "prokaryotic promoter",
                                "local_path": "originals/papers/p1.pdf",
                            },
                            {"id": "stale", "title": "Old physics arxiv", "year": 2020},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            db.ingest_from_filesystem(root, rebuild=True)
            with db.connect(root) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 2)

            cand_path.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "id": "p1",
                                "title": "Promoter biology benchmark",
                                "year": 2024,
                                "abstract": "prokaryotic promoter",
                                "local_path": "originals/papers/p1.pdf",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (root / ".litproj" / "runs" / "rfi-0001" / "triaged.json").write_text(
                json.dumps({"entries": [{"id": "p1", "decision": "keep", "reason": "core"}]}),
                encoding="utf-8",
            )
            (root / "agent-docs" / "summaries" / "rfi-0001" / "p1.json").write_text(
                json.dumps({"paper_id": "p1", "summary_md": "summary"}),
                encoding="utf-8",
            )
            (root / "extracted" / "p1-doc.md").write_text("# full text", encoding="utf-8")

            stats = db.ingest_from_filesystem(root, rebuild=True)
            self.assertEqual(stats["papers"], 1)
            self.assertEqual(stats["triaged"], 1)
            self.assertEqual(stats["summaries"], 1)
            self.assertEqual(stats["extracted"], 1)
            with db.connect(root) as conn:
                rows = conn.execute("SELECT id, status, extracted_path FROM papers").fetchall()
                self.assertEqual([r["id"] for r in rows], ["p1"])
                self.assertEqual(rows[0]["status"], "summarized")
                self.assertEqual(rows[0]["extracted_path"], "extracted/p1-doc.md")
                link_count = conn.execute("SELECT COUNT(*) FROM paper_rfi_links").fetchone()[0]
                self.assertEqual(link_count, 1)

    def test_search_profile_and_sanity_gate(self) -> None:
        from skills.lit_search._lib import search

        self.assertEqual(
            search.sources_for_domain_profile("biomed"),
            ["pubmed", "semantic_scholar", "europepmc"],
        )
        self.assertNotIn("arxiv", search.sources_for_domain_profile("biomed"))
        bad = [
            {"title": "LIGO gravitational wave detector", "abstract": "black hole merger"}
            for _ in range(20)
        ]
        with self.assertRaises(CodexRunError):
            search._validate_sanity_gate(bad, "prokaryotic promoter biology")

    def test_review_lint_dangling_placeholder_and_abstract_high_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reviews = root / "agent-docs" / "reviews"
            reviews.mkdir(parents=True)
            (reviews / "rfi-0001-review.md").write_text(
                "# Review\n\n핵심 주장 [42]\n\nTODO citation placeholder\n\n## References\n1. A paper\n",
                encoding="utf-8",
            )
            (reviews / "rfi-0001-claim-matrix.json").write_text(
                json.dumps(
                    {
                        "claims": [
                            {
                                "claim": "strong claim",
                                "refs": ["p1"],
                                "fulltext_refs": [],
                                "abstract_only_refs": ["p1"],
                                "confidence": "high",
                                "section": "Synthesis",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            codes = {f.code for f in doctor.check_review_lint(root)}
            self.assertIn("review_dangling_citation", codes)
            self.assertIn("review_placeholder_ref", codes)
            self.assertIn("claim_abstract_only_high_confidence", codes)

    def test_fetch_europepmc_urls_and_doi_resolver(self) -> None:
        from skills.lit_fetch._lib import browser_fetch

        self.assertEqual(
            browser_fetch.europepmc_pdf_url("12345"),
            "https://europepmc.org/articles/PMC12345?pdf=render",
        )
        self.assertIn("ptpmcrender.fcgi", browser_fetch.europepmc_fcgi_pdf_url("PMC12345"))

        sys.path.insert(0, str(ROOT / "skills" / "lit_fetch"))
        try:
            from _lib import fetch
        finally:
            try:
                sys.path.remove(str(ROOT / "skills" / "lit_fetch"))
            except ValueError:
                pass

        class Resp:
            status_code = 200

            def json(self):
                return {"resultList": {"result": [{"pmcid": "99999"}]}}

        old_get = fetch.requests.get
        fetch._DOI_PMC_CACHE.clear()
        fetch.requests.get = lambda *args, **kwargs: Resp()
        try:
            self.assertEqual(fetch._resolve_pmc_id_from_doi("10.1000/test", 1), "PMC99999")
            urls = fetch._resolve_pdf_candidates({"doi": "10.1000/test"}, timeout=1)
        finally:
            fetch.requests.get = old_get
        self.assertEqual(urls[0], ("europepmc", "https://europepmc.org/articles/PMC99999?pdf=render"))

    def test_second_opinion_normalizer_maps_bundle(self) -> None:
        from skills.second_opinion import skill

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            for rel in (
                "agent-docs/reviews/rfi-0001-review.md",
                "REQUEST_FOR_INFORMATION.md",
                "agent-docs/reviews/rfi-0001-claim-matrix.json",
                ".litproj/runs/001/candidates.json",
                ".litproj/runs/001/triaged.json",
            ):
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("{}", encoding="utf-8")
                paths.append(p)
            mapped = skill.normalize(paths)
            self.assertIn("review.md", mapped)
            self.assertIn("rfi.md", mapped)
            self.assertIn("claim-matrix.json", mapped)
            self.assertIn("candidates.json", mapped)
            self.assertIn("triaged.json", mapped)

    def test_rfi_followup_open_close_and_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent-docs" / "rfi").mkdir(parents=True)
            (root / "agent-docs" / "reviews").mkdir(parents=True)
            (root / ".litproj").mkdir()
            (root / "agent-docs" / "rfi" / "0001-promoter-biology.md").write_text(
                """---
id: "0001"
slug: "promoter-biology"
status: "done"
---

# RFI-0001
""",
                encoding="utf-8",
            )

            paths = followup.open_followup(
                root,
                rfi_id="0001",
                topic="cross host evidence",
                question="B. subtilis portability 근거 보강",
                domain_profile="biomed",
                priority="high",
                must_address=["non-E. coli full-text evidence"],
            )
            self.assertTrue(paths.followup_file.exists())
            self.assertTrue(paths.query_plan.exists())
            q = json.loads(paths.query_plan.read_text(encoding="utf-8"))
            self.assertEqual(q["followup_id"], "fu-001")
            self.assertEqual(q["domain_profile"], "biomed")
            self.assertFalse(doctor.check_followups(root))

            paths.addendum.write_text("# Addendum\n\n## References\n", encoding="utf-8")
            paths.claim_matrix.write_text(json.dumps({"claims": []}), encoding="utf-8")
            followup.close_followup(
                root,
                rfi_id="0001",
                followup_id="fu-001",
                outcome="보강 완료",
            )
            self.assertFalse(doctor.check_followups(root))
            items = followup.list_followups(root, rfi_id="0001")
            self.assertEqual(items[0]["status"], "done")

    def test_rfi_followup_done_requires_addendum(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent-docs" / "rfi").mkdir(parents=True)
            (root / "agent-docs" / "rfi" / "0001-promoter-biology").mkdir(parents=True)
            (root / "agent-docs" / "rfi" / "0001-promoter-biology.md").write_text(
                """---
id: "0001"
slug: "promoter-biology"
status: "done"
---
# RFI
""",
                encoding="utf-8",
            )
            paths = followup.open_followup(root, rfi_id="0001", topic="gap")
            followup.close_followup(root, rfi_id="0001", followup_id="fu-001")
            codes = {f.code for f in doctor.check_followups(root)}
            self.assertIn("followup_addendum_missing", codes)


if __name__ == "__main__":
    unittest.main()
