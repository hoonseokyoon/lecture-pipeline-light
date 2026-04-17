"""스킬별 Python venv 를 하네스가 관리.

각 스킬이 `scripts/requirements.txt` 를 두면, 이 모듈이:
1. 처음 호출 시 venv 생성 + 패키지 설치
2. requirements.txt 가 바뀌거나 하네스 schema 가 증가하면 재빌드
3. 그 외엔 캐시 hit — bin/Scripts 경로만 반환

기존 WSL + bash `scripts/bootstrap.sh` 조합을 대체. 크로스플랫폼 (`venv` +
`pip` 표준 라이브러리만 사용). uv 지원 없음 — pip 로만 설치.

캐시 위치: `~/.lecture-pipeline/venvs/<skill_name>/`

Stamp 스키마 (`<venv>/.stamp.json`):
    {
        "schema": 1,                # VenvManager 로직 변경 시 증가
        "req_hash": "sha256:...",   # requirements.txt SHA256
        "python": "3.11.9 (...)",   # sys.version
        "platform": "win32"         # sys.platform
    }

이 중 하나라도 불일치하면 재빌드.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import sys
import threading
import venv as venv_module
from pathlib import Path

logger = logging.getLogger("lecture_pipeline.venv_manager")


class VenvBuildError(Exception):
    """venv 생성/복원 실패."""


class VenvManager:
    SCHEMA_VERSION = 1
    CACHE_ROOT = Path.home() / ".lecture-pipeline" / "venvs"

    def __init__(self, cache_root: Path | None = None):
        self.cache_root = cache_root or self.CACHE_ROOT
        self._locks: dict[str, threading.Lock] = {}
        self._locks_table_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    def ensure(self, skill_dir: Path) -> Path | None:
        """skill 의 venv 가 최신 상태임을 보장하고 bin/Scripts 경로 반환.

        requirements.txt 가 없으면 None 반환 (venv 불필요).
        """
        req_file = self._find_requirements(skill_dir)
        if req_file is None:
            return None

        key = skill_dir.name
        lock = self._get_lock(key)
        with lock:
            venv_path = self.cache_root / key
            if self._is_fresh(venv_path, req_file):
                logger.debug("[%s] venv 캐시 hit: %s", key, venv_path)
                return self._bin_dir(venv_path)
            self._rebuild(venv_path, req_file, key)
            return self._bin_dir(venv_path)

    # ------------------------------------------------------------------
    # 내부
    # ------------------------------------------------------------------

    def _get_lock(self, key: str) -> threading.Lock:
        with self._locks_table_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    @staticmethod
    def _find_requirements(skill_dir: Path) -> Path | None:
        """우선순위: `scripts/requirements.txt` > `requirements.txt`."""
        for candidate in (
            skill_dir / "scripts" / "requirements.txt",
            skill_dir / "requirements.txt",
        ):
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _bin_dir(venv_path: Path) -> Path:
        """플랫폼별 실행파일 디렉토리."""
        return venv_path / ("Scripts" if sys.platform == "win32" else "bin")

    @staticmethod
    def _python_in_venv(venv_path: Path) -> Path:
        bin_dir = VenvManager._bin_dir(venv_path)
        return bin_dir / ("python.exe" if sys.platform == "win32" else "python")

    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.sha256()
        h.update(path.read_bytes())
        return h.hexdigest()

    @classmethod
    def _current_stamp(cls, req_file: Path) -> dict:
        return {
            "schema": cls.SCHEMA_VERSION,
            "req_hash": cls._hash_file(req_file),
            "python": sys.version,
            "platform": sys.platform,
        }

    def _is_fresh(self, venv_path: Path, req_file: Path) -> bool:
        stamp_path = venv_path / ".stamp.json"
        if not stamp_path.exists():
            return False
        try:
            data = json.loads(stamp_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        expected = self._current_stamp(req_file)
        return all(data.get(k) == v for k, v in expected.items())

    def _rebuild(self, venv_path: Path, req_file: Path, key: str) -> None:
        """반-빌드 상태가 남지 않도록 tmp 에 빌드 → atomic rename."""
        logger.info("[%s] venv 빌드 중 (최초 또는 requirements 변경)...", key)
        self.cache_root.mkdir(parents=True, exist_ok=True)

        # 기존 venv 가 있었다면 완전히 제거 후 tmp 에 새로 빌드
        tmp_path = venv_path.with_name(venv_path.name + ".tmp")
        if tmp_path.exists():
            shutil.rmtree(tmp_path, ignore_errors=True)

        try:
            venv_module.EnvBuilder(
                with_pip=True,
                clear=True,
            ).create(tmp_path)
        except Exception as exc:
            raise VenvBuildError(f"[{key}] venv 생성 실패: {exc}") from exc

        py = self._python_in_venv(tmp_path)
        if not py.exists():
            shutil.rmtree(tmp_path, ignore_errors=True)
            raise VenvBuildError(
                f"[{key}] venv Python 없음 (예상 경로: {py})"
            )

        try:
            subprocess.run(
                [
                    str(py), "-m", "pip", "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "-r", str(req_file),
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(tmp_path, ignore_errors=True)
            tail = ((exc.stderr or "") + (exc.stdout or ""))[-800:]
            raise VenvBuildError(
                f"[{key}] pip install 실패 (exit={exc.returncode}):\n{tail}"
            ) from exc

        # stamp 기록 후 atomic rename (venv_path 이미 있으면 제거)
        stamp = self._current_stamp(req_file)
        (tmp_path / ".stamp.json").write_text(
            json.dumps(stamp, ensure_ascii=False), encoding="utf-8",
        )
        if venv_path.exists():
            shutil.rmtree(venv_path, ignore_errors=True)
        tmp_path.rename(venv_path)
        logger.info("[%s] venv 준비 완료: %s", key, venv_path)
