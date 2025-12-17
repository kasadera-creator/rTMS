from rtms_app.models import MappingSession


def get_latest_mt_percent(patient):
    """Return latest resting MT% from mapping sessions for initial treatment defaults."""
    mapping = (
        MappingSession.objects
        .filter(patient=patient)
        .order_by('-date', '-id')
        .first()
    )
    if not mapping:
        return None
    return mapping.resting_mt
