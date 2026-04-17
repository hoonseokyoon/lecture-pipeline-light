"""스킬 실행 결과를 워크스페이스 `out_root` 아래 저장하는 OutputWriter.

- `rename_map` 템플릿 + `{stem}` 치환으로 파일명 변환
- 여러 출력 파일은 **공통 `-N` suffix** 로 충돌 회피 (lecture_note의
  note.md/json/html 그룹이 같은 N 을 공유)
- `_reserve_group` 이 `O_CREAT|O_EXCL` 로 원자적 그룹 예약을 수행해 병렬 task
  가 같은 stem 을 써도 race-free. 파일 쓰기 전용 락 불필요.
- 선택적으로 `msg_queue` 를 받아 각 쓰기마다 GUI 에 `("log", ...)` 이벤트 전파.
"""

import os
import queue
from pathlib import Path


def _with_increment(path: Path, n: int) -> Path:
    """n==0이면 원본 path, n>0이면 `<stem>-<n><suffix>` 형태."""
    if n <= 0:
        return path
    return path.with_name(f"{path.stem}-{n}{path.suffix}")


def _reserve_group(targets: list[Path]) -> tuple[int, list[Path]]:
    """그룹 타깃을 공통 suffix로 원자적으로 예약.

    모든 경로를 `O_CREAT|O_EXCL`로 한 번에 잡고, 어느 하나라도 충돌하면
    이미 생성한 빈 파일들을 rollback 후 suffix N 을 증가시켜 재시도.
    반환되는 파일은 0바이트 상태로 존재하며, 호출자는 `write_bytes`로
    덮어쓰면 된다.

    락 없이 race-free. 공통 `-N` suffix 동작을 보존한다.
    """
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    n = 0
    while True:
        paths = [_with_increment(t, n) for t in targets]
        opened: list[tuple[int, Path]] = []
        try:
            for p in paths:
                fd = os.open(str(p), flags)
                opened.append((fd, p))
        except FileExistsError:
            for fd, _ in opened:
                try:
                    os.close(fd)
                except OSError:
                    pass
            for _, p in opened:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
            n += 1
            continue
        for fd, _ in opened:
            os.close(fd)
        return n, paths


class OutputWriter:
    """스킬 출력 파일을 `out_root` 아래 저장."""

    def __init__(
        self,
        msg_queue: "queue.Queue[tuple[str, object]] | None" = None,
    ):
        self._q = msg_queue

    def write(
        self,
        *,
        outputs: dict[str, bytes],
        out_root: Path,
        rename_map: dict[str, str],
        stem: str,
        skill_name: str,
        rel: Path | None,
    ) -> list[Path]:
        """outputs 딕셔너리를 저장하고 최종 경로 리스트 반환.

        Args:
            outputs: {original_name: bytes}. 스킬이 반환한 파일들.
            out_root: 저장 디렉토리 (없으면 자동 생성).
            rename_map: {original_name: template}. 템플릿 내 `{stem}` 치환됨.
                매핑에 없는 original_name 은 그대로 사용.
            stem: `{stem}` 치환 값. per-file 모드면 입력 파일 stem, batch 모드
                면 대표 입력(보통 첫 PDF)의 stem.
            skill_name: 로그 메시지에 쓰이는 skill 이름.
            rel: 입력 파일 상대경로. 로그 prefix. None 이면 생략.

        Returns:
            실제 기록된 파일 경로 리스트 (suffix 적용 후).
        """
        if not outputs:
            return []

        ordered = list(outputs.items())
        base_targets: list[Path] = []
        for name, _ in ordered:
            template = rename_map.get(name, name)
            try:
                renamed = template.format(stem=stem)
            except (KeyError, IndexError, ValueError):
                renamed = template
            base_targets.append(out_root / renamed)

        _, reserved = _reserve_group(base_targets)

        prefix = f"{rel} → " if rel else ""
        for (_, content), final in zip(ordered, reserved):
            final.write_bytes(content)
            if self._q is not None:
                self._q.put(
                    ("log", f"[{skill_name}] OK {prefix}{final.name}")
                )
        return reserved
