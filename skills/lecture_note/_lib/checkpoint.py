"""Composite skill용 단계별 체크포인트 저장/재개 헬퍼.

경로: ~/.cache/lecture-pipeline/composite/<skill_name>/<run_id>/

각 step이 완료되면 해당 파일을 저장. 재실행 시 존재하면 로드(compute 생략).
"""

import json
from pathlib import Path
from typing import Any, Callable


class CheckpointDir:
    def __init__(self, skill_name: str, run_id: str):
        self.skill_name = skill_name
        self.run_id = run_id
        self.path = (
            Path.home() / ".cache" / "lecture-pipeline" / "composite"
            / skill_name / run_id
        )
        self.path.mkdir(parents=True, exist_ok=True)

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
