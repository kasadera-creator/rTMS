# rtms_app/assessment_rules.py
"""Centralized HAM-D evaluation thresholds and rules.

Future-proof for protocol-specific thresholds if needed.
"""
from typing import Optional


# Thresholds (currently protocol-independent; can be extended later)
RESPONSE_RATE_THRESHOLD = 0.50  # 50%
REMISSION_HAMD17_THRESHOLD = 7
REMISSION_HAMD21_THRESHOLD = 9

# Severity classification for HAM-D17
HAMD17_SEVERITY_BANDS = [
    (0, 7, "正常"),
    (8, 13, "軽症"),
    (14, 18, "中等症"),
    (19, 22, "重症"),
    (23, 999, "最重症"),
]


def classify_hamd17_severity(score: Optional[int]) -> Optional[str]:
    """Return severity band label for HAM-D17 score."""
    if score is None:
        return None
    for low, high, label in HAMD17_SEVERITY_BANDS:
        if low <= score <= high:
            return label
    return "最重症"


def compute_improvement_rate(baseline: Optional[int], current: Optional[int]) -> Optional[float]:
    """Calculate improvement rate: (baseline - current) / baseline.

    Returns None if baseline is missing or zero.
    """
    if baseline is None or baseline == 0 or current is None:
        return None
    return (baseline - current) / float(baseline)


def classify_response_status(
    score_17: Optional[int],
    improvement: Optional[float],
    remission_threshold: int = REMISSION_HAMD17_THRESHOLD,
    response_threshold: float = RESPONSE_RATE_THRESHOLD,
) -> str:
    """Classify response status uniformly: 反応なし / 反応 / 寛解.

    Args:
        score_17: HAM-D17 current score
        improvement: improvement rate as fraction (e.g., 0.25 for 25%)
        remission_threshold: HAM-D17 threshold for remission (default 7)
        response_threshold: improvement threshold (default 0.50 = 50%)

    Returns:
        "寛解" | "反応" | "反応なし" | "未評価"
    """
    if score_17 is None:
        return "未評価"
    # Remission check first
    if score_17 <= remission_threshold:
        return "寛解"
    # Response check
    if improvement is not None and improvement >= response_threshold:
        return "反応"
    return "反応なし"
