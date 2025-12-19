from django import template

register = template.Library()

@register.filter
def get_item(mapping, key):
    """Return mapping[key] if possible, else empty string.
    Works for dict-like objects used in templates.
    """
    try:
        if mapping is None:
            return ""
        # Prefer dict-like API
        if hasattr(mapping, 'get'):
            return mapping.get(key, "")
        # Fallback to subscription
        return mapping[key]
    except Exception:
        return ""
