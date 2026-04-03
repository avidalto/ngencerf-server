from django.db import models

from calibration.models.base_run import BaseRun


class ForecastBaseRun(BaseRun):
    configuration = models.ForeignKey("ForecastConfiguration", null=False, on_delete=models.RESTRICT)
    cycle_date = models.DateTimeField(null=False)

    class Meta:
        abstract = True
