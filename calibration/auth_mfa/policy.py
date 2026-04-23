from django.conf import settings


def is_mfa_globally_enabled() -> bool:
    return bool(getattr(settings, "MFA_ENABLED", False))