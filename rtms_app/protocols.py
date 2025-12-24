# rtms_app/protocols.py
"""Protocol abstraction layer for rTMS.

Each course (Patient.protocol_type) specifies the protocol used.
This module centralizes protocol-specific logic (sessions, eval weeks, required fields).
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal, Optional

ProtocolType = Literal["INSURANCE", "PMS"]


@dataclass
class ProtocolSpec:
    """Specification for a treatment protocol.

    Attributes:
        code: INSURANCE | PMS
        display_name: Japanese display name
        total_sessions: Total planned treatment sessions (e.g., 30)
        required_evaluation_weeks: Week numbers where HAM-D must be entered
        allow_early_taper: Whether remission triggers taper (week 4-6)
    """
    code: ProtocolType
    display_name: str
    total_sessions: int
    required_evaluation_weeks: list[int]
    allow_early_taper: bool = True


# ========================================================================
# Protocol registry
# ========================================================================
# Future expansion: add fields for required questionnaires, discharge docs, etc.

INSURANCE_PROTOCOL = ProtocolSpec(
    code="INSURANCE",
    display_name="保険診療プロトコル",
    total_sessions=30,
    required_evaluation_weeks=[0, 3, 4, 6],  # baseline, w3, w4, w6
    allow_early_taper=True,
)

PMS_PROTOCOL = ProtocolSpec(
    code="PMS",
    display_name="市販後調査プロトコル",
    total_sessions=30,  # Placeholder; adjust per real PMS requirement
    required_evaluation_weeks=[0, 3, 4, 6],
    allow_early_taper=True,
)

# Map protocol code -> spec
_REGISTRY = {
    "INSURANCE": INSURANCE_PROTOCOL,
    "PMS": PMS_PROTOCOL,
}


def get_protocol_by_code(code: str) -> Optional[ProtocolSpec]:
    """Retrieve protocol specification by code.

    Args:
        code: INSURANCE | PMS

    Returns:
        ProtocolSpec or None if not found
    """
    return _REGISTRY.get(code)


def get_protocol(patient) -> ProtocolSpec:
    """Return the protocol spec for this patient's current course.

    Falls back to INSURANCE if unset or invalid.
    """
    from rtms_app.models import Patient  # delayed to avoid circular import
    if not isinstance(patient, Patient):
        # fallback: handle patient_id or None
        return INSURANCE_PROTOCOL

    protocol_code = getattr(patient, 'protocol_type', None) or "INSURANCE"
    spec = get_protocol_by_code(protocol_code)
    return spec or INSURANCE_PROTOCOL
