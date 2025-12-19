from __future__ import annotations
from typing import Optional


def classify_hamd_response(hamd17: Optional[int], improvement_pct: Optional[float], hamd24: Optional[int] = None) -> str:
    """Classify response status uniformly across app and print.

    Returns one of: "寛解" | "無効" | "反応" | "未評価"
    Rules:
    - 未評価: when hamd17 is None
    - 寛解: hamd17 <= 7 (HAMD24 option not used; if adopted, extend condition to <= 9)
    - 無効: not remitted and improvement_pct is not None and < 20%
    - 反応: otherwise
    """
    if hamd17 is None:
        return "未評価"
    if hamd17 <= 7:
        return "寛解"
    if improvement_pct is not None and improvement_pct < 20.0:
        return "無効"
    return "反応"


def classify_hamd17_severity(hamd17: Optional[int]) -> Optional[str]:
    """Return severity band for HAMD17 total.

    Bands: 正常 / 軽症 / 中等症 / 重症 / 最重症. Returns None when score is missing.
    """
    if hamd17 is None:
        return None
    if hamd17 <= 7:
        return "正常"
    if hamd17 <= 13:
        return "軽症"
    if hamd17 <= 18:
        return "中等症"
    if hamd17 <= 22:
        return "重症"
    return "最重症"
