import logging

from django.contrib.auth import get_user_model
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from djoser.serializers import UserSerializer, UserCreateSerializer, SendEmailResetSerializer, PasswordResetConfirmRetypeSerializer
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from calibration.views.email_verification_views import send_initial_verification_email

logger = logging.getLogger(__name__)
User = get_user_model()


class CustomUserCreateSerializer(UserCreateSerializer):
    class Meta(UserCreateSerializer.Meta):
        model = User
        fields = ("id", "email", "first_name", "last_name", "password")
        extra_kwargs = {"password": {"write_only": True}}

    def create(self, validated_data):
        """
        Create a new user and send the initial email verification message.

        Unlike Djoser activation, users are created as active immediately.
        Email ownership is enforced separately through the email_verified flag.

        :param validated_data: Validated serializer data.
        :return: Newly created user.
        """
        # Automatically set username to email
        validated_data["username"] = validated_data["email"]

        user = super().create(validated_data)

        try:
            send_initial_verification_email(user)
        except Exception:
            logger.exception(
                "Failed to send initial verification email for user_id=%s email=%r",
                user.id,
                user.email,
            )

        return user


class CustomUserSerializer(UserSerializer):
    email_verified = serializers.BooleanField(read_only=True)

    class Meta(UserSerializer.Meta):
        model = User
        fields = ("id", "email", "first_name", "last_name", "email_verified")


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["email"] = user.email  # noqa
        token["email_verified"] = bool(getattr(user, "email_verified", False))
        return token

    def validate(self, attrs):
        # Snapshot incoming keys/values safely
        login_field = getattr(User, "USERNAME_FIELD", "username")
        raw_identifier = attrs.get(login_field)
        provided_keys = sorted(attrs.keys())

        # Never log password; only log whether it was supplied
        logger.info(
            "JWT login attempt: login_field=%s, provided_keys=%s, identifier=%r, password_supplied=%s",
            login_field,
            provided_keys,
            raw_identifier,
            bool(attrs.get("password")),
        )

        if not raw_identifier:
            logger.warning(
                "JWT login attempt missing identifier for login_field=%s; provided_keys=%s",
                login_field,
                provided_keys,
            )

        # Optional: pre-check user existence by identifier (safe)
        if raw_identifier:
            try:
                # case-insensitive check is usually what people expect for emails/usernames
                exists = User.objects.filter(**{f"{login_field}__iexact": raw_identifier}).exists()
                logger.debug(
                    "JWT login precheck: user_exists=%s for %s=%r",
                    exists,
                    login_field,
                    raw_identifier,
                )
            except Exception:
                logger.exception("JWT login precheck failed for %s=%r", login_field, raw_identifier)

        try:
            # data: [dict, Any] = super().validate(attrs)

            data = super().validate(attrs)

        except AuthenticationFailed as e:
            logger.warning(
                "JWT login failed: login_field=%s identifier=%r detail=%r",
                login_field,
                raw_identifier,
                getattr(e, "detail", None),
            )
            raise

        except serializers.ValidationError as e:
            # SimpleJWT uses ValidationError for bad credentials; Djoser may wrap it.
            logger.warning(
                "JWT login failed: login_field=%s identifier=%r detail=%r",
                login_field,
                raw_identifier,
                getattr(e, "detail", None),
            )
            raise

        except Exception:
            logger.exception(
                "JWT login failed with unexpected error: login_field=%s identifier=%r",
                login_field,
                raw_identifier,
            )
            raise

        # Success
        logger.info(
            "JWT login success: user_id=%s, %s=%r, is_active=%s, email_verified=%s, is_staff=%s",
            getattr(self.user, "id", None),
            login_field,
            getattr(self.user, login_field, None),
            getattr(self.user, "is_active", None),
            getattr(self.user, "email_verified", None),
            getattr(self.user, "is_staff", None),
        )

        data["first_name"] = self.user.first_name
        data["last_name"] = self.user.last_name

        data["email_verified"] = bool(getattr(self.user, "email_verified", False))  # type: ignore
        data["email"] = self.user.email

        return data


class VerifiedEmailResetSerializer(SendEmailResetSerializer):
    def get_user(self, is_active=True):
        """
        Return the user for password reset if the account exists.

        Djoser uses this serializer for:
            POST /auth/users/reset_password/

        We log the lookup result here because successful password reset is also
        treated as proof of email ownership in
        VerifiedPasswordResetConfirmRetypeSerializer.

        :param is_active: Whether to restrict lookup to active users.
        :return: Matching user instance, or None if not found.
        """
        submitted_email = self.data.get("email")

        logger.info(
            "Password reset requested: submitted_email=%r, is_active=%s",
            submitted_email,
            is_active,
        )

        user = super().get_user(is_active=is_active)

        logger.info(
            "Password reset lookup result: submitted_email=%r, user_found=%s, user_id=%s, verified=%s, active=%s",
            submitted_email,
            bool(user),
            getattr(user, "id", None),
            getattr(user, "email_verified", None),
            getattr(user, "is_active", None),
        )

        return user


class VerifiedPasswordResetConfirmRetypeSerializer(PasswordResetConfirmRetypeSerializer):
    def save(self):
        """
        Complete the password reset and treat a successful reset as proof that
        the user controls the email address on file.

        Djoser uses this serializer for:
            POST /auth/users/reset_password_confirm/

        This is the retype variant because:
            SET_PASSWORD_RETYPE = True

        After the parent serializer successfully validates the uid/token and
        updates the password, this marks the user as email_verified=True if not
        already verified.

        :return: Result from the parent serializer save().
        """
        result = super().save()

        uid = self.validated_data["uid"]
        user_id = force_str(urlsafe_base64_decode(uid))
        user = User.objects.get(pk=user_id)

        if not getattr(user, "email_verified", False):
            user.email_verified = True
            user.save(update_fields=["email_verified"])

        return result
