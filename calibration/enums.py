from typing import Any, Type

from django.core.cache import cache

from calibration.models import Status, ForcingSource, ObservationalSource, Domain, Optimization, GeopackageSource, PlotDefinition, \
    ForecastConfiguration, Metric
from calibration.util.AbstractEnum import AbstractEnum
from calibration.views.cache_prefix import CACHE_PREFIX


class StatusEnum(AbstractEnum):
    """
    Enum for different statuses with caching support for efficient retrieval.
    """

    SAVED = 'Saved'
    READY = 'Ready'
    SUBMITTED = 'Submitted'
    RUNNING = 'Running'
    DONE = 'Done'
    CANCELLED = 'Cancelled'
    FAILED = 'Failed'
    SERVER_ERROR = 'Server error'

    @classmethod
    def get_model(cls) -> Type[Status]:
        return Status


class ForcingSourceEnum(AbstractEnum):
    """
    Enum for Forcing Sources
    """
    AORC = 'AORC'
    NWM_RETROSPECTIVE = 'NWM Retrospective'

    @classmethod
    def get_model(cls) -> Type[ForcingSource]:
        return ForcingSource

    @classmethod
    def get_filter(cls) -> dict[str, Any]:
        # Apply the filter to return only active elements
        return {'is_active': True}


class ObservationalSourceEnum(AbstractEnum):
    """
    Enum for Observational Sources
    """

    HISTORICAL = 'Historical'

    @classmethod
    def get_model(cls) -> Type[ObservationalSource]:
        return ObservationalSource

    @classmethod
    def get_filter(cls) -> dict[str, Any]:
        # Apply the filter to return only active elements
        return {'is_active': True}


class GeopackageSourceEnum(AbstractEnum):
    """
    Enum for Geopackage Sources
    """

    HYDROFABRIC = 'Hydrofabric'

    @classmethod
    def get_model(cls) -> Type[GeopackageSource]:
        return GeopackageSource

    @classmethod
    def get_filter(cls) -> dict[str, Any]:
        # Apply the filter to return only active elements
        return {'is_active': True}


class ForecastConfigEnum(AbstractEnum):
    """
    Enum for Forecast Cycles,
    """

    @classmethod
    def get_model(cls) -> Type[ForecastConfiguration]:
        return ForecastConfiguration

    @classmethod
    def get_filter(cls) -> dict[str, Any]:
        # Apply the filter to return only active elements
        return {'is_active': True}


class DomainEnum(AbstractEnum):
    """
    Domain Enum with database synchronization.
    """

    PUERTO_RICO = 'Puerto_Rico'
    CONUS = 'CONUS'

    @classmethod
    def get_aliases(cls):
        return {
            cls.PUERTO_RICO: ['Puerto Rico', 'Puerto_Rico']
        }

    @classmethod
    def get_model(cls) -> Type[Domain]:
        return Domain


class MetricEnum(AbstractEnum):
    """
    Metric Enum with database synchronization
    """

    @classmethod
    def get_model(cls) -> Type[Metric]:
        return Metric

    @classmethod
    def get_filter(cls) -> dict[str, Any]:
        # Apply the filter to return only active elements
        return {'is_active': True}


class OptimizationEnum(AbstractEnum):
    """
    Enum for Optimization types with prefetching for related inputs.
    """

    DDS = 'DDS'
    GWO = 'GWO'
    PSO = 'PSO'

    @classmethod
    def get_model(cls) -> Type[Optimization]:
        return Optimization

    @classmethod
    def load_items(cls) -> None:
        """
        Override: load Optimization rows and prefetch inputs so they are cached
        once per server run and used everywhere else without extra SELECTs.
        """
        cache_key = f"{CACHE_PREFIX}{cls.__name__}_cache"

        model = cls.get_model()
        filter_criteria = cls.get_filter() or {}

        items = model.objects.filter(**filter_criteria).prefetch_related('inputs')

        # Store the results in a dictionary with the item's name as the key
        item_dict = {item.name: item for item in items}

        cache.set(cache_key, item_dict, timeout=None)


class PlotDefinitionsEnum(AbstractEnum):
    """
    Enum for Plot Definitions.
    """

    HYDROGRAPH_EVOLUTION = 'Hydrograph evolution'
    OBJECTIVE_FUNCTION_EVOLUTION = 'Objective Function evolution'
    METRIC_EVOLUTION = 'Metric evolution'
    PARAMETER_EVOLUTION = 'Parameter evolution'
    SCATTERPLOT_STREAMFLOW = 'Scatterplot streamflow'
    METRICS_VS_OBJECTIVE_FUNCTION = 'Metrics vs Objective Function'
    STREAM_FLOW_PRECIPITATION = 'Stream Flow Precipitation'
    FLOW_DURATION_CURVES = 'Flow Duration Curves'
    COST_HISTORY = 'Cost History'
    BAR_CHART_METRICS = 'Bar Chart Metrics'
    FLOW_DURATION_CURVES_VALIDATION = 'Flow Duration Curves Validation'
    HYDROGRAPH_VALIDATION = 'Hydrograph Validation'
    STREAMFLOW_VALIDATION_PRECIPITATION = 'Streamflow Validation Precipitation'
    FORECAST_HYDROGRAPH = 'Forecast Hydrograph'
    CALIBRATION_METRICS = 'Calibration Metrics'

    @classmethod
    def get_model(cls) -> Type[PlotDefinition]:
        return PlotDefinition


# Below are simple enums without database synchronization or aliasing.

class DataTypeEnum(AbstractEnum):
    DOUBLE = 'double'
    FLOAT = 'float'
    INTEGER = 'integer'
    BOOLEAN = 'boolean'
    STRING = 'string'


class SlurmCallbackStatusEnum(AbstractEnum):
    DONE = 'DONE'
    FAILED = 'FAILED'
    CANCELED = 'CANCELED'
    STARTING = 'STARTING'


class LocationEnum(AbstractEnum):
    NODE = 'node'


class UnitsEnum(AbstractEnum):
    M = 'm'
    NONE = 'none'


class ValidationType(AbstractEnum):
    VALID_BEST = 'valid_best'
    VALID_CONTROL = 'valid_control'
    VALID_ITERATION = 'valid_iteration'


class LogCategory(AbstractEnum):
    GENERAL = 'general'
    CALIBRATION = 'calibration'
    VALIDATION = 'validation'
    FORECAST = 'forecast'
    HINDCAST = 'hindcast'
    COLD_START = 'cold start'
    VERIFICATION = 'verification'


# Used for both ValidationMetrics and NWMRetrospectiveMetrics
class ValidationMetricPeriod(AbstractEnum):
    calib = 'calib'
    valid = 'valid'
    full = 'full'


class JobGenesis(AbstractEnum):
    CLONE = 'clone'
    IMPORT = 'import'
    GUI = 'gui'


class GetValidationJobsScope(AbstractEnum):
    IDS = 'ids'
    STATUS = 'status'
    DETAILS = 'details'


class NgenLogging(AbstractEnum):
    DEBUG = 'debug'
    INFO = 'info'
    WARNING = 'warning'
    SEVERE = 'severe'
    FATAL = 'fatal'
