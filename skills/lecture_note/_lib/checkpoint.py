"""Composite skill용 단계별 체크포인트 저장/재개 헬퍼.

경로: ~/.cache/lecture-pipeline/composite/<skill_name>/<run_id>/

각 step이 완료되면 해당 파일을 저장. 재실행 시 존재하면 로드(compute 생략).

발견성 보강:
- 디렉토리 첫 생성 시 `_started_at.txt` (ISO 타임스탬프) 작성. 이후 재실행
  에서는 갱신 안 함 — "이 캐시가 처음 만들어진 시각" 으로 의미 유지.
- 부모 디렉토리 (`<skill_name>/_index.jsonl`) 에 매 실행마다 한 줄 append:
  `{"started_at", "run_id", "inputs"}`. `tail _index.jsonl` 로 최근 실행 →
  run_id 매핑 즉시 확인 가능.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


def _iso_now() -> str:
    """로컬 타임존 ISO 8601 (초 단위)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


class CheckpointDir:
    def __init__(self, skill_name: str, run_id: str):
        self.skill_name = skill_name
        self.run_id = run_id
        self.path = (
            Path.home() / ".cache" / "lecture-pipeline" / "composite"
            / skill_name / run_id
        )
        # 디렉토리 첫 생성 여부 판정 — _started_at 을 한 번만 쓰기 위함.
        is_new = not self.path.exists()
        self.path.mkdir(parents=True, exist_ok=True)
        if is_new:
            try:
                (self.path / "_started_at.txt").write_text(
                    _iso_now() + "\n", encoding="utf-8",
                )
            except OSError:
                pass  # 발견성 보조 파일 — 실패해도 본류 영향 없음

    def subdir(self, name: str) -> Path:
        d = self.path / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def file(self, name: str) -> Path:
        return self.path / name

    def write_inputs_manifest(self, input_paths: list[Path]) -> None:
        manifest = {
            "inputs": [
                {"name": Path(p).name, "path": str(Path(p).resolve())}
                for p in input_paths
            ]
        }
        (self.path / "inputs.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 부모 디렉토리 인덱스에 한 줄 append. 매 실행마다 새 entry —
        # 캐시 hit 으로 step 들이 전부 재사용되더라도 "사용자가 이 시점에
        # 다시 돌렸다" 는 사실은 기록에 남음.
        index_path = self.path.parent / "_index.jsonl"
        entry = {
            "started_at": _iso_now(),
            "run_id": self.run_id,
            "inputs": [item["name"] for item in manifest["inputs"]],
        }
        try:
            with index_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 인덱스 실패해도 본류 영향 없음

    # ------------------------------------------------------------------
    # Get-or-compute helpers
    # ------------------------------------------------------------------

    def get_or_compute_json(
        self,
        name: str,
        compute: Callable[[], Any],
    ) -> Any:
        target = self.path / name
        if target.exists():
            return json.loads(target.read_text(encoding="utf-8"))
        result = compute()
        target.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return result

    def get_or_compute_bytes(
        self,
        name: str,
        compute: Callable[[], bytes],
    ) -> bytes:
        target = self.path / name
        if target.exists():
            return target.read_bytes()
        result = compute()
        if not isinstance(result, bytes):
            raise TypeError(f"{name}의 compute 결과는 bytes여야 함")
        target.write_bytes(result)
        return result

    def get_or_compute_text(
        self,
        name: str,
        compute: Callable[[], str],
    ) -> str:
        target = self.path / name
        if target.exists():
            return target.read_text(encoding="utf-8")
        result = compute()
        target.write_text(result, encoding="utf-8")
        return result

    def has(self, name: str) -> bool:
        return (self.path / name).exists()
