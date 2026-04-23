from django.conf import settings
from django.contrib.auth import get_user_model

User = get_user_model()


def is_mfa_globally_enabled() -> bool:
    return bool(getattr(settings, "MFA_ENABLED", False))


def is_mfa_enabled_for_user(user: User) -> bool:
    return bool(getattr(user, "mfa_enabled", False))


def should_require_mfa_for_user(user: User) -> bool:
    """
    Return True only when MFA is globally enabled and the user has completed MFA setup.
    """
    return is_mfa_globally_enabled() and is_mfa_enabled_for_user(user)


def should_require_mfa_setup_for_user(user: User) -> bool:
    """
    Return True when MFA is globally enabled but the user has not completed setup yet.
    """
    return is_mfa_globally_enabled() and not is_mfa_enabled_for_user(user)