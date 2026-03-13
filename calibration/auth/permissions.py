import logging

from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

logger = logging.getLogger(__name__)

ALLOWED_PATH_PREFIXES = (
    "/auth/users/me/",
    "/auth/users/activation/",
    "/auth/users/send_verification_email/",
    "/auth/users/verify_email_confirm/",
    "/auth/jwt/create/",
    "/auth/jwt/refresh/",
    "/auth/users/reset_password/",
    "/auth/ users/reset_password_confirm/"
)


class IsEmailVerifiedOrAllowed(BasePermission):
    """
    Global DRF permission enforcing email verification for authenticated users.

    Behavior:
    - Unauthenticated requests are allowed through this permission (other auth rules apply).
    - Verified users are allowed.
    - Unverified users are only allowed to access allowlisted auth endpoints needed
      to verify or change email and to obtain/refresh JWTs.
    - Otherwise, raises PermissionDenied with a specific error message/code.

    :return: None
    """

    def has_permission(self, request, view):
        """
        Determine whether the incoming request should be permitted.

        This is called by DRF before executing the view. When the user is authenticated
        but not email-verified, it blocks non-allowlisted endpoints.

        :param request: DRF Request.
        :param view: The DRF view instance.
        :return: True if permitted; raises PermissionDenied otherwise.
        """
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return True

        if getattr(user, "email_verified", False):
            return True

        if request.path.startswith(ALLOWED_PATH_PREFIXES):
            return True

        logger.warning(
            "Email verification required: path=%s user=%r verified=%s",
            request.path,
            getattr(user, "email", None),
            getattr(user, "email_verified", None),
        )
        raise PermissionDenied(
            detail="Email address is not verified. Please verify your email or change it to a verifiable address.",
            code="email_not_verified",
        )
