"""zero_shot_rec의 stage별 프롬프트 빌더.

공통 컨벤션:
- 입력 이미지는 inputs/image.png (Stage 0/1) 또는 inputs/roi.png (Stage 2).
- 좌표계: 정규화 [0, 1000], 원점 좌상단, y는 아래로 증가.
- 출력은 outputs/result.json 파일에 JSON으로. 또한 "last message" fallback을
  위해 최종 응답에도 동일 JSON을 그대로 담는다 (codex_runner.py:670-675).
"""

from __future__ import annotations


_COMMON_TAIL = (
    "\n\n최종 출력은 JSON 하나만. outputs/result.json 파일로 저장하고, "
    "응답 말미에도 동일한 JSON을 그대로 담아라. 스키마(_schema.json)를 "
    "엄격히 준수. 설명/markdown/추가 텍스트 금지."
)


def build_stage0_presence_prompt(condition: str) -> str:
    return (
        f'이미지(inputs/image.png)에 다음 조건의 target이 존재하는지 판정하라.\n\n'
        f'조건(referring expression):\n"{condition}"\n\n'
        f'판정 기준:\n'
        f'- target_present=true: 이미지에 조건의 target이 실제로 존재 (완벽하게 '
        f'일치하지 않아도 조건과 합리적으로 부합하는 대상이 있음)\n'
        f'- target_present=false: 이미지에 target이 아예 없음 '
        f'(예: "빨간 차" 요청인데 이미지에 차 자체가 없음)\n\n'
        f'reasoning에 판단 근거를 1문장으로 간결히.'
        + _COMMON_TAIL
    )


def build_stage0_propose_prompt(condition: str, n_candidates: int = 3) -> str:
    return (
        f'이미지(inputs/image.png)에 다음 조건의 target이 존재함이 이미 확인되었다. '
        f'이 target에 대한 bounding box 후보를 정확히 {n_candidates}개 제안하라.\n\n'
        f'조건(referring expression):\n"{condition}"\n\n'
        f'좌표 규약: 정규화 [0, 1000] float, (x1, y1)=좌상단, (x2, y2)=우하단.\n\n'
        f'규칙:\n'
        f'- 반드시 정확히 {n_candidates}개 제안 (타이트/느슨/약간 이동 등 다양한 가설).\n'
        f'- 박스는 target을 완전히 포함하되 과도한 여백 최소화.\n'
        f'- 후보끼리 위치/크기가 충분히 구분되어야 함 (IoU가 너무 높지 않게).\n'
        f'- 각 후보에 id (1..{n_candidates}), 4개 좌표, rationale 1~2문장.'
        + _COMMON_TAIL
    )


def build_stage0_pick_prompt(condition: str) -> str:
    return (
        f'이미지(inputs/image.png)에는 번호(1, 2, 3)가 매겨진 컬러 박스가 그려져 있다.\n'
        f'다음 조건과 가장 잘 맞는 박스의 번호를 고르라.\n\n'
        f'조건:\n"{condition}"\n\n'
        f'판단 기준:\n'
        f'- target이 박스 안에 제대로 포함되어 있는가\n'
        f'- 박스가 target 외 불필요한 영역을 많이 포함하지 않는가\n'
        f'- 조건에 명시된 속성(색/위치/관계 등)과 가장 일치하는 것\n'
        f'best_id와 선택 근거(reason)를 반환.'
        + _COMMON_TAIL
    )


def build_stage1_containment_prompt(condition: str) -> str:
    return (
        f'이미지(inputs/image.png)에는 빨간 bounding box가 그려져 있다.\n\n'
        f'조건(referring expression):\n"{condition}"\n\n'
        f'이전 단계에서 이 박스가 target을 담고 있다고 이미 검증되었다. '
        f'이 단계의 유일한 임무는 박스의 fit 품질 판정 — '
        f'"target이 있는가?"를 다시 묻지 않는다 (존재는 확정).\n\n'
        f'선택지:\n'
        f'  A = 박스가 target에 타이트하게 맞음 (refinement 종료 가능)\n'
        f'  B = target은 박스 안에 있지만 박스가 너무 커서 여백 과다 → 축소 필요\n'
        f'  C = target 일부가 박스 밖으로 잘림 → 확장 필요\n'
        f'  E = 여러 target 후보가 박스 안에 포함되어 모호함\n\n'
        f'- B일 때 loose_sides에 여백이 큰 변들을 나열 (top/bottom/left/right).\n'
        f'- C일 때 clipped_sides에 잘림이 있는 변들을 나열.\n'
        f'- 그 외는 해당 배열을 빈 배열로.\n'
        f'- notes에 판단 근거 1~2문장 (target의 실제 경계가 어디에 있는지).'
        + _COMMON_TAIL
    )


def build_stage2_edge_prompt(condition: str,
                             edge: str,
                             dot_labels: list[str]) -> str:
    edge_kr = {
        "top": "윗", "bottom": "아래", "left": "왼쪽", "right": "오른쪽",
    }.get(edge, edge)
    axis = "수평" if edge in ("top", "bottom") else "수직"
    region_desc = (
        "좌(L)/중(C)/우(R)"
        if edge in ("top", "bottom")
        else "상(L)/중(C)/하(R)"
    )
    outward_desc = {
        "top":    "위쪽 (이미지 상단으로)",
        "bottom": "아래쪽 (이미지 하단으로)",
        "left":   "왼쪽 (이미지 좌측으로)",
        "right":  "오른쪽 (이미지 우측으로)",
    }[edge]
    labels_preview = ", ".join(dot_labels[:6])
    if len(dot_labels) > 6:
        labels_preview += f", ..., {dot_labels[-1]}"

    return (
        f'두 개의 이미지가 주어진다.\n'
        f'- inputs/roi.png: 현재 박스의 "{edge_kr} 경계" 주변을 확대한 ROI.\n'
        f'    · 빨간 {axis}선 = 현재 박스의 {edge_kr} 경계 위치\n'
        f'    · 노란 점 {len(dot_labels)}개(라벨 {labels_preview}) = 후보 좌표 앵커\n'
        f'- inputs/full.png: 전체 이미지 + 현재 박스(빨강). 주변 문맥 파악용.\n\n'
        f'조건(referring expression):\n"{condition}"\n\n'
        f'**판정 절차**: 먼저 full.png로 target의 위치와 대략적인 경계를 확인. '
        f'그 다음 roi.png로 실제 {edge_kr} 경계의 정확한 위치를 점 라벨로 특정. '
        f'roi.png에 나온 점 라벨만 nearest_point로 사용 가능.\n\n'
        f'다음 세 가지를 판정하라.\n\n'
        f'1) direction — target의 실제 {edge_kr} 경계는 현재 빨간 선 대비 어디?\n'
        f'   - outward: 박스 바깥 방향(={outward_desc}) → 박스 확장 필요\n'
        f'   - same:    선이 실제 경계와 거의 일치\n'
        f'   - inward:  박스 안쪽 → 박스 축소 필요\n\n'
        f'2) region — 선을 {region_desc} 3구간으로 볼 때 어긋남이 가장 큰 구간.\n'
        f'   - uniform: 세 구간 모두 비슷하게 어긋남\n\n'
        f'3) nearest_point — target의 실제 {edge_kr} 경계에 가장 가까운 점의 라벨.\n'
        f'   제공된 라벨 중 하나만. 선이 실제 경계와 완전히 일치하면 선 위의 점을.\n\n'
        f'4) confidence — 0.0~1.0. 자신 없거나 ROI에서 target이 안 보이면 ≤0.3.\n'
        f'5) notes — 판단 근거 한 줄.'
        + _COMMON_TAIL
    )
