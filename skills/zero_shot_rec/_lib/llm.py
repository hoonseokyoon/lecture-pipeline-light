"""Unified LLM dispatcher — config['backend']로 Codex vs Gemini 라우팅.

모든 stage가 `call_llm`만 호출하면 되도록 추상화. 두 백엔드는 동일한 반환 형태
({"result.json": bytes}).
"""

from __future__ import annotations

from codex_runner import run_codex_task

from _lib.gemini_backend import run_gemini_task, set_rpm


def call_llm(
    *,
    cfg: dict,
    prompt: str,
    inputs: dict,
    output_schema: dict,
    reasoning_effort: str,
) -> dict[str, bytes]:
    """백엔드 추상화 호출.

    cfg['backend']:
        - 'codex' (default): Codex CLI + Claude fallback (기존 인프라 재사용)
        - 'gemini': google.genai in-process (WSL/subprocess 불필요)

    Gemini 경로는 reasoning_effort를 무시 (Gemini 2.5 thinking은 자동).
    """
    backend = str(cfg.get("backend", "codex")).lower()
    timeout = int(cfg.get("timeout", 600))

    if backend == "gemini":
        set_rpm(int(cfg.get("gemini_rpm", 0)))
        return run_gemini_task(
            prompt=prompt,
            inputs=inputs,
            output_schema=output_schema,
            model=cfg.get("gemini_model", "gemini-2.5-pro"),
            timeout=timeout,
        )

    # Codex (기본)
    return run_codex_task(
        prompt=prompt,
        inputs=inputs,
        expected_outputs=["result.json"],
        output_schema=output_schema,
        model=cfg["model"],
        reasoning_effort=reasoning_effort,
        timeout=timeout,
    )
