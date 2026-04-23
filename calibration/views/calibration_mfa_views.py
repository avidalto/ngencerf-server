import json
import logging

from django_otp.plugins.otp_totp.models import TOTPDevice
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.util.calibration_validators import EmptySerializer, MFASetupResponseSerializer, ErrorResponseSerializer
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, validate_request, validate_response, get_user_email
from calibration.auth_mfa.policy import is_mfa_globally_enabled

logger = logging.getLogger(__name__)


@extend_schema(
    request=EmptySerializer,
    responses={
        200: MFASetupResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
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
    description="Begin TOTP MFA setup for the authenticated user"
)
@api_view(['POST'])
@handle_exceptions
def setup_mfa(request: Request) -> Response:
    """
    Begin TOTP MFA setup for the authenticated user.

    This endpoint:
    - requires the user to already be authenticated
    - returns 404 if MFA is globally disabled
    - creates a new TOTP setup secret, or resets an unfinished one
    - returns the otpauth URL for QR enrollment

    It does NOT mark MFA as enabled for the user.
    That only happens after setup is confirmed with a valid TOTP code.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(EmptySerializer, data)
    if error_return:
        return error_return

    # MFA setup should behave as unavailable when the feature flag is off.
    # This keeps all MFA-related behavior completely disabled unless explicitly enabled.
    if not is_mfa_globally_enabled():
        return Response(
            {
                "response_type": "error",
                "message": "MFA is not enabled.",
            },
            status=status.HTTP_404_NOT_FOUND,
        )

    # This endpoint is authenticated, so request.user should be the current logged-in user.
    user = request.user

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

    # If the device/credential is already confirmed, then MFA setup was already completed earlier.
    # In that case, we do not want to generate a fresh secret here.
    if device.confirmed:
        return Response(
            {
                "response_type": "error",
                "message": "MFA is already configured for this user.",
            },
            status=status.HTTP_400_BAD_REQUEST,
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
    response = {
        "otpauth_url": device.config_url,
    }

    # Validate the outgoing response so it matches the documented serializer.
    response_validator, error_response = validate_response(MFASetupResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}() - '
        f'{json.dumps(response_validator.data)}'
    )

    return Response(response_validator.data)