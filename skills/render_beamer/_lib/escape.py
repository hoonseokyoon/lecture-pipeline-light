"""TeX 특수문자 escape.

outline 의 bullet/title/hint 는 자연어 + inline math ($...$) 가 섞여 있다.
`$...$` 블록 안은 raw LaTeX 이므로 건드리지 않고 바깥만 escape.
equations 는 별도 함수(`equation_passthrough`) 로 pass-through.
"""

from __future__ import annotations

import re

_MATH_SPLIT = re.compile(r"(\$[^$]*\$)")

_ESCAPE_ORDER = [
    ("\\", r"\textbackslash{}"),
    ("&", r"\&"),
    ("%", r"\%"),
    ("#", r"\#"),
    ("_", r"\_"),
    ("{", r"\{"),
    ("}", r"\}"),
    ("~", r"\textasciitilde{}"),
    ("^", r"\textasciicircum{}"),
    ("$", r"\$"),
]


def _escape_plain(s: str) -> str:
    # 백슬래시 먼저 처리해야 이후 치환에서 중복 escape 되지 않는다.
    for ch, rep in _ESCAPE_ORDER:
        s = s.replace(ch, rep)
    return s


def escape_outside_math(text: str) -> str:
    """$...$ 영역을 보존하고 바깥의 TeX 특수문자만 escape.

    한글·영문 일반 텍스트는 그대로 유지 (XeLaTeX+kotex 이 Unicode 처리).
    """
    if not text:
        return ""
    parts = _MATH_SPLIT.split(text)
    out: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:  # $...$
            out.append(part)
        else:
            out.append(_escape_plain(part))
    return "".join(out)


def equation_passthrough(eq: str) -> str:
    """equations 필드는 이미 raw LaTeX — 그대로 반환."""
    return eq
