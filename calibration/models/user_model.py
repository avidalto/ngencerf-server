from django.db import models
from django.contrib.auth.models import AbstractUser, BaseUserManager


class CustomUserManager(BaseUserManager):
    def create_user(self, email, username, password=None, **extra_fields):
        if not email:
            raise ValueError("Email field is required")
        email = self.normalize_email(email)
        user = self.model(email=email, username=username, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        if not extra_fields.get('is_staff'):
            raise ValueError("Superuser must have is_staff=True.")
        if not extra_fields.get('is_superuser'):
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(email=email, username=email, password=password, **extra_fields)


class CustomUser(AbstractUser):
    email = models.EmailField(
        unique=True,
        error_messages={
            'unique': "A user with this email already exists."
        }
    )
    username = models.CharField(max_length=255, blank=True, null=True)  # Make username optional
    email_verified = models.BooleanField(default=False)

    objects = CustomUserManager()

    class Meta:
        db_table = 'custom_user'

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []   # No other fields required for superuser

    def save(self, *args, **kwargs):
        # Always set the username to the email value
        self.username = self.email
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.email} - ({self.first_name} {self.last_name})"
