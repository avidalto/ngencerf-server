from django.conf import settings
from django.db import models

from calibration.models.base_model import BaseModel


class MFARecoveryCode(BaseModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mfa_recovery_codes")
    code_hash = models.CharField(max_length=128, null=False)
    used = models.BooleanField(default=False)

    class Meta:
        db_table = "mfa_recovery_code"

    def __str__(self):
        return (
            f"MFARecoveryCode: {self.id}, "
            f"user_id: {self.user_id}, "
            f"used: {self.used}"
        )
