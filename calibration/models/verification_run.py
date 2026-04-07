from django.db import models
from django.db.models import Q

from calibration.models import HindcastRun
from calibration.models.base_run import BaseRun
from calibration.models.forecast_run import ForecastRun


class VerificationRun(BaseRun):
    forecast_run = models.ForeignKey(ForecastRun, null=True, on_delete=models.CASCADE, db_index=True, related_name="verification_runs")
    hindcast_run = models.ForeignKey(HindcastRun, null=True, on_delete=models.CASCADE, db_index=True, related_name="verification_runs")

    class Meta:
        db_table = 'verification_run'
        indexes = [
            models.Index(fields=["forecast_run"], name="idx_verif_forecast_run"),
            models.Index(fields=["hindcast_run"], name="idx_verif_hindcast_run"),
            models.Index(fields=["status"], name="idx_verif_status"),
        ]
        constraints = [
            models.CheckConstraint(
                name="verif_exactly_one_parent_run",
                check=(
                        (Q(forecast_run__isnull=False) & Q(hindcast_run__isnull=True)) |
                        (Q(forecast_run__isnull=True) & Q(hindcast_run__isnull=False))
                ),
            ),
        ]

    @property
    def parent_run(self) -> ForecastRun | HindcastRun:
        if self.forecast_run_id is not None:
            return self.forecast_run
        if self.hindcast_run_id is not None:
            return self.hindcast_run
        raise ValueError("VerificationRun has no parent run set.")

    def __str__(self):
        if self.forecast_run_id is not None:
            parent = f"forecast_run_id: {self.forecast_run_id}"
        else:
            parent = f"hindcast_run_id: {self.hindcast_run_id}"

        return (
            f"VerificationRun {self.id}, "
            f"{parent}, "
            f"status.name: {self.status.name}"
        )
