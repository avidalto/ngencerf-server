import logging
from typing import cast
from urllib.parse import unquote

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.util.calibration_validators import ErrorResponseSerializer, SendVerificationEmailRequestSerializer, \
    VerifyEmailConfirmRequestSerializer
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, get_user_email, validate_request, get_elapsed_str, ResponseError

logger = logging.getLogger(__name__)

User = get_user_model()


def _send_email_verification_message(user: User, target_email: str) -> None:
    """
    Send a signed email verification message to the given email address.

    This helper centralizes the actual email creation/sending logic so it can be
    reused by:
    - send_verification_email() for resend / change-email verification
    - send_initial_verification_email() for initial registration verification

    :param user: User who is being verified.
    :param target_email: Email address that should receive the verification email.
    :return: None
    """
    # Create signed verification token bound to (user_id, target_email).
    token = _make_email_verify_token(user_id=int(user.id), email=target_email)

    verify_url = (
        f"{settings.EMAIL_FRONTEND_URL.rstrip('/')}"
        f"/login?action=verify-email&token={token}"
    )

    context = {
        "user": user,
        "site_name": settings.EMAIL_SITE_NAME,
        "verify_url": verify_url,
        "target_email": target_email,
    }

    subject = f"Verify your email address for {settings.EMAIL_SITE_NAME}"
    text_body = render_to_string("email/verify_email.txt", context)
    html_body = render_to_string("email/verify_email.html", context)

    email_message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[target_email],
    )
    email_message.attach_alternative(html_body, "text/html")
    email_message.send(fail_silently=False)


def send_initial_verification_email(user: User) -> None:
    """
    Send the initial email verification message for a newly created user.

    This is intended to replace Djoser's activation email when we are using
    the custom signed-token verification flow for initial registration.

    Unlike resend/change-email verification, this always targets the user's
    current email address stored on the account.

    :param user: Newly created user.
    :return: None
    :raises ValidationError: If the user does not have a usable email address.
    """
    target_email = (getattr(user, "email", "") or "").strip()

    if not target_email:
        raise ValidationError({"email": ["No email available to verify."]})

    _send_email_verification_message(user, target_email)

    logger.info(
        "Sent initial verification email: user_id=%s email=%r",
        user.id,
        target_email,
    )


def _normalize_token_for_signing(token: str) -> str:
    """
    Normalize a token coming from a URL/query param.

    Common failure modes:
    - Token is percent-encoded (contains %XX) and never decoded before POST.
      Example: ":" becomes "%3A", "/" becomes "%2F", "+" can become "%2B", etc.
    - Token is decoded twice or encoded twice.
    - Token has leading/trailing whitespace.

    This normalizes the token while trying to avoid altering already-correct tokens.

    :param token: Incoming token string.
    :return: Normalized token string suitable for signing.loads().
    """
    token = (token or "").strip()

    # If it contains percent-encoding, decode once.
    # "Percent-encoded" means URL-escaping: characters are encoded as %XX hex sequences.
    # This often happens when a token is read from a querystring and then POSTed without decoding.
    if "%" in token:
        decoded_once = unquote(token)

        # Heuristic: if it *still* contains percent-encoding after one decode, decode again.
        # This handles accidental double-encoding.
        if "%" in decoded_once:
            decoded_twice = unquote(decoded_once)
            return decoded_twice.strip()

        return decoded_once.strip()

    return token


def _make_email_verify_token(*, user_id: int, email: str) -> str:
    """
    Create a signed, time-limited verification token for a user/email pair.

    This token is used for the custom email verification flow:
      - initial registration verification
      - resend verification email
      - change email and verify the new email

    It is embedded in a verification link sent via email, then later POSTed to
    verify_email_confirm.

    :param user_id: ID of the user requesting verification.
    :param email: Email address to be verified (current or requested new email).
    :return: URL-safe signed token string.
    """
    payload = {"user_id": user_id, "email": email}
    token = signing.dumps(payload, salt=settings.EMAIL_VERIFY_SALT)

    logger.info("Email verify token issued: user_id=%s email=%r", user_id, email)

    return token


def _load_email_verify_token(token: str) -> dict:
    """
    Validate and decode a signed email verification token.

    The token is generated by _make_email_verify_token() using
    django.core.signing.dumps() with settings.EMAIL_VERIFY_SALT.

    Expiration is enforced by django.core.signing.loads(..., max_age=...),
    where max_age is defined by settings.EMAIL_VERIFY_MAX_AGE_SECONDS.

    :param token: Signed token string from the verification link.
    :return: Decoded payload dict containing at least {"user_id": ..., "email": ...}.
    :raises signing.SignatureExpired: Token was valid but exceeded the configured lifetime.
    :raises signing.BadSignature: Token signature is invalid, corrupted, or was signed with a different salt.
    """
    raw_token = token or ""
    normalized_token = _normalize_token_for_signing(raw_token)

    # "percent_encoded" means the token contains URL-escaped %XX sequences (e.g. "%3A").
    # If the frontend POSTs the querystring value without decoding it first, the signature won't match.
    percent_encoded = "%" in raw_token

    # "normalized_changed" indicates we altered the incoming string (strip and/or unquote once/twice).
    # If this flips to True often, it points to a frontend token handling bug.
    normalized_changed = normalized_token != raw_token.strip()

    logger.info(
        "Email verify token load: percent_encoded=%s normalized_changed=%s",
        percent_encoded,
        normalized_changed,
    )

    try:
        payload = signing.loads(
            normalized_token,
            salt=settings.EMAIL_VERIFY_SALT,
            max_age=settings.EMAIL_VERIFY_MAX_AGE_SECONDS,
        )
        logger.info(
            "Email verify token OK: keys=%s",
            sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
        )
        return payload

    except signing.SignatureExpired:
        logger.warning("Email verify token expired")
        raise

    except signing.BadSignature:
        logger.warning("Email verify token invalid (bad signature)")
        raise


@extend_schema(
    request=SendVerificationEmailRequestSerializer,
    responses={
        204: OpenApiResponse(description="Verification email sent"),
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error",
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error",
        ),
    },
    description="Send a signed verification email to the user's current email or a requested new email",
)
@api_view(["POST"])
@handle_exceptions
@permission_classes([IsAuthenticated])
def send_verification_email(request: Request) -> Response:
    """
    Send an email verification link to the user's current email or a new email.

    Called by the UI when:
    - The user is unverified and clicks "resend verification", OR
    - The user wants to change email and verify the new email.

    Notes about which email is used:
    - The user always has a "current email" stored on the account (user.email).
    - The UI may also provide a "new email" (new_email) to replace the current email.
    - The email we are sending to / verifying ("target email") is:
        - new_email, if provided
        - otherwise the current email

    Request body:
      - new_email (optional): if provided, send verification to this email; otherwise verify current user.email

    Response:
      - 204 No Content on success

    :param request: DRF Request (authenticated).
    :return: 204 No Content on success.
    :raises ValidationError: If there is no usable email to verify.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(SendVerificationEmailRequestSerializer, data)
    if error_return:
        return error_return

    user = request.user

    # Current email already stored on the user record.
    current_email = (getattr(user, "email", "") or "").strip()

    # Optional new email provided by UI when user wants to change email.
    requested_new_email = (validator.get("new_email") or "").strip()

    if requested_new_email:
        # If the user is trying to change their email, fail early if another
        # account already uses that address. Exclude the current user so they
        # can re-enter their own email without triggering a false duplicate.
        email_exists = User.objects.filter(email__iexact=requested_new_email).exclude(id=user.id).exists()
        if email_exists:
            return ResponseError("A user with this email already exists.")

    # The email address we will actually send to / verify.
    target_email = requested_new_email or current_email

    if not target_email:
        # Defensive fallback. In this system, email is required, so this should not occur
        # unless the database contains legacy/bad rows or the user record is corrupted.
        raise ValidationError({"new_email": ["No email available to verify."]})

    _send_email_verification_message(cast(User, user), target_email)

    logger.info(
        "Sent verification email: user_id=%s current_email=%r target_email=%r new_email_provided=%s",
        user.id,
        current_email,
        target_email,
        bool(requested_new_email),
    )

    logger.debug(f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)}')
    return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    request=VerifyEmailConfirmRequestSerializer,
    responses={
        204: OpenApiResponse(description="Email verified"),
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error",
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error",
        ),
    },
    description="Confirm a signed verification token (used for initial registration, resend verification, and email changes)",
)
@api_view(["POST"])
@handle_exceptions
@permission_classes([AllowAny])
def verify_email_confirm(request: Request) -> Response:
    """
    Confirm an email verification token and mark the user's email as verified.

    Called by the UI when the user clicks a signed-token verification link in their email.
    This endpoint is used for:
      - initial registration verification
      - resend verification
      - change-email verification

    The UI extracts the token from the URL and POSTs it to this endpoint.

    Request body:
      - token: signed token containing {user_id, email}

    Behavior:
      - Loads and validates the signed token (including expiry).
      - Fetches the user by user_id from the token.
      - If token email differs from the user's current email, updates user.email
        (and user.username if you maintain username=email).
      - Sets user.email_verified=True.

    Response:
      - 204 No Content on success

    :param request: DRF Request (unauthenticated allowed).
    :return: 204 No Content on success.
    :raises ValidationError: If token is missing, invalid, expired, or user not found.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(VerifyEmailConfirmRequestSerializer, data)
    if error_return:
        return error_return

    token = (validator.get("token") or "").strip()

    try:
        payload = _load_email_verify_token(token)
    except signing.SignatureExpired:
        raise ValidationError({"token": ["Token expired."]})
    except signing.BadSignature:
        raise ValidationError({"token": ["Invalid token."]})

    user_id = payload.get("user_id")
    email = (payload.get("email") or "").strip()

    if not user_id or not email:
        raise ValidationError({"token": ["Invalid token payload."]})

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        raise ValidationError({"token": ["User not found."]})

    update_fields: list[str] = []

    # If the token email differs from the stored email, treat this as "change email + verify".
    if (getattr(user, "email", "") or "").strip().lower() != email.lower():
        email_exists = (
            User.objects
            .filter(email__iexact=email)
            .exclude(id=user.id)
            .exists()
        )
        if email_exists:
            return ResponseError("A user with this email already exists.")

        user.email = email
        update_fields.append("email")

        # Keep username==email
        if hasattr(user, "username"):
            user.username = email
            update_fields.append("username")

    if not getattr(user, "email_verified", False):
        user.email_verified = True
        update_fields.append("email_verified")

    if update_fields:
        user.save(update_fields=update_fields)

    logger.info(
        "Email verified via signed token: user_id=%s email=%r changed_email=%s",
        user.id,
        user.email,
        "email" in update_fields,
    )

    logger.debug(f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)}')
    return Response(status=status.HTTP_204_NO_CONTENT)
