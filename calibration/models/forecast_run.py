from django.db import models

from calibration.models.ForecastBaseRun import ForecastBaseRun


class ForecastRun(ForecastBaseRun):
    calibration_run = models.ForeignKey('CalibrationRun', null=False, related_name="forecasts_from_calibration", on_delete=models.CASCADE, db_index=True)
    cold_start_run = models.ForeignKey('ColdStartRun', null=True, related_name="forecasts_from_cold_start", on_delete=models.CASCADE, db_index=True)

    class Meta:
        db_table = 'forecast_run'
        indexes = [
            models.Index(fields=['calibration_run'], name='idx_forecast_calibration_run'),
            models.Index(fields=['cold_start_run'], name='idx_forecast_cold_start_run'),
            models.Index(fields=['status'], name='idx_forecast_status'),
            models.Index(fields=['cycle_date'], name='idx_forecast_cycle_date'),
            models.Index(fields=['calibration_run', 'status', '-id'], name='idx_fcst_run_status_id_desc')
        ]

    def __str__(self):
        return (
            f"ForecastRun {self.id}, "
            f"configuration: {self.configuration.name}, "
            f"Calibration Job {self.calibration_run.id}, "
            f"owner: {self.calibration_run.owner.username}, "  # type: ignore[attr-defined]  # Suppress PyCharm warning for unresolved attribute
            f"status.name: {self.status.name}"
        )
