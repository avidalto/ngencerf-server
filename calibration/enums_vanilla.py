from enum import StrEnum, Enum
from typing import Self, Any


# These enums are used from settings.py.  We need to avoid any references to the model

class ScriptEnum(StrEnum):
    CALIBRATION = "calibration"
    VALIDATION = "validation"
    VALIDATION_ITERATION = "validation_iteration"
    COLD_START = "cold_start"
    FORECAST = "forecast"
    HINDCAST = "hindcast"
    VERIFICATION = "verification"


class NgenEnvironmentEnum(StrEnum):
    LOCAL = "LOCAL"
    PARALLEL_WORKS = "PARALLEL_WORKS"
    DOCKER = "DOCKER"


class JobType(StrEnum):
    CALIBRATION = 'calibration'
    VALIDATION = 'validation'
    COLD_START = 'cold_start'
    FORECAST = 'forecast'
    HINDCAST = 'hindcast'
    VERIFICATION = 'verification'
    COMPARISON = 'comparison'


class SecondaryDataEnum(StrEnum):
    SWE = 'SWE'
    SOIL_MOISTURE = 'Soil Moisture'
    PRECIPITATION = 'Precipitation'


class _SortFieldMixin:
    value: tuple[str, Any]

    @property
    def orm_field(self):
        return self.value[1]

    @classmethod
    def from_name(cls, name: str) -> Self:
        try:
            return next(member for member in cls if member.value[0] == name)  # type: ignore[misc]
        except StopIteration as exc:
            raise ValueError(f"Invalid sort field: {name}") from exc

    @classmethod
    def get_names(cls) -> list[str]:
        return [member.value[0] for member in cls]  # type: ignore[misc]


class ForecastSortField(_SortFieldMixin, Enum):
    FORECAST_RUN_ID = ("forecast_run_id", "id")
    GAGE_ID = ("gage_id", "calibration_run__gage__gage_id")
    CALIBRATION_RUN_ID = ("calibration_run_id", "calibration_run__id")
    SUBMIT_DATE = ("submit_date", "submit_date")
    CREATED_AT = ("created_at", "created_at")
    CYCLE_DATE = ("cycle_date", "cycle_date")
    CONFIGURATION = ("configuration", "configuration__name")
    DOMAIN_NAME = ("domain_name", "calibration_run__gage__domain__name")
    FORECAST_STATUS = ("forecast_status", "status__name")
    COLD_START_DATE = ("cold_start_date", "cold_start_run__cold_start_date")
    COLD_START_STATUS = ("cold_start_status", "cold_start_run__status__name")
    COLD_START_SUBMIT_DATE = ("cold_start_submit_date", "cold_start_run__submit_date")


class HindcastSortField(_SortFieldMixin, Enum):
    HINDCAST_RUN_ID = ("hindcast_run_id", "id")
    GAGE_ID = ("gage_id", "calibration_run__gage__gage_id")
    CALIBRATION_RUN_ID = ("calibration_run_id", "calibration_run__id")
    SUBMIT_DATE = ("submit_date", "submit_date")
    CREATED_AT = ("created_at", "created_at")
    CYCLE_DATE = ("cycle_date", "cycle_date")
    CONFIGURATION = ("configuration", "configuration__name")
    DOMAIN_NAME = ("domain_name", "calibration_run__gage__domain__name")
    HINDCAST_STATUS = ("hindcast_status", "status__name")
    COLD_START_DATE = ("cold_start_date", "cold_start_run__cold_start_date")
    COLD_START_STATUS = ("cold_start_status", "cold_start_run__status__name")
    COLD_START_SUBMIT_DATE = ("cold_start_submit_date", "cold_start_run__submit_date")


# ────────────────────────────────────────────────────────────────────────────────
# NOTE:
# This class is duplicated in the CLI project under:
#   `cli/ngencerf/calibration_sort_fields.py`
#
# The duplication allows the standalone CLI to be built and executed independently
# of the full Django server environment (e.g., for distribution as a PyInstaller
# binary where Django and its dependencies are not available).
#
# The CLI build process includes a consistency check (`check_enum_consistency.py`)
# that verifies this definition remains identical between the server and CLI
# versions. If any field names differ, the build will fail.
# ────────────────────────────────────────────────────────────────────────────────
class CalibrationSortField(_SortFieldMixin, Enum):
    CALIBRATION_RUN_ID = ("calibration_run_id", "id")
    GAGE_ID = ("gage_id", "gage__gage_id")
    DOMAIN_NAME = ("domain_name", "gage__domain__name")
    JOB_NAME = ("job_name", "job_name")
    SUBMIT_DATE = ("submit_date", "submit_date")
    CREATED_AT = ("created_at", "created_at")
    LAST_UPDATED_ON = ("last_updated_on", "updated_at")
    JOB_GENESIS = ("job_genesis", "job_genesis")
    COMBINED_STATUS = ("status", "combined_status")
    OBJECTIVE_FUNCTION = ("objective_function", "objective_function__name")
    OPTIMIZATION_ALGORITHM = ("optimization_algorithm", "optimization__name")
    PERIOD = ("period", ["calibration_start_period", "calibration_end_period"])
    STOP_CRITERIA = ("stop_criteria", "calibrationstopcriteria__value")
    IS_ARCHIVED = ("is_archived", "is_archived")
    IS_LOCKED = ("is_locked", "is_locked")
    VALIDATION_RUNS = ("validation_runs", "validation_run_count")


class VerificationSortField(_SortFieldMixin, Enum):
    VERIFICATION_RUN_ID = ("verification_run_id", "id")
    FORECAST_RUN_ID = ("forecast_run_id", "forecast_run__id")
    STATUS = ("status", "status__name")
    SUBMIT_DATE = ("submit_date", "submit_date")
    CREATED_AT = ("created_at", "created_at")
