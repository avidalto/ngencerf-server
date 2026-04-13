from django.db import models

from calibration.models.forecast_base_run import ForecastBaseRun


class HindcastRun(ForecastBaseRun):
    calibration_run = models.ForeignKey('CalibrationRun', null=False, related_name="hindcasts_from_calibration", on_delete=models.CASCADE, db_index=True)
    cold_start_run = models.ForeignKey('ColdStartRun', null=True, related_name="hindcasts_from_cold_start", on_delete=models.CASCADE, db_index=True)
    created_new_cold_start = models.BooleanField(default=False)
    interval_cycle = models.IntegerField(null=False)
    num_iterations = models.IntegerField(null=False)

    class Meta:
        db_table = 'hindcast_run'
        indexes = [
            models.Index(fields=['calibration_run'], name='idx_hindcast_calibration_run'),
            models.Index(fields=['cold_start_run'], name='idx_hindcast_cold_start_run'),
            models.Index(fields=['status'], name='idx_hindcast_status'),
            models.Index(fields=['cycle_date'], name='idx_hindcast_cycle_date'),
            models.Index(fields=['calibration_run', 'status', '-id'], name='idx_hcst_run_status_id_desc')
        ]

    def __str__(self):
        return (
            f"HindcastRun {self.id}, "
            f"configuration: {self.configuration.name}, "
            f"interval_cycle: {self.interval_cycle}, "
            f"num_iterations: {self.num_iterations}, "
            f"Calibration Job {self.calibration_run.id}, "
            f"owner: {self.calibration_run.owner.username}, "  # type: ignore[attr-defined]  # Suppress PyCharm warning for unresolved attribute
            f"status.name: {self.status.name}"
        )
