import datetime
from typing import List, Optional, Set

DEFAULT_TOTAL_SESSIONS = 30
DEFAULT_PER_WEEK = 5


def generate_planned_dates(start_date: datetime.date,
                           total_sessions: int = DEFAULT_TOTAL_SESSIONS,
                           per_week: int = DEFAULT_PER_WEEK,
                           skip_weekdays: Optional[set] = None,
                           holidays: Optional[set] = None) -> List[datetime.date]:
    """
    Generate a list of planned treatment dates starting from `start_date`.

    - By default skips Saturday(5) and Sunday(6).
    - `holidays` may be a set of datetime.date to exclude (optional).
    Returns a list of `total_sessions` dates.
    """
    if skip_weekdays is None:
        skip_weekdays = {5, 6}
    holidays = holidays or set()

    dates: List[datetime.date] = []
    d = start_date
    # safety guard to avoid infinite loop
    max_days = total_sessions * 7 + 365
    tried = 0
    while len(dates) < total_sessions and tried < max_days:
        if d.weekday() not in skip_weekdays and d not in holidays:
            dates.append(d)
        d = d + datetime.timedelta(days=1)
        tried += 1
    return dates


def session_info_for_date(planned_dates: List[datetime.date],
                          target_date: datetime.date,
                          per_week: int = DEFAULT_PER_WEEK) -> Optional[dict]:
    """
    Given a planned_dates list and a target_date, return session number and week number.
    Returns None if target_date is not in planned_dates.
    """
    try:
        idx = planned_dates.index(target_date)
    except ValueError:
        return None
    n = idx + 1
    week = (idx // per_week) + 1
    return {"session_no": n, "week_no": week}


def format_rtms_label(session_no: int, week_no: int) -> str:
    return f"rTMS治療 {session_no}回目（第{week_no}週）"


def mapping_dates_from_planned(planned_dates: List[datetime.date], per_week: int = DEFAULT_PER_WEEK) -> List[datetime.date]:
    """
    Given planned_dates (sequential treatment dates), return the first date of each week-block.
    Example: planned_dates[0:5] -> week1, planned_dates[5:10] -> week2, etc.
    """
    mapping = []
    for i in range(0, len(planned_dates), per_week):
        block = planned_dates[i:i+per_week]
        if block:
            mapping.append(block[0])
    return mapping

# ==========================================================
# New canonical scheduling helpers (clinic closures + no drift)
# ==========================================================

def is_year_end_closed(d: datetime.date) -> bool:
    """Year-end closure: Dec 29 to Jan 3."""
    return (d.month == 12 and d.day >= 29) or (d.month == 1 and d.day <= 3)


def is_closed(d: datetime.date, holidays: Optional[Set[datetime.date]] = None) -> bool:
    """Closed if weekend, year-end, or in provided holiday set."""
    if d.weekday() in (5, 6):
        return True
    if is_year_end_closed(d):
        return True
    if holidays and d in holidays:
        return True
    return False


def next_open_day(d: datetime.date, holidays: Optional[Set[datetime.date]] = None) -> datetime.date:
    """Roll forward to the next open clinic day (never backward)."""
    cur = d
    while is_closed(cur, holidays):
        cur = cur + datetime.timedelta(days=1)
    return cur


def generate_treatment_dates(start_date: datetime.date,
                             total: int = DEFAULT_TOTAL_SESSIONS,
                             holidays: Optional[Set[datetime.date]] = None) -> List[datetime.date]:
    """Generate 30 treatment dates on open days only (Mon-Fri, not holidays, not year-end)."""
    dates: List[datetime.date] = []
    d = start_date
    # guard to avoid infinite loops
    tried = 0
    max_days = total * 7 + 365
    while len(dates) < total and tried < max_days:
        if not is_closed(d, holidays):
            dates.append(d)
        d = d + datetime.timedelta(days=1)
        tried += 1
    return dates


def generate_mapping_dates(start_date: datetime.date,
                           weeks: int = 8,
                           holidays: Optional[Set[datetime.date]] = None) -> List[dict]:
    """
    Generate weekly mapping dates without drift.
    - Nominal is always start_date + 7*k
    - Actual rolls forward to the next open day if nominal is closed
    Returns list of {nominal, actual, week_no} dicts.
    """
    mapping = []
    for k in range(weeks):
        nominal = start_date + datetime.timedelta(days=7 * k)
        actual = next_open_day(nominal, holidays)
        mapping.append({"nominal": nominal, "actual": actual, "week_no": k + 1})
    return mapping
