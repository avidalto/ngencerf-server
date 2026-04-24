import base64
import json
import logging
import secrets
from typing import cast
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.hashers import make_password, check_password
from django.core import signing
from django.core.signing import BadSignature, SignatureExpired
from django.db import transaction
from django_otp.plugins.otp_totp.models import TOTPDevice
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from calibration.models import MFARecoveryCode
from calibration.util.calibration_validators import MFASetupResponseSerializer, ErrorResponseSerializer, MFAConfirmSetupSerializer, \
    LoginRequestSerializer, MFAVerifySerializer, MFARequiredResponseSerializer, MFASetupRequiredResponseSerializer, \
    TokenPairResponseSerializer, MFASetupRequestSerializer, MFAConfirmSetupResponseSerializer
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, validate_request, validate_response, get_user_email

logger = logging.getLogger(__name__)

User = get_user_model()

# Salt used to sign the temporary MFA token.
MFA_TOKEN_SALT = "mfa-login"
# The MFA token is only valid for a short time after password validation.
MFA_TOKEN_MAX_AGE_SECONDS = 300

MFA_ISSUER_NAME = "ngenCerf"

UI_ACTION_STAY_ON_LOGIN = "STAY_ON_LOGIN"
UI_ACTION_RETURN_TO_LOGIN = "RETURN_TO_LOGIN"
UI_ACTION_RETRY_SETUP_CONFIRM = "RETRY_SETUP_CONFIRM"
UI_ACTION_RETRY_MFA_VERIFY = "RETRY_MFA_VERIFY"
UI_ACTION_RESTART_MFA_SETUP = "RESTART_MFA_SETUP"

"""
To test, get a secret from the /auth/mfa endpoint.
Plug that secret into this code and run in management console

import pyotp

secret = "CG54RTPML3FNO76WD5TS4XTRR57LRFGA"  # extract from URL
totp = pyotp.TOTP(secret)
print(totp.now())"""


def is_mfa_globally_enabled() -> bool:
    return settings.MFA_ENABLED


def generate_mfa_token(user_id: int) -> str:
    """
    Generate a short-lived signed token used between password validation
    and MFA setup/verification.
    """
    # We do NOT return raw user_id to the client because it can be tampered with.
    # Instead, we return a server-signed token containing the user_id.
    return signing.dumps({"user_id": user_id}, salt=MFA_TOKEN_SALT)


def validate_mfa_token(mfa_token: str) -> int:
    """
    Validate the signed MFA token and return the embedded user_id.

    Raises:
        SignatureExpired: if the token is too old
        BadSignature: if the token has been tampered with
        KeyError: if user_id is missing
    """
    # This verifies:
    # 1) the token was created by this server
    # 2) the token was not modified
    # 3) the token is not too old
    payload = signing.loads(
        mfa_token,
        salt=MFA_TOKEN_SALT,
        max_age=MFA_TOKEN_MAX_AGE_SECONDS,
    )
    return payload["user_id"]


def mfa_error_response(
        *,
        error_code: str,
        ui_action: str,
        message: str,
        status_code: int,
) -> Response:
    """
    Return a consistent MFA error response shape for the UI.

    error_code:
        Specific machine-readable reason for logging/debugging.

    ui_action:
        High-level instruction the UI can use for navigation/state handling.
    """
    return Response(
        {
            "response_type": "error",
            "error_code": error_code,
            "ui_action": ui_action,
            "message": message,
        },
        status=status_code,
    )


@extend_schema(
    request=MFASetupRequestSerializer,
    responses={
        200: MFASetupResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error, parsing error, or invalid MFA token"
        ),
        404: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="MFA is not enabled"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Begin TOTP MFA setup after password validation"
)
@api_view(['POST'])
@permission_classes([AllowAny])
@handle_exceptions
def setup_mfa(request: Request) -> Response:
    """
    Begin TOTP MFA setup after the password step has already succeeded.

    This endpoint:
    - requires a valid short-lived MFA token from /auth/login/
    - returns 404 if MFA is globally disabled
    - creates a new TOTP setup secret, or resets an unfinished one
    - returns the otpauth URL for QR enrollment

    It does NOT mark MFA as enabled for the user.
    That only happens after setup is confirmed with a valid TOTP code.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(MFASetupRequestSerializer, data)
    if error_return:
        return error_return

    # MFA setup should behave as unavailable when the feature flag is off.
    # This keeps all MFA-related behavior completely disabled unless explicitly enabled.
    if not is_mfa_globally_enabled():
        return mfa_error_response(
            error_code="MFA_DISABLED",
            ui_action=UI_ACTION_RETURN_TO_LOGIN,
            message="MFA is not enabled.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    mfa_token = validator.get("mfa_token")

    try:
        # Validate the temporary MFA token before trusting the embedded user_id.
        user_id = validate_mfa_token(mfa_token)
    except SignatureExpired:
        return mfa_error_response(
            error_code="MFA_TOKEN_EXPIRED",
            ui_action=UI_ACTION_RETURN_TO_LOGIN,
            message="MFA token has expired. Please log in again.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except (BadSignature, KeyError):
        return mfa_error_response(
            error_code="INVALID_MFA_TOKEN",
            ui_action=UI_ACTION_RETURN_TO_LOGIN,
            message="Invalid MFA token.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return mfa_error_response(
            error_code="INVALID_USER",
            ui_action=UI_ACTION_RETURN_TO_LOGIN,
            message="Invalid user.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    logger.debug(f'{get_caller_name()}() resolved MFA setup user: {user.email}')

    # Get the user's default TOTP device/credential if it already exists.
    # Otherwise create a new unconfirmed device/credential.
    #
    # IMPORTANT:
    # - "device" here is NOT a physical phone or authenticator app
    # - it is a server-side credential (shared secret + config)
    #
    # confirmed=False means the user has NOT yet proven that their authenticator
    # app is correctly configured. That confirmation will happen in the next endpoint.
    device, created = TOTPDevice.objects.get_or_create(
        user=user,
        name="default",
        defaults={"confirmed": False},
    )

    if device.confirmed:
        return mfa_error_response(
            error_code="MFA_ALREADY_CONFIGURED",
            ui_action=UI_ACTION_RETURN_TO_LOGIN,
            message="MFA is already configured for this user.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # If an unconfirmed device/credential already existed, delete it and recreate it.
    #
    # Why:
    # - The user may have started setup earlier and never finished
    # - Recreating the device/credential guarantees a fresh secret each time setup is restarted
    # - That avoids reusing an old stale QR code / secret indefinitely
    #
    # If the device/credential was just created above, there is nothing to reset.
    if not created:
        device.delete()
        device = TOTPDevice.objects.create(
            user=user,
            name="default",
            confirmed=False,
        )

    # config_url is the provisioning URI in otpauth:// format.
    # Authenticator apps such as Google Authenticator can scan a QR code
    # generated from this value, or import it directly if your frontend supports that.
    issuer = MFA_ISSUER_NAME
    account = user.email

    label = quote(f"{issuer}:{account}")
    issuer_param = quote(issuer)

    secret = base64.b32encode(device.bin_key).decode("utf-8").replace("=", "")

    otpauth_url = (
        f"otpauth://totp/{label}"
        f"?secret={secret}"
        f"&issuer={issuer_param}"
        f"&algorithm=SHA1"
        f"&digits=6"
        f"&period=30"
    )

    response = {
        "otpauth_url": otpauth_url,
    }

    # Validate the outgoing response so it matches the documented serializer.
    response_validator, error_response = validate_response(MFASetupResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {user.email} from {get_caller_name()}() - '
        f'{json.dumps(response_validator.data)}'
    )

    return Response(response_validator.data)


@extend_schema(
    request=MFAConfirmSetupSerializer,
    responses={
        200: MFAConfirmSetupResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error, invalid MFA token, or invalid MFA code"
        ),
        404: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="MFA is not enabled or setup has not been started"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Confirm TOTP MFA setup after password validation"
)
@api_view(['POST'])
@permission_classes([AllowAny])
@handle_exceptions
def confirm_setup_mfa(request: Request) -> Response:
    """
    Confirm TOTP MFA setup after the password step has already succeeded.

    This endpoint:
    - requires a valid short-lived MFA token from /auth/login/
    - returns 404 if MFA is globally disabled
    - verifies the provided TOTP code against the user's unconfirmed device/credential
    - marks the device/credential as confirmed
    - marks user.mfa_enabled = True
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(MFAConfirmSetupSerializer, data)
    if error_return:
        return error_return

    if not is_mfa_globally_enabled():
        return mfa_error_response(
            error_code="MFA_DISABLED",
            ui_action=UI_ACTION_RETURN_TO_LOGIN,
            message="MFA is not enabled.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    mfa_token = validator.get("mfa_token")
    code = validator.get("code")

    try:
        # Validate the temporary MFA token before trusting the embedded user_id.
        user_id = validate_mfa_token(mfa_token)
    except SignatureExpired:
        return mfa_error_response(
            error_code="MFA_TOKEN_EXPIRED",
            ui_action=UI_ACTION_RETURN_TO_LOGIN,
            message="MFA token has expired. Please log in again.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except (BadSignature, KeyError):
        return mfa_error_response(
            error_code="INVALID_MFA_TOKEN",
            ui_action=UI_ACTION_RETURN_TO_LOGIN,
            message="Invalid MFA token.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return mfa_error_response(
            error_code="INVALID_USER",
            ui_action=UI_ACTION_RETURN_TO_LOGIN,
            message="Invalid user.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    logger.debug(f'{get_caller_name()}() resolved MFA confirm user: {user.email}')

    try:
        device = TOTPDevice.objects.get(
            user=user,
            name="default",
            confirmed=False,
        )
    except TOTPDevice.DoesNotExist:
        return mfa_error_response(
            error_code="MFA_SETUP_NOT_STARTED",
            ui_action=UI_ACTION_RESTART_MFA_SETUP,
            message="MFA setup has not been started for this user.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    # Allow slight clock drift during initial setup confirmation.
    # tolerance=1 allows the previous/next 30-second window.
    device.tolerance = 1

    if not device.verify_token(code):
        return mfa_error_response(
            error_code="INVALID_MFA_SETUP_CODE",
            ui_action=UI_ACTION_RETRY_SETUP_CONFIRM,
            message="Invalid authentication code.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    device.confirmed = True
    device.save(update_fields=["tolerance", "confirmed"])

    user.mfa_enabled = True
    user.save(update_fields=["mfa_enabled"])

    recovery_codes = replace_recovery_codes_for_user(user)

    response = {
        "message": "MFA setup completed successfully.",
        "recovery_codes": recovery_codes,
    }

    response_validator, error_response = validate_response(MFAConfirmSetupResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {user.email} from {get_caller_name()}() - '
        f'{json.dumps(response_validator.data)}'
    )

    return Response(response_validator.data)


@extend_schema(
    request=LoginRequestSerializer,
    responses={
        200: OpenApiResponse(
            description="Login succeeded, MFA setup required, or MFA verification required"
        ),
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        401: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Invalid credentials or inactive user"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Authenticate a user and determine the next MFA step"
)
@api_view(["POST"])
@permission_classes([AllowAny])
@handle_exceptions
def login(request: Request) -> Response:
    data = request.data
    logger.debug(f"{get_caller_name()}() login attempt - {data.get('email')}")

    validator, error_return = validate_request(LoginRequestSerializer, data)
    if error_return:
        return error_return

    email = validator.get("email")
    password = validator.get("password")

    user = authenticate(request, email=email, password=password)

    if not user:
        return mfa_error_response(
            error_code="INVALID_CREDENTIALS",
            ui_action=UI_ACTION_STAY_ON_LOGIN,
            message="Invalid credentials",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    user = cast(User, user)

    if not user.is_active:
        return mfa_error_response(
            error_code="USER_DISABLED",
            ui_action=UI_ACTION_STAY_ON_LOGIN,
            message="User account is disabled",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    mfa_global = is_mfa_globally_enabled()

    # Case 1: MFA OFF -> ignore all per-user MFA state and stored credentials.
    if not mfa_global:
        refresh = RefreshToken.for_user(user)
        response = {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
        }

        response_validator, error_response = validate_response(TokenPairResponseSerializer, response)
        if error_response:
            return error_response

        return Response(response_validator.data)

    # Case 2: MFA ON, but this user is not currently MFA-enabled -> force setup.
    # Existing stored devices/credentials are ignored while the user flag is False.
    if not user.mfa_enabled:
        response = {
            "mfa_setup_required": True,
            "mfa_token": generate_mfa_token(user.id),
            "message": "MFA setup required before login.",
        }

        response_validator, error_response = validate_response(MFASetupRequiredResponseSerializer, response)
        if error_response:
            return error_response

        return Response(response_validator.data, status=status.HTTP_200_OK)

    # Case 3: MFA ON and this user is MFA-enabled -> require MFA verification.
    # Return a signed temporary token instead of raw user_id.
    # This proves the password step already succeeded for this user.
    response = {
        "mfa_required": True,
        "mfa_token": generate_mfa_token(user.id),
        "message": "MFA verification required.",
    }

    response_validator, error_response = validate_response(MFARequiredResponseSerializer, response)
    if error_response:
        return error_response

    return Response(response_validator.data, status=status.HTTP_200_OK)


@extend_schema(
    request=MFAVerifySerializer,
    responses={
        200: TokenPairResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error, invalid MFA token, MFA not configured, or invalid/expired code"
        ),
        404: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="MFA is not enabled"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Verify MFA code and complete login"
)
@api_view(["POST"])
@permission_classes([AllowAny])
@handle_exceptions
def verify_mfa(request: Request) -> Response:
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(MFAVerifySerializer, data)
    if error_return:
        return error_return

    if not is_mfa_globally_enabled():
        return mfa_error_response(
            error_code="MFA_DISABLED",
            ui_action=UI_ACTION_RETURN_TO_LOGIN,
            message="MFA is not enabled.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    mfa_token = validator.get("mfa_token")
    code = validator.get("code")

    try:
        # Validate the temporary MFA token before trusting the embedded user_id.
        user_id = validate_mfa_token(mfa_token)
    except SignatureExpired:
        return mfa_error_response(
            error_code="MFA_TOKEN_EXPIRED",
            ui_action=UI_ACTION_RETURN_TO_LOGIN,
            message="MFA token has expired. Please log in again.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except (BadSignature, KeyError):
        return mfa_error_response(
            error_code="INVALID_MFA_TOKEN",
            ui_action=UI_ACTION_RETURN_TO_LOGIN,
            message="Invalid MFA token.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return mfa_error_response(
            error_code="INVALID_USER",
            ui_action=UI_ACTION_RETURN_TO_LOGIN,
            message="Invalid user.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    logger.debug(f'{get_caller_name()}() resolved MFA verify user: {user.email}')

    # If the user flag is off, ignore any stored devices/credentials.
    if not user.mfa_enabled:
        return mfa_error_response(
            error_code="MFA_NOT_ENABLED_FOR_USER",
            ui_action=UI_ACTION_RETURN_TO_LOGIN,
            message="MFA is not enabled for this user.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    device = TOTPDevice.objects.filter(user=user, confirmed=True).first()

    # If the flag says MFA is enabled but there is no confirmed credential,
    # treat that as an inconsistent state and fail safely.
    if not device:
        return mfa_error_response(
            error_code="MFA_DEVICE_MISSING",
            ui_action=UI_ACTION_RETURN_TO_LOGIN,
            message="MFA is enabled for this user but no confirmed MFA credential exists.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # tolerance was already stored on the device during MFA setup confirmation.
    if not device.verify_token(code):
        if not verify_recovery_code(user, code):
            return mfa_error_response(
                error_code="INVALID_MFA_CODE",
                ui_action=UI_ACTION_RETRY_MFA_VERIFY,
                message="Invalid or expired code",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        logger.info(f'{get_caller_name()}() recovery code used for MFA verify user: {user.email}')

    refresh = RefreshToken.for_user(user)
    response = {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
    }

    response_validator, error_response = validate_response(TokenPairResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {user.email} from {get_caller_name()}() - '
        f'{json.dumps(response_validator.data)}'
    )

    return Response(response_validator.data)


def generate_recovery_codes(num_codes: int = 10) -> list[str]:
    """
    Generate one-time MFA recovery codes.

    These are shown to the user once and stored only as hashes.
    """
    return [
        f"{secrets.token_hex(3)}-{secrets.token_hex(3)}"
        for _ in range(num_codes)
    ]


def replace_recovery_codes_for_user(user: User) -> list[str]:
    """
    Replace all existing recovery codes for a user and return the plaintext
    codes so the UI can show them once.
    """
    recovery_codes = generate_recovery_codes()

    MFARecoveryCode.objects.filter(user=user).delete()

    MFARecoveryCode.objects.bulk_create([
        MFARecoveryCode(
            user=user,
            code_hash=make_password(code),
            used=False,
        )
        for code in recovery_codes
    ])

    return recovery_codes


def verify_recovery_code(user: User, code: str) -> bool:
    """
    Verify and consume a one-time MFA recovery code.
    """
    with transaction.atomic():
        recovery_codes = (
            MFARecoveryCode.objects
            .select_for_update()
            .filter(user=user, used=False)
        )

        for recovery_code in recovery_codes:
            if check_password(code, recovery_code.code_hash):
                recovery_code.used = True
                recovery_code.save(update_fields=["used"])
                return True

    return False
