from django.core.validators import RegexValidator, MinValueValidator
from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail
from rest_framework.fields import empty
from rest_framework.settings import api_settings

from calibration.enums import DataTypeEnum, UnitsEnum, LocationEnum, ForcingSourceEnum, ObservationalSourceEnum, DomainEnum, StatusEnum, \
    OptimizationEnum, GeopackageSourceEnum, SlurmCallbackStatusEnum, JobGenesis, PlotDefinitionsEnum, ForecastConfigEnum, LogCategory, \
    NgenLogging
from calibration.enums_vanilla import CalibrationSortField, VerificationSortField, ForecastSortField, HindcastSortField
from calibration.util.caching import get_cached_modules_with_groups


class BaseSerializer(serializers.Serializer):
    def run_validation(self, data=None):
        if data is not None and data != empty:
            unknown = set(data) - set(self.fields)
            if unknown:
                errors = ["Unknown field: {}".format(f) for f in unknown]
                raise serializers.ValidationError({
                    api_settings.NON_FIELD_ERRORS_KEY: errors,
                })

        return super().run_validation(data)


def enum_validator(enum_class, *, allow_blank: bool = True):
    """
    Validates if the value is a valid name or alias of the enum class, case-insensitively.

    By default, blanks (None or empty/whitespace strings) are allowed so that optional
    fields can represent "no selection". Set allow_blank=False for contexts where blanks
    are not meaningful (e.g., list elements).

    NOTE:
    Valid names are loaded lazily on the first validation call and cached for the
    lifetime of the process. This avoids database access at import time (important
    during migrations) while still preventing repeated lookups during validation.
    """
    valid_names_lc: set[str] | None = None
    original_valid_names: list[str] | None = None

    def validate_enum(value):
        nonlocal valid_names_lc, original_valid_names

        # Skip validation for blanks (handled as no-op)
        if value is None or (isinstance(value, str) and value.strip() == ""):
            if allow_blank:
                return
            raise serializers.ValidationError("This field may not be blank.")

        if valid_names_lc is None or original_valid_names is None:
            if hasattr(enum_class, "get_all_valid_names"):
                loaded_names = list(enum_class.get_all_valid_names())
            elif hasattr(enum_class, "get_names"):
                loaded_names = list(enum_class.get_names())
            else:
                raise RuntimeError(
                    f"Enum class '{enum_class.__name__}' must define either "
                    f"'get_names()' or 'get_all_valid_names()'."
                )

            original_valid_names = loaded_names
            valid_names_lc = {str(name).lower() for name in loaded_names}

        valid_names = original_valid_names
        valid_names_lc_local = valid_names_lc

        if valid_names is None or valid_names_lc_local is None:
            raise RuntimeError("Enum validator failed to initialize valid names.")

        # Normalize to string for comparison (prevents .lower() crashes on non-str types)
        original_value = value
        value_lc = str(value).lower()

        if value_lc not in valid_names_lc_local:
            raise serializers.ValidationError(
                f"Invalid value '{original_value}'. This field must be one of {valid_names}."
            )

    return validate_enum


def no_space_validator(value):
    if ' ' in value:
        raise serializers.ValidationError("This field must not contain spaces.")


def greater_than_zero(value):
    if value <= 0:
        raise serializers.ValidationError("This field must be greater than 0.")


class ModuleNameField(serializers.CharField):
    """
    Validates a single module name against the cached active module set.

    - Trims whitespace
    - Validates case-sensitively (must match DB casing exactly)
    - Returns the trimmed value unchanged
    """

    default_error_messages = {
        "blank": "Module name must not be blank.",
        "invalid": "Invalid module name: '{value}'.",
    }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_blank", False)
        super().__init__(*args, **kwargs)

    @staticmethod
    def _valid_modules_cs() -> set[str]:
        return {m.name for m in get_cached_modules_with_groups().values()}

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        trimmed = value.strip()

        if not trimmed:
            self.fail("blank")

        if trimmed not in self._valid_modules_cs():
            self.fail("invalid", value=value)

        return trimmed


class EmptySerializer(BaseSerializer):
    pass


class GenericMessageResponseSerializer(BaseSerializer):
    message = serializers.CharField(required=True)


class CalibrationRunIdSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True, min_value=1)


class GenericMessageWithIdResponseSerializer(GenericMessageResponseSerializer, CalibrationRunIdSerializer):
    pass


class DataValidationResponseSerializer(GenericMessageResponseSerializer):
    data_validation_id = serializers.IntegerField(required=True, min_value=1)


class GenericMessageAndStatusResponseSerializer(GenericMessageResponseSerializer):
    message = serializers.CharField(required=True)
    status = serializers.CharField(validators=[enum_validator(StatusEnum, allow_blank=False)], required=True)


class GenericResponseSerializer(GenericMessageAndStatusResponseSerializer):
    calibration_run_id = serializers.IntegerField(required=True, min_value=1)


class GenericResponseSerializerWithValidator(GenericMessageAndStatusResponseSerializer):
    validation_run_id = serializers.IntegerField(required=False, min_value=1)


class ColdStartRunIdSerializer(BaseSerializer):
    cold_start_run_id = serializers.IntegerField(required=True, min_value=1)


class ForecastRunIdSerializer(BaseSerializer):
    forecast_run_id = serializers.IntegerField(required=True, min_value=1)


class HindcastRunIdSerializer(BaseSerializer):
    hindcast_run_id = serializers.IntegerField(required=True, min_value=1)


class VerificationRunIdSerializer(BaseSerializer):
    verification_run_id = serializers.IntegerField(required=True, min_value=1)


class DeleteForecastRunResponseSerializer(GenericMessageResponseSerializer):
    forecast_run_id = serializers.IntegerField(required=True, min_value=1)


class DeleteHindcastRunResponseSerializer(GenericMessageResponseSerializer):
    hindcast_run_id = serializers.IntegerField(required=True, min_value=1)


class CalibrationRunIdList(BaseSerializer):
    calibration_run_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=True)


class GetStatusForComparisonRequestSerializer(CalibrationRunIdList):
    pass


class ValidationRunIdSerializer(BaseSerializer):
    validation_run_id = serializers.IntegerField(required=True, min_value=1)


class CalibrationOrValidationRunIdSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    validation_run_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)

    def validate(self, data):
        calibration_run_id = data.get('calibration_run_id')
        validation_run_id = data.get('validation_run_id')

        # Ensure that only one of them is specified
        if bool(calibration_run_id) == bool(validation_run_id):  # Both are specified or both are None
            raise serializers.ValidationError(
                "You must specify either 'calibration_run_id' or 'validation_run_id', but not both."
            )

        return data


class ForecastOrHindcastSerializer(BaseSerializer):
    forecast_run_id = serializers.IntegerField(required=False, allow_null=False, min_value=1)
    hindcast_run_id = serializers.IntegerField(required=False, allow_null=False, min_value=1)

    def validate(self, data):
        forecast_run_id = data.get('forecast_run_id')
        hindcast_run_id = data.get('hindcast_run_id')

        # Ensure that only one of them is specified
        if bool(forecast_run_id) == bool(hindcast_run_id):  # Both are specified or both are None
            raise serializers.ValidationError(
                "You must specify either 'forecast_run_id' or 'hindcast_run_id', but not both."
            )

        return data


class CalibrationOrValidationOrColdStartOrForecastOrHindcastOrVerificationRunIdSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=False, allow_null=False, min_value=1)
    validation_run_id = serializers.IntegerField(required=False, allow_null=False, min_value=1)
    forecast_run_id = serializers.IntegerField(required=False, allow_null=False, min_value=1)
    hindcast_run_id = serializers.IntegerField(required=False, allow_null=False, min_value=1)
    cold_start_run_id = serializers.IntegerField(required=False, allow_null=False, min_value=1)
    verification_run_id = serializers.IntegerField(required=False, allow_null=False, min_value=1)

    def validate(self, data):
        """
        Ensure that only one of calibration_run_id, validation_run_id, cold_start_run_id,
        forecast_run_id, hindcast_run_id, or verification_run_id is specified.
        """
        calibration_run_id = data.get('calibration_run_id')
        validation_run_id = data.get('validation_run_id')
        cold_start_run_id = data.get('cold_start_run_id')
        forecast_run_id = data.get('forecast_run_id')
        hindcast_run_id = data.get('hindcast_run_id')
        verification_run_id = data.get('verification_run_id')

        # Collect the IDs that are specified (non-null and non-zero values)
        specified_ids = [
            id_value
            for id_value in [
                calibration_run_id,
                validation_run_id,
                cold_start_run_id,
                forecast_run_id,
                hindcast_run_id,
                verification_run_id,
            ]
            if id_value is not None
        ]

        # Check that exactly one ID is specified
        if len(specified_ids) != 1:
            raise serializers.ValidationError(
                "You must specify exactly one of 'calibration_run_id', 'validation_run_id', "
                "'cold_start_run_id', 'forecast_run_id', 'hindcast_run_id' or 'verification_run_id'."
            )

        return data


class GetStatusRequestSerializer(CalibrationOrValidationOrColdStartOrForecastOrHindcastOrVerificationRunIdSerializer):
    include_performance_metrics = serializers.BooleanField(required=False, default=False)


class CancelJobResponseSerializer(GenericMessageAndStatusResponseSerializer,
                                  CalibrationOrValidationOrColdStartOrForecastOrHindcastOrVerificationRunIdSerializer):
    def validate(self, data):
        # Call the parent validate method to include its logic
        return super().validate(data)


class CreateValidationRequestSerializer(CalibrationRunIdSerializer):
    iteration_id = serializers.IntegerField(required=True, min_value=1)


class LoggingConfigSerializer(BaseSerializer):
    logging_enabled = serializers.BooleanField(required=False, default=True)
    split_logs_by_module = serializers.BooleanField(required=False, default=False)
    modules = serializers.DictField(child=serializers.CharField(), default=dict)

    def validate_modules(self, value: dict) -> dict:
        """
        Lowercase all module names and validate:
        - Keys (module names) must match known modules (case-insensitive),
          or be the special case 'ngen' or 'forcing'
        - Values must be valid log levels from NgenLogging

        Returns a new dict with all lowercase keys.
        """
        validator = enum_validator(NgenLogging, allow_blank=False)

        valid_modules = {m.name.lower() for m in get_cached_modules_with_groups().values()}
        valid_modules.add('ngen')  # Special case
        valid_modules.add('forcing')  # Special case
        # From Oct 20 through Nov 4, 2025, we were using ngen-forcing.  We need to keep accepting the legacy name
        valid_modules.add('ngen-forcing')  # Special case

        errors = {}
        normalized = {}

        for module_name, log_level in value.items():
            lowered_name = module_name.lower()
            if lowered_name not in valid_modules:
                errors[module_name] = f"Invalid module name: '{module_name}'"
                continue

            try:
                validator(log_level)
                normalized[lowered_name] = log_level
            except serializers.ValidationError as e:
                # Keep message readable; e.detail can be a list/dict depending on source
                errors[module_name] = f"Invalid log level for module '{module_name}': {e}"

        if errors:
            raise serializers.ValidationError(errors)

        return normalized


class ForecastConfigurationSerializer(BaseSerializer):
    configuration_name = serializers.CharField(required=True, validators=[enum_validator(ForecastConfigEnum)])


class HindcastConfigurationSerializer(CalibrationRunIdSerializer):
    configuration_name = serializers.CharField(required=True, validators=[enum_validator(ForecastConfigEnum)])


class CreateColdStartRequestSerializer(CalibrationRunIdSerializer):
    configuration_name = serializers.CharField(required=True, validators=[enum_validator(ForecastConfigEnum)])
    cycle_date = serializers.DateTimeField(required=True, allow_null=False)
    cold_start_date = serializers.DateTimeField(required=False, allow_null=False)


class CreateForecastRequestSerializer(CalibrationRunIdSerializer):
    configuration_name = serializers.CharField(required=True, validators=[enum_validator(ForecastConfigEnum)])
    cycle_date = serializers.DateTimeField(required=True, allow_null=False)
    cold_start_date = serializers.DateTimeField(required=False, allow_null=True)
    logging_config = LoggingConfigSerializer(required=False)


class CreateHindcastRequestSerializer(CalibrationRunIdSerializer):
    configuration_name = serializers.CharField(required=True, validators=[enum_validator(ForecastConfigEnum)])
    cycle_date = serializers.DateTimeField(required=False, allow_null=True)
    interval_cycle = serializers.ChoiceField(choices=[1, 3, 6, 12, 18, 24], required=True)
    num_iterations = serializers.IntegerField(required=True, allow_null=False, validators=[MinValueValidator(1)])
    cold_start_cycle_date = serializers.DateTimeField(required=False, allow_null=True)
    cold_start_date = serializers.DateTimeField(required=False, allow_null=True)
    cold_start_run_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    logging_config = LoggingConfigSerializer(required=False)
    validate_only = serializers.BooleanField(default=False)


##################################
# Common serializers that need to be defined before usage
##################################
class SlothParameters(BaseSerializer):
    param_name = serializers.CharField(required=True, allow_blank=False)
    param_count = serializers.IntegerField(required=True)
    param_type = serializers.CharField(required=True, validators=[enum_validator(DataTypeEnum)])
    param_units = serializers.CharField(required=True, validators=[enum_validator(UnitsEnum)])
    param_location = serializers.CharField(required=True, validators=[enum_validator(LocationEnum)])
    param_value = serializers.FloatField(required=True)
    maps_to_module = ModuleNameField(required=True)

    maps_to_variable_name = serializers.CharField(required=True, allow_blank=False)


class TimeRangeSerializerAllowEmpty(BaseSerializer):
    start_time = serializers.DateTimeField(required=False, allow_null=True)
    end_time = serializers.DateTimeField(required=False, allow_null=True)


class CalibrationTimeControls(BaseSerializer):
    calibration_start_time = serializers.DateTimeField()
    calibration_end_time = serializers.DateTimeField()
    simulation_start_time = serializers.DateTimeField()
    simulation_end_time = serializers.DateTimeField()

    def __init__(self, *args, allow_empty=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_empty = allow_empty  # Explicitly define allow_empty attribute

        # If allow_empty is True, make the fields not required
        if allow_empty:
            self.fields['calibration_start_time'].required = False
            self.fields['calibration_end_time'].required = False
            self.fields['simulation_start_time'].required = False
            self.fields['simulation_end_time'].required = False
        else:
            self.fields['calibration_start_time'].required = True
            self.fields['calibration_end_time'].required = True
            self.fields['simulation_start_time'].required = True
            self.fields['simulation_end_time'].required = True


class ValidationTimeControls(BaseSerializer):
    validation_start_time = serializers.DateTimeField()
    validation_end_time = serializers.DateTimeField()
    simulation_start_time = serializers.DateTimeField()
    simulation_end_time = serializers.DateTimeField()

    def __init__(self, *args, allow_empty=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_empty = allow_empty  # Explicitly define allow_empty attribute

        # If allow_empty is True, make the fields not required
        if allow_empty:
            self.fields['validation_start_time'].required = False
            self.fields['validation_end_time'].required = False
            self.fields['simulation_start_time'].required = False
            self.fields['simulation_end_time'].required = False
        else:
            self.fields['validation_start_time'].required = True
            self.fields['validation_end_time'].required = True
            self.fields['simulation_start_time'].required = True
            self.fields['simulation_end_time'].required = True


class SaveTuningParametersSerializer(BaseSerializer):
    name = serializers.CharField(required=True, allow_blank=False)
    minimum = serializers.FloatField(required=True, allow_null=False)
    maximum = serializers.FloatField(required=True, allow_null=False)
    initial_value = serializers.FloatField(required=True, allow_null=False)
    module = ModuleNameField(required=True)

    def __init__(self, *args, allow_missing_bounds=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_missing_bounds = allow_missing_bounds

        # Adjust field requirements based on allow_missing_bounds
        if self.allow_missing_bounds:
            for field in ['minimum', 'maximum', 'initial_value']:
                self.fields[field].required, self.fields[field].allow_null = False, True

    def validate(self, data):
        # Only validate ranges if minimum, maximum, and initial_value are provided
        min_val = data.get('minimum')
        max_val = data.get('maximum')

        if min_val is not None and max_val is not None:
            if min_val > max_val:
                raise serializers.ValidationError(
                    f"Minimum ({min_val}) must be less than maximum ({max_val}) for parameter {data['name']} in module {data['module']}"
                )

        return data


class LoadTuningParametersSerializer(BaseSerializer):
    """
    This serializer is used when loading, so min, max and initial_value are not required
    """
    name = serializers.CharField(required=True, allow_blank=False)
    minimum = serializers.FloatField(required=False, allow_null=True)
    maximum = serializers.FloatField(required=False, allow_null=True)
    initial_value = serializers.FloatField(required=False, allow_null=True)
    data_type = serializers.CharField(required=True, validators=[enum_validator(DataTypeEnum)])
    description = serializers.CharField(required=True, allow_blank=False)
    user_selected_for_tuning = serializers.BooleanField(required=True)
    units = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class OptimizationInputsSerializer(BaseSerializer):
    name = serializers.CharField(required=True, allow_blank=False)
    value = serializers.FloatField(required=True)


class GageSerializer(BaseSerializer):
    gage_id = serializers.CharField(required=True, allow_blank=False)
    agency = serializers.CharField(required=True, allow_blank=False)
    station_name = serializers.CharField(required=True, allow_blank=False)
    latitude = serializers.FloatField(required=True, allow_null=True)
    longitude = serializers.FloatField(required=True, allow_null=True)
    altitude = serializers.FloatField(required=True, allow_null=True)


class EdsErrorsSerializer(BaseSerializer):
    name = serializers.CharField(required=True, allow_null=False)
    message = serializers.CharField(required=True, allow_null=False)
    status_code = serializers.IntegerField(required=True, allow_null=True)


# initial_value is a strings, since Data Services sometimes has some extra crap in there, like units
# We save them in the db as floats, so we'll have to sanitize them
class ModuleParametersSerializer(BaseSerializer):
    name = serializers.CharField(required=True, allow_blank=False)
    data_type = serializers.CharField(required=True, validators=[enum_validator(DataTypeEnum)])
    description = serializers.CharField(required=True, allow_blank=False)
    # TODO min and max really should not be null
    min = serializers.FloatField(required=True, allow_null=True)
    max = serializers.FloatField(required=True, allow_null=True)
    initial_value = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    units = serializers.CharField(required=False, allow_null=True, allow_blank=True)


# Used by LoadTuningParameters
class ModuleMetadataStaticSerializer(BaseSerializer):
    name = serializers.CharField(required=True, allow_blank=False)
    parameters = LoadTuningParametersSerializer(required=True, many=True)


##################################
# Landing page
##################################

class GetCalibrationJobsSummaryResponseSerializer(BaseSerializer):
    running_count = serializers.IntegerField()
    ready_count = serializers.IntegerField()
    saved_count = serializers.IntegerField()


class ValidationStatusSerializer(ValidationRunIdSerializer):
    validation_type = serializers.CharField(required=True)
    status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum, allow_blank=False)])


class CalibrationJobsResponseSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True, min_value=1)
    gage_id = serializers.CharField(required=True, allow_null=True)
    domain_name = serializers.CharField(required=True, allow_null=True)
    job_genesis = serializers.CharField(required=True, validators=[enum_validator(JobGenesis)])
    created_at = serializers.DateTimeField(required=True)
    last_updated_on = serializers.DateTimeField(required=True)
    status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum, allow_blank=False)])
    calibration_start_period = serializers.DateTimeField(required=False, allow_null=True)
    calibration_end_period = serializers.DateTimeField(required=False, allow_null=True)
    job_name = serializers.CharField(required=False, allow_null=True, validators=[no_space_validator])
    submit_date = serializers.DateTimeField(required=True, allow_null=True)
    objective_function = serializers.CharField(required=False, allow_null=True)
    optimization_algorithm = serializers.CharField(required=False, allow_null=True)
    validations = ValidationStatusSerializer(many=True, required=False)
    is_archived = serializers.BooleanField(required=True, allow_null=True)
    is_locked = serializers.BooleanField(required=True, allow_null=True)
    is_downloadable = serializers.BooleanField(required=True, allow_null=False)
    is_lstm = serializers.BooleanField(required=True, allow_null=False)
    stop_criteria = serializers.IntegerField(required=False, allow_null=True)
    modules = serializers.ListField(child=ModuleNameField(required=True), required=False, allow_empty=True)


class GetCalibrationJobsResponseSerializer(BaseSerializer):
    jobs = CalibrationJobsResponseSerializer(many=True, required=True)
    total_count = serializers.IntegerField(required=True)
    date_range = serializers.ListField(child=serializers.DateTimeField(required=True, allow_null=False), min_length=2, max_length=2, required=False)
    id_range = serializers.ListField(child=serializers.IntegerField(required=True, allow_null=False), min_length=2, max_length=2, required=False)


class GetCalibrationJobIDsResponseSerializer(BaseSerializer):
    jobs = serializers.ListField(child=serializers.IntegerField(), required=True, allow_empty=True)
    total_count = serializers.IntegerField(required=True)
    date_range = serializers.ListField(child=serializers.DateTimeField(required=True, allow_null=False), min_length=2, max_length=2, required=False, )
    id_range = serializers.ListField(child=serializers.IntegerField(required=True, allow_null=False), min_length=2, max_length=2, required=False)

    gages = serializers.ListField(child=serializers.CharField(), required=False, allow_empty=True)


class ValidationJobsParameter(BaseSerializer):
    name = serializers.CharField(required=True, allow_null=False, allow_blank=False)
    value = serializers.FloatField(required=True, allow_null=False)


class LoadCalibrationJobSerializer(CalibrationRunIdSerializer):
    include_gpkg_map = serializers.BooleanField(required=False, default=True)


class JobElement(GenericMessageResponseSerializer):
    calibration_run_id = serializers.IntegerField(required=True, min_value=1)
    success = serializers.BooleanField(required=True, allow_null=False)


class CalibrationRunListResponse(BaseSerializer):
    jobs = JobElement(many=True, required=True, allow_null=False)


class FooterResponseSerializer(BaseSerializer):
    ngenCerf_version = serializers.CharField(required=True)
    ngenCerf_date = serializers.CharField(required=True)
    ngenCerf_copyright = serializers.CharField(required=True)
    contact_email = serializers.CharField(required=True, allow_blank=True)


def validate_automatic_validation(value):
    if value is not True:
        raise serializers.ValidationError("automatic_validation must always be True.")
    return value


class LoadCalibrationRunResponseSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True, min_value=1)
    last_updated_on = serializers.DateTimeField(required=True)
    job_data_dir = serializers.CharField(required=True)
    submit_date = serializers.DateTimeField(required=True, allow_null=True)
    gage = GageSerializer(required=True, allow_null=True)
    forcing_source_requested = serializers.CharField(required=True, allow_null=True, validators=[enum_validator(ForcingSourceEnum)])
    forcing_source_actual = serializers.CharField(required=True, allow_null=True, validators=[enum_validator(ForcingSourceEnum)])
    observational_source = serializers.CharField(required=True, allow_null=True, validators=[enum_validator(ObservationalSourceEnum)])
    geopackage_source = serializers.CharField(required=True, allow_null=True, validators=[enum_validator(GeopackageSourceEnum)])
    geopackage_image_url = serializers.CharField(required=False)
    external_data_status = serializers.JSONField(required=False)
    modules = serializers.ListField(child=ModuleNameField(required=True), required=False, allow_empty=True)
    job_name = serializers.CharField(required=True, allow_null=True, allow_blank=False, validators=[no_space_validator])
    formulation_errors = serializers.JSONField(required=False)
    formulation_warnings = serializers.JSONField(required=False)
    parameters_selected = serializers.BooleanField(required=True)
    use_sloth = serializers.BooleanField(default=False)
    sloth_parameters = SlothParameters(many=True, default=[])
    automatic_validation = serializers.BooleanField(default=True, validators=[validate_automatic_validation])
    time_range = TimeRangeSerializerAllowEmpty(required=False)
    calibration_times = CalibrationTimeControls(required=False, allow_empty=True)
    validation_times = ValidationTimeControls(required=False, allow_empty=True)
    num_catchments = serializers.IntegerField(required=True, allow_null=True)
    logging_config = LoggingConfigSerializer(required=False)
    objective_function = serializers.CharField(required=True, allow_null=True)
    streamflow_threshold = serializers.FloatField(required=False, allow_null=True, validators=[greater_than_zero])
    peak_flow_threshold = serializers.FloatField(required=False, allow_null=True, validators=[greater_than_zero])
    optimization = serializers.CharField(allow_blank=False, required=True, allow_null=True, validators=[enum_validator(OptimizationEnum)])
    optimization_inputs = OptimizationInputsSerializer(many=True, default=[])
    save_plot_iteration_frequency = serializers.IntegerField(min_value=1, required=True, allow_null=True)
    save_output_iteration = serializers.BooleanField(required=True, allow_null=True)
    stop_criteria = serializers.IntegerField(required=True, allow_null=True, min_value=2)
    status = serializers.CharField(validators=[enum_validator(StatusEnum, allow_blank=False)], required=True)
    failure_messages = serializers.ListField(child=serializers.DictField(), required=False)


class GitInfoSerializer(BaseSerializer):
    release = serializers.CharField(required=False, allow_blank=True)
    build_date = serializers.DateTimeField(required=False, input_formats=['%Y-%m-%d %H:%M:%S %Z'])
    commit_hash = serializers.CharField(required=True)
    commit_date = serializers.DateTimeField(required=False, input_formats=['%Y-%m-%d %H:%M:%S %Z'])
    author = serializers.CharField(required=False)
    message = serializers.CharField(required=False)
    modules = serializers.ListField(child=serializers.DictField(), required=False)

    @staticmethod
    def validate_modules(modules):
        """
        Ensure that each module entry is a dict with exactly one key-value pair,
        and validate its value using GitInfoSerializer.
        """
        validated_modules = []
        for module in modules:
            if not isinstance(module, dict):
                raise serializers.ValidationError("Each module must be an object.")
            if len(module) != 1:
                raise serializers.ValidationError("Each module must have exactly one key.")
            # Get the single key and its associated value
            module_name, module_data = list(module.items())[0]
            # Validate the module_data using this serializer recursively
            serializer = GitInfoSerializer(data=module_data)
            serializer.is_valid(raise_exception=True)
            validated_modules.append({module_name: serializer.validated_data})
        return validated_modules


class GetGitInfoResponseSerializer(BaseSerializer):
    server_start = serializers.DateTimeField(required=False)
    server_uptime = serializers.DurationField(required=False)
    git_info = serializers.DictField(child=GitInfoSerializer())


class ArchiveJobRequestSerializer(CalibrationRunIdList):
    archive = serializers.BooleanField(default=True, allow_null=False, required=False)


class LockJobRequestSerializer(CalibrationRunIdList):
    lock = serializers.BooleanField(default=True, allow_null=False, required=False)


class ModuleFilterSerializer(serializers.Serializer):
    """Filter by one or more module names with logical operator ('and' | 'or')."""
    operator = serializers.ChoiceField(choices=['or', 'and'], required=False, default='and')
    modules = serializers.ListField(child=ModuleNameField(required=True), required=False, allow_empty=True)


class DateFilterSerializer(serializers.Serializer):
    """Filter by created_at using 'before', 'after' or 'between' logic."""
    operator = serializers.ChoiceField(choices=['before', 'after', 'between'], required=False, allow_blank=True)
    create_date = serializers.DateTimeField(required=False, allow_null=True)
    start_date = serializers.DateTimeField(required=False, allow_null=True)
    end_date = serializers.DateTimeField(required=False, allow_null=True)

    def validate(self, attrs):
        operator = (attrs.get("operator") or "").lower()

        if operator in ("before", "after"):
            # require ONLY create_date
            if not attrs.get("create_date"):
                raise serializers.ValidationError(
                    "create_date is required when operator is 'before' or 'after'."
                )
            # forbid range fields
            if attrs.get("start_date") or attrs.get("end_date"):
                raise serializers.ValidationError(
                    "start_date and end_date are not allowed when operator is 'before' or 'after'."
                )

        elif operator == "between":
            # require BOTH start and end
            if not attrs.get("start_date") or not attrs.get("end_date"):
                raise serializers.ValidationError(
                    "start_date and end_date are required when operator is 'between'."
                )
            # forbid create_date
            if attrs.get("create_date"):
                raise serializers.ValidationError(
                    "create_date is not allowed when operator is 'between'."
                )

        return attrs


class IdFilterSerializer(serializers.Serializer):
    """Filter by id using 'before', 'after' or 'between' logic."""
    operator = serializers.ChoiceField(choices=['before', 'after', 'between'], required=False, allow_blank=True)
    id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    start_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    end_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)

    def validate(self, attrs):
        operator = (attrs.get("operator") or "").lower()

        if operator in ("before", "after"):
            if attrs.get("id") is None:
                raise serializers.ValidationError(
                    "id is required when operator is 'before' or 'after'."
                )
            if attrs.get("start_id") is not None or attrs.get("end_id") is not None:
                raise serializers.ValidationError(
                    "start_id and end_id are not allowed when operator is 'before' or 'after'."
                )

        elif operator == "between":
            if attrs.get("start_id") is None or attrs.get("end_id") is None:
                raise serializers.ValidationError(
                    "start_id and end_id are required when operator is 'between'."
                )
            if attrs.get("id") is not None:
                raise serializers.ValidationError(
                    "id is not allowed when operator is 'between'."
                )

        return attrs


class FilterSerializer(BaseSerializer):
    gage_id = serializers.CharField(required=False, allow_blank=True)
    domain_name = serializers.CharField(required=False, allow_blank=True, validators=[enum_validator(DomainEnum)])
    status = serializers.ListField(child=serializers.CharField(validators=[enum_validator(StatusEnum, allow_blank=False)]), required=False,
                                   allow_empty=True)
    module_filter = ModuleFilterSerializer(required=False)
    date_filter = DateFilterSerializer(required=False)
    id_filter = IdFilterSerializer(required=False)
    include_archived = serializers.BooleanField(default=False, required=False)


class SortSerializer(BaseSerializer):
    direction = serializers.ChoiceField(choices=['asc', 'desc'], required=False, default='asc')


class CalibrationSortSerializer(SortSerializer):
    field = serializers.CharField(required=False, allow_blank=False, validators=[enum_validator(CalibrationSortField, allow_blank=False)])


class ForecastSortSerializer(SortSerializer):
    field = serializers.CharField(required=False, allow_blank=False, validators=[enum_validator(ForecastSortField, allow_blank=False)])


class HindcastSortSerializer(SortSerializer):
    field = serializers.CharField(required=False, allow_blank=False, validators=[enum_validator(HindcastSortField, allow_blank=False)])


class VerificationSortSerializer(SortSerializer):
    field = serializers.CharField(required=False, allow_blank=False, validators=[enum_validator(VerificationSortField, allow_blank=False)])


class GetGagesRequestSerializer(BaseSerializer):
    domain_name = serializers.CharField(required=False, allow_blank=True, validators=[enum_validator(DomainEnum)])
    include_archived = serializers.BooleanField(default=False, required=False)


class GetGagesResponseSerializer(BaseSerializer):
    gages = serializers.ListField(child=serializers.CharField(required=True), required=True, allow_empty=True)


class PaginationSerializer(BaseSerializer):
    limit = serializers.IntegerField(required=False, min_value=1, max_value=500)
    offset = serializers.IntegerField(required=False, min_value=0, default=0)
    filters = FilterSerializer(required=False, allow_null=True)


class CalibrationPaginationSerializer(PaginationSerializer):
    sort = CalibrationSortSerializer(required=False, allow_null=True)
    ids_only = serializers.BooleanField(required=False, default=False)
    include_modules = serializers.BooleanField(required=False, default=False)


class ForecastPaginationSerializer(PaginationSerializer):
    sort = ForecastSortSerializer(required=False, allow_null=True)


class HindcastPaginationSerializer(PaginationSerializer):
    sort = HindcastSortSerializer(required=False, allow_null=True)


class VerificationPaginationSerializer(PaginationSerializer):
    sort = VerificationSortSerializer(required=False, allow_null=True)


##################################
# Gage Tab
##################################

class GageIdSerializer(BaseSerializer):
    gage_id = serializers.CharField(required=True, allow_blank=False)


class GetValidationJobsRequestSerializer(ValidationRunIdSerializer):
    include_validations = serializers.BooleanField(required=False, default=False)


class SaveGageRequestSerializer(CalibrationRunIdSerializer):
    gage_id = serializers.CharField(required=False, allow_blank=False)
    job_name = serializers.CharField(required=False, allow_blank=False, validators=[no_space_validator])
    forcing_source_requested = serializers.CharField(required=False, allow_null=True, validators=[enum_validator(ForcingSourceEnum)])
    observational_source = serializers.CharField(required=False, allow_null=True, validators=[enum_validator(ObservationalSourceEnum)])
    geopackage_source = serializers.CharField(required=False, allow_null=True, validators=[enum_validator(GeopackageSourceEnum)])


class SaveGageResponseSerializer(GenericResponseSerializer):
    geopackage_image_url = serializers.CharField(required=False, allow_null=True)
    eds_errors = EdsErrorsSerializer(many=True, required=False)
    warnings = serializers.ListField(required=False, child=serializers.CharField(required=True))
    num_catchments = serializers.IntegerField(required=True, allow_null=True)
    forcing_source_requested = serializers.CharField(required=False, allow_null=True, validators=[enum_validator(ForcingSourceEnum)])
    forcing_source_actual = serializers.CharField(required=False, allow_null=True, validators=[enum_validator(ForcingSourceEnum)])


class DomainResponseSerializer(BaseSerializer):
    name = serializers.CharField(required=True, validators=[enum_validator(DomainEnum, allow_blank=False)])
    display_name = serializers.CharField(required=True)
    description = serializers.CharField(required=True, allow_blank=False)


class ForcingSourceSerializer(BaseSerializer):
    name = serializers.CharField(required=True, validators=[enum_validator(ForcingSourceEnum)])
    description = serializers.CharField(required=True)


class ObservationalSourceSerializer(BaseSerializer):
    name = serializers.CharField(required=True, validators=[enum_validator(ObservationalSourceEnum)])
    description = serializers.CharField(required=True)


class GeopackageSourceSerializer(BaseSerializer):
    name = serializers.CharField(required=True, validators=[enum_validator(GeopackageSourceEnum)])
    description = serializers.CharField(required=True)


class FastGagesSerializer(serializers.Field):
    """
    Fast validation for huge gage lists:
    - ensures it's a list
    - ensures each element is a dict
    - DOES NOT deeply validate fields
    """

    def to_internal_value(self, data):
        if not isinstance(data, list):
            raise serializers.ValidationError("gages must be a list")

        # Light validation: each item must be a dict
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                raise serializers.ValidationError(f"gages[{i}] must be an object")

        return data  # return untouched

    def to_representation(self, value):
        return value  # no transformation


class LoadGageResponseSerializer(BaseSerializer):
    forcing_source_values = ForcingSourceSerializer(many=True)
    observational_source_values = ObservationalSourceSerializer(many=True)
    geopackage_source_values = GeopackageSourceSerializer(many=True)
    gages = FastGagesSerializer(required=True)
    gage = GageSerializer(required=False)
    domain_values = DomainResponseSerializer(many=True)


class UpdateGageStatusRequestSerializer(BaseSerializer):
    gage_id = serializers.CharField(required=True)
    is_active = serializers.BooleanField(required=False)


class UpdateGageStatusResponseSerializer(GenericMessageResponseSerializer):
    gage_id = serializers.CharField(required=True)
    is_active = serializers.BooleanField(required=True)


class CreateCalibrationRunResponseSerializer(GenericMessageResponseSerializer):
    calibration_run_id = serializers.IntegerField(required=True, min_value=1)
    job_data_dir = serializers.CharField(required=True)


class CreateAndRunValidationResponseSerializer(GenericResponseSerializer, ValidationRunIdSerializer):
    submit_date = serializers.DateTimeField(required=True, allow_null=False)


class CreateAndRunColdStartResponseSerializer(CalibrationRunIdSerializer, ColdStartRunIdSerializer):
    message = serializers.CharField(required=True)
    submit_date = serializers.DateTimeField(required=True, allow_null=False)
    cold_start_status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum, allow_blank=False)])


class CreateAndRunForecastResponseSerializer(CalibrationRunIdSerializer, ForecastRunIdSerializer):
    message = serializers.CharField(required=True)
    cold_start_run_id = serializers.IntegerField(required=True, allow_null=True, min_value=1)
    submit_date = serializers.DateTimeField(required=True, allow_null=False)


class CreateAndRunHindcastResponseSerializer(CalibrationRunIdSerializer, HindcastRunIdSerializer, ColdStartRunIdSerializer):
    message = serializers.CharField(required=True)
    submit_date = serializers.DateTimeField(required=True, allow_null=False)


class CreateAndValidateHindcastResponseSerializer(CalibrationRunIdSerializer):
    message = serializers.CharField(required=True)
    cold_start_run_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)


# Geopackage from Data Services
class GeopackageSerializer(BaseSerializer):
    uri = serializers.CharField(required=True, allow_blank=False)
    creation_date = serializers.DateTimeField(required=True)


##################################
# Plot Definitions Tab
##################################

class PlotListStaticSerializer(BaseSerializer):
    name = serializers.CharField(required=True, allow_blank=False, allow_null=False)
    display_name = serializers.CharField(required=True, allow_blank=False, allow_null=False)
    description = serializers.CharField(required=True, allow_blank=False, allow_null=False)
    timeseries_available = serializers.BooleanField(required=True, allow_null=False)


class GetPlotNamesResponseSerializer(CalibrationOrValidationRunIdSerializer):
    plot_names = PlotListStaticSerializer(many=True)
    status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum, allow_blank=False)])


class GetPlotNamesForComparisonResponseSerializer(BaseSerializer):
    plot_names = PlotListStaticSerializer(many=True)


class GetPlotRequestSerializer(CalibrationOrValidationRunIdSerializer):
    plot_name = serializers.CharField(required=True, allow_null=False, validators=[enum_validator(PlotDefinitionsEnum)])
    include_data = serializers.BooleanField(required=False, default=False)
    force_include_plot = serializers.BooleanField(required=False, default=False)
    start = serializers.IntegerField(required=False, default=0, min_value=0)
    limit = serializers.IntegerField(required=False, default=100, min_value=1)


class GetPlotsForComparisonRequestSerializer(CalibrationRunIdList):
    plot_name = serializers.CharField(required=True, allow_null=False, validators=[enum_validator(PlotDefinitionsEnum)])
    gage_id = serializers.CharField(required=True)
    start = serializers.IntegerField(required=False, default=0, min_value=0)
    limit = serializers.IntegerField(required=False, default=100, min_value=1)


class PaginationMetadataSerializer(BaseSerializer):
    start = serializers.IntegerField(required=True)
    limit = serializers.IntegerField(required=True)
    count = serializers.IntegerField(required=True)


class GetPlotResponseSerializer(CalibrationRunIdSerializer):
    validation_run_id = serializers.IntegerField(required=False, min_value=1)
    forecast_run_id = serializers.IntegerField(required=False, min_value=1)
    plot_name = serializers.CharField(required=True, allow_null=False)
    plot_file_path = serializers.CharField(required=False, allow_null=False)
    plot_url = serializers.CharField(required=False, allow_null=False)
    plot_data = serializers.JSONField(required=False)
    pagination_metadata = PaginationMetadataSerializer(required=False)


class ForecastRunDataResponseSerializer(ForecastOrHindcastSerializer):
    timeseries_data = serializers.JSONField(required=True)


class GetPlotErrorResponseSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True, min_value=1)
    message = serializers.CharField(required=True)


class GetPlotForComparisonResponseSerializer(GetPlotResponseSerializer):
    calibration_run_id = serializers.IntegerField(required=False, min_value=1)


class GetPlotsForComparisonResponseSerializer(CalibrationRunIdList):
    plots = GetPlotForComparisonResponseSerializer(many=True, required=False)
    errors = GetPlotErrorResponseSerializer(many=True, required=False)


##################################
# Formulation Tab
##################################


class S3UriField(serializers.CharField):
    def __init__(self, validate_directory=False, **kwargs):
        regex = r'^s3://([^/]+)/(.*?([^/]+)/)$' if validate_directory else r'^s3://([^/]+)/(.*?([^/]+))$'
        self.default_validators = [RegexValidator(regex, 'This field must be a valid S3 URI')]
        super().__init__(**kwargs)


class S3DirectoryValidator(BaseSerializer):
    uri = S3UriField(validate_directory=True)


# TODO Might be able to get rid of this soon
class S3FileValidator(BaseSerializer):
    # TODO We need to allow_null due to EDS error handling.  Need to get EDS to change their data when an error is returned
    uri = S3UriField(allow_null=True)


class ValidateFormulationRequestSerializer(CalibrationRunIdSerializer):
    modules = serializers.ListField(child=ModuleNameField(required=True), required=False, allow_empty=True, default=list)


class ModulePropertiesSerializer(BaseSerializer):
    module = ModuleNameField(required=True)
    property_name = serializers.CharField(required=True, allow_blank=False)
    property_value = serializers.CharField(required=True, allow_blank=False)

    def validate_property_name(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("property_name must not be blank.")
        return value

    def validate_property_value(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("property_value must not be blank.")
        return value


# Used by SaveFormulationRequestSerializer and ImportDataSerializer
def validate_module_properties_against_modules(
        modules: list[str] | set[str],
        props: list[dict[str, str]],
) -> None:
    module_names = set(modules or [])
    errors: list[str] = []

    if props and not module_names:
        errors.append("module_properties cannot be specified unless modules is non-empty")

    # membership check
    for i, p in enumerate(props):
        mod = p["module"]
        if mod not in module_names:
            errors.append(f"[{i}] module '{mod}' is not included in modules")

    # uniqueness check
    seen: set[tuple[str, str]] = set()
    for i, p in enumerate(props):
        key = (p["module"], p["property_name"])
        if key in seen:
            errors.append(f"[{i}] duplicate property for module '{key[0]}' and property '{key[1]}'")
        seen.add(key)

    if errors:
        raise serializers.ValidationError({"module_properties": errors})


class SaveFormulationRequestSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True, min_value=1)
    modules = serializers.ListField(child=ModuleNameField(required=True), required=False, allow_empty=True, default=list)
    use_sloth = serializers.BooleanField(required=True)
    sloth_parameters = SlothParameters(required=False, many=True)
    module_properties = ModulePropertiesSerializer(many=True, required=False, default=list)

    def validate(self, attrs: dict) -> dict:
        attrs = super().validate(attrs)

        validate_module_properties_against_modules(
            modules=attrs.get("modules") or [],
            props=attrs.get("module_properties") or [],
        )

        return attrs


class ModulePropertyChoiceResponseSerializer(BaseSerializer):
    value = serializers.CharField(required=True, allow_blank=False)  # always string on the wire
    label = serializers.CharField(required=True, allow_blank=False)
    description = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class ModulePropertyResponseSerializer(BaseSerializer):
    name = serializers.CharField(required=True, allow_blank=False)
    display_name = serializers.CharField(required=True)
    description = serializers.CharField(required=True)
    data_type = serializers.CharField(required=True, validators=[enum_validator(DataTypeEnum)])
    value = serializers.CharField(required=True)
    choices = ModulePropertyChoiceResponseSerializer(many=True, required=False)


class ModulePropertiesByModuleResponseSerializer(BaseSerializer):
    name = ModuleNameField(required=True)
    properties = ModulePropertyResponseSerializer(many=True, required=True, allow_empty=True)


class ModulePropertiesResponseSerializer(BaseSerializer):
    modules = ModulePropertiesByModuleResponseSerializer(many=True, required=True, allow_empty=True)


class ValidateFormulationResponseSerializer(BaseSerializer):
    formulation_errors = serializers.JSONField(required=False)
    formulation_warnings = serializers.JSONField(required=False)
    formulation_messages = serializers.JSONField(required=False)
    module_properties = ModulePropertiesResponseSerializer(required=False)


class SaveFormulationResponseSerializer(GenericResponseSerializer):
    formulation_errors = serializers.JSONField(required=False)
    formulation_warnings = serializers.JSONField(required=False)
    formulation_messages = serializers.JSONField(required=False)
    eds_errors = EdsErrorsSerializer(many=True, required=False)


class ModuleStaticSerializer(BaseSerializer):
    name = ModuleNameField(required=True)
    display_name = serializers.CharField(required=True, allow_blank=False)
    description = serializers.CharField(required=True, allow_blank=False)
    groups = serializers.ListField(child=serializers.CharField(required=True))
    is_active = serializers.BooleanField(required=True)


class GetModulesResponseSerializer(BaseSerializer):
    modules = ModuleStaticSerializer(many=True)
    module_groups = serializers.ListField(child=serializers.CharField(required=True), required=True, allow_empty=False)


##################################
# Tuning Tab
##################################
class ValidateParametersResponseSerializer(BaseSerializer):
    parameter_errors = serializers.JSONField(required=False)
    parameter_warnings = serializers.JSONField(required=False)


class UploadUserParameterFile(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True, min_value=1)
    user_parameter_file = serializers.FileField(required=True)

    def validate_user_parameter_file(self, value):
        request = self.context.get('request')
        files = request.FILES.getlist('user_parameter_file')
        if len(files) != 1:
            raise serializers.ValidationError("Only one parameter file should be uploaded.")
        return value


class ParameterFileSerializer(BaseSerializer):
    param = serializers.CharField(required=True)
    min = serializers.FloatField(required=True)
    max = serializers.FloatField(required=True)
    init = serializers.FloatField(required=True)
    model = serializers.CharField(required=True)


class UserParameterFileUploadResponse(BaseSerializer):
    message = serializers.CharField(required=True)
    calibration_run_id = serializers.IntegerField(required=True, min_value=1)
    user_parameter_file = ParameterFileSerializer(many=True, required=True)


# Module object from Data Services containing module parameters and output variables
class ModuleMetadataSerializer(BaseSerializer):
    module_name = serializers.CharField(required=True, allow_blank=False)
    error = serializers.CharField(required=False, allow_blank=False)

    calibratable_parameters = ModuleParametersSerializer(many=True, required=False, allow_empty=True)

    def validate(self, data):
        has_error = bool(data.get("error"))
        has_params = "calibratable_parameters" in data

        # If Data Services returns an error for a module, it may omit calibratable_parameters.
        if has_error:
            return data

        # If no error, calibratable_parameters must be present.
        if not has_params:
            raise serializers.ValidationError({
                "calibratable_parameters": "This field is required unless 'error' is provided."
            })

        return data


# List of module objects from Data Services containing module parameters and output variables
class ModuleDataListSerializer(BaseSerializer):
    modules = ModuleMetadataSerializer(many=True, required=True)

    def validate_modules(self, value):
        if len(value) < 1:
            raise serializers.ValidationError("This list may not be empty.")
        return value


class SaveTuningRequestSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True, min_value=1)
    parameters = SaveTuningParametersSerializer(many=True, required=False)
    calibration_times = CalibrationTimeControls(required=False, allow_empty=False)
    validation_times = ValidationTimeControls(required=False, allow_empty=False)
    automatic_validation = serializers.BooleanField(default=True, validators=[validate_automatic_validation])


class SaveTuningResponseSerializer(GenericResponseSerializer):
    parameter_errors = serializers.JSONField(required=False)
    parameter_warnings = serializers.JSONField(required=False)


class LoadTuningResponseSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True, min_value=1)
    modules = ModuleMetadataStaticSerializer(many=True, required=False)
    time_range = TimeRangeSerializerAllowEmpty(required=True)
    calibration_times = CalibrationTimeControls(required=False, allow_empty=True)
    validation_times = ValidationTimeControls(required=False, allow_empty=True)
    status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum, allow_blank=False)])


##################################
# Optimization Tab
##################################


class SaveOptimizationRequestSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=True, min_value=1)
    optimization_inputs = OptimizationInputsSerializer(many=True, required=False)
    optimization = serializers.CharField(allow_blank=False, required=False, validators=[enum_validator(OptimizationEnum)])
    objective_function = serializers.CharField(allow_blank=False, required=False)
    streamflow_threshold = serializers.FloatField(required=False, validators=[greater_than_zero])
    peak_flow_threshold = serializers.FloatField(required=False, validators=[greater_than_zero])
    stop_criteria = serializers.IntegerField(required=False, min_value=2)
    save_plot_iteration_frequency = serializers.IntegerField(min_value=1, required=False)
    save_output_iteration = serializers.BooleanField(required=False)


class OptimizationInputStaticSerializer(serializers.Serializer):
    name = serializers.CharField(required=True)
    description = serializers.CharField(required=True)
    data_type = serializers.CharField(required=True, validators=[enum_validator(DataTypeEnum)])
    default_value = serializers.FloatField(required=True)
    min = serializers.FloatField(required=False, allow_null=True)
    max = serializers.FloatField(required=False, allow_null=True)
    is_active = serializers.BooleanField(required=True)


class OptimizationInputsUserSerializer(serializers.Serializer):
    name = serializers.CharField(required=True)
    value = serializers.FloatField(required=True)


class MetricSerializer(serializers.Serializer):
    name = serializers.CharField()
    display_name = serializers.CharField()
    categorical = serializers.BooleanField()
    event_based = serializers.BooleanField()


class OptimizationStaticSerializer(serializers.Serializer):
    name = serializers.CharField()
    description = serializers.CharField()
    is_active = serializers.BooleanField()
    inputs = OptimizationInputStaticSerializer(many=True)


class LoadOptimizationResponseSerializer(serializers.Serializer):
    metrics = MetricSerializer(many=True)
    optimizations = OptimizationStaticSerializer(many=True)


##################################
# Run Tab
##################################

class PerformanceMetricsSerializer(BaseSerializer):
    run_time = serializers.DurationField(required=True, allow_null=True)
    num_cpus = serializers.IntegerField(required=True, allow_null=True)
    cpu_time = serializers.DurationField(required=True, allow_null=True)
    max_rss = serializers.CharField(required=True, allow_null=True)
    max_disk_read = serializers.CharField(required=True, allow_null=True)
    max_disk_write = serializers.CharField(required=True, allow_null=True)
    reserved_time = serializers.DurationField(required=False, allow_null=True)
    io_throughput = serializers.CharField(required=False, allow_null=True)


class CommonStatusFieldsMixin(serializers.Serializer):
    calibration_run_id = serializers.IntegerField(required=False, min_value=1)
    job_name = serializers.CharField(required=False)
    status = serializers.CharField(validators=[enum_validator(StatusEnum, allow_blank=False)], required=True)
    failure_messages = serializers.ListField(child=serializers.DictField(), required=False)
    submit_date = serializers.DateTimeField(required=False, allow_null=True)
    sent_date = serializers.DateTimeField(required=False, allow_null=True)
    run_start = serializers.DateTimeField(required=False, allow_null=True)
    run_end = serializers.DateTimeField(required=False, allow_null=True)
    elapsed_time = serializers.DurationField(required=False, allow_null=True)
    performance_metrics = PerformanceMetricsSerializer(required=False)


class GetStatusForValidationResponseSerializer(CommonStatusFieldsMixin, ValidationRunIdSerializer):
    message = serializers.CharField(required=False)
    validation_type = serializers.CharField(required=True)
    iteration_num = serializers.IntegerField(allow_null=True)


class GetStatusColdStartSerializer(BaseSerializer):
    cold_start_run_id = serializers.IntegerField(required=True, min_value=1)
    cold_start_date = serializers.DateTimeField(required=True)
    status = serializers.CharField(required=True)
    submit_date = serializers.DateTimeField(required=False, allow_null=True)
    sent_date = serializers.DateTimeField(required=False, allow_null=True)
    run_start = serializers.DateTimeField(required=False, allow_null=True)
    run_end = serializers.DateTimeField(required=False, allow_null=True)
    elapsed_time = serializers.DurationField(required=False, allow_null=True)
    failure_messages = serializers.ListField(child=serializers.DictField(), required=False)
    performance_metrics = PerformanceMetricsSerializer(required=False)


class GetStatusForForecastResponseSerializer(CommonStatusFieldsMixin, ForecastRunIdSerializer, CalibrationRunIdSerializer):
    message = serializers.CharField(required=False)
    configuration = serializers.CharField(required=True, validators=[enum_validator(ForecastConfigEnum)])
    cycle_date = serializers.DateTimeField(required=True, allow_null=False)
    cold_start_run = GetStatusColdStartSerializer(required=False, allow_null=True)


class GetStatusForHindcastResponseSerializer(CommonStatusFieldsMixin, HindcastRunIdSerializer, CalibrationRunIdSerializer):
    message = serializers.CharField(required=False)
    configuration = serializers.CharField(required=True, validators=[enum_validator(ForecastConfigEnum)])
    cycle_date = serializers.DateTimeField(required=True, allow_null=False)
    cold_start_run = GetStatusColdStartSerializer(required=False, allow_null=True)


class GetStatusForCalibrationResponseSerializer(GenericResponseSerializer, CommonStatusFieldsMixin):
    warnings = serializers.ListField(required=False, child=serializers.CharField(required=True))
    errors = serializers.ListField(required=False, child=serializers.CharField(required=True))
    validations = GetStatusForValidationResponseSerializer(many=True)


class GetStatusForVerificationResponseSerializer(CommonStatusFieldsMixin, CalibrationRunIdSerializer, VerificationRunIdSerializer):
    message = serializers.CharField(required=True)
    forecast_run = GetStatusForForecastResponseSerializer(required=False, allow_null=True)


class GetVerificationJobResponseSerializer(CommonStatusFieldsMixin, ForecastRunIdSerializer, VerificationRunIdSerializer):
    forecast_run = GetStatusForForecastResponseSerializer(required=False, allow_null=True)


class GetVerificationJobsResponseSerializer(BaseSerializer):
    verification_jobs = GetVerificationJobResponseSerializer(many=True, required=True)

    total_count = serializers.IntegerField(required=True)
    date_range = serializers.ListField(child=serializers.DateTimeField(required=True, allow_null=False),
                                       min_length=2, max_length=2, required=False)
    id_range = serializers.ListField(child=serializers.IntegerField(required=True, allow_null=False),
                                     min_length=2, max_length=2, required=False)
    gages = serializers.ListField(child=serializers.CharField(), required=False, allow_empty=True)


class GetStatusForComparisonResponseSerializer(CalibrationRunIdList):
    statuses = CommonStatusFieldsMixin(many=True, required=False)
    errors = GetPlotErrorResponseSerializer(many=True, required=False)


class ImportResponseSerializer(GenericResponseSerializer):
    warnings = serializers.ListField(required=False, child=serializers.CharField(required=True))
    errors = serializers.ListField(required=False, child=serializers.CharField(required=True))
    messages = serializers.JSONField(required=False)


class SubmitCalibrationJobResponseSerializer(GenericResponseSerializer):
    submit_date = serializers.DateTimeField(required=True, allow_null=False)


class GetIterationsResponseSerializer(GenericResponseSerializer):
    iteration = serializers.IntegerField(required=True, allow_null=True)


class CalibrationJobSlurmCallbackRequestSerializer(CalibrationRunIdSerializer):
    job_status = serializers.CharField(required=True, validators=[enum_validator(SlurmCallbackStatusEnum, allow_blank=False)])


class ValidationJobSlurmCallbackRequestSerializer(ValidationRunIdSerializer):
    job_status = serializers.CharField(required=True, validators=[enum_validator(SlurmCallbackStatusEnum, allow_blank=False)])


class ColdStartJobSlurmCallbackRequestSerializer(ColdStartRunIdSerializer):
    job_status = serializers.CharField(required=True, validators=[enum_validator(SlurmCallbackStatusEnum, allow_blank=False)])


class ForecastJobSlurmCallbackRequestSerializer(ForecastRunIdSerializer):
    job_status = serializers.CharField(required=True, validators=[enum_validator(SlurmCallbackStatusEnum, allow_blank=False)])


class HindcastJobSlurmCallbackRequestSerializer(HindcastRunIdSerializer):
    job_status = serializers.CharField(required=True, validators=[enum_validator(SlurmCallbackStatusEnum, allow_blank=False)])


class VerificationJobSlurmCallbackRequestSerializer(VerificationRunIdSerializer):
    job_status = serializers.CharField(required=True, validators=[enum_validator(SlurmCallbackStatusEnum, allow_blank=False)])


class RunCalibrationJob(CalibrationRunIdSerializer):
    logging_config = LoggingConfigSerializer(required=False)


def get_mpi_rules_field(required: bool = True) -> serializers.ListField:
    return serializers.ListField(
        required=required,
        allow_null=not required,
        child=serializers.ListField(
            child=serializers.IntegerField(),
            min_length=2,
            max_length=2
        )
    )


class MPINodesRulesSerializer(BaseSerializer):
    mpi_rules = get_mpi_rules_field(required=False)

    def validate_mpi_rules(self, value):
        if value in (None, []):
            # Accept empty input for GET-style query (no validation needed)
            return value

        previous_threshold = -1
        for i, rule in enumerate(value):
            max_catchments, num_nodes = rule

            if max_catchments < -1:
                raise serializers.ValidationError(f"Invalid threshold {max_catchments} at index {i}")

            if num_nodes < 1:
                raise serializers.ValidationError(f"Number of nodes must be ≥ 1 at index {i}")

            if i < len(value) - 1:
                if max_catchments == -1:
                    raise serializers.ValidationError(f"-1 (infinite) threshold must only appear as the last rule (index {i})")
                if max_catchments <= previous_threshold:
                    raise serializers.ValidationError(f"Thresholds must be strictly increasing (problem at index {i})")

            previous_threshold = max_catchments

        if value[-1][0] != -1:
            raise serializers.ValidationError("The final rule must have max_catchments = -1 to cover all cases")

        return value


class MPINodesRulesResponseSerializer(GenericMessageResponseSerializer):
    mpi_rules = get_mpi_rules_field(required=False)


##################################
# Forecast Tab
##################################
class ForecastConfigSerializer(BaseSerializer):
    name = serializers.CharField(required=True, validators=[enum_validator(ForecastConfigEnum)])
    data_sources = serializers.CharField(required=False, allow_null=True)
    domain = serializers.CharField(required=False, validators=[enum_validator(DomainEnum)])
    availability_lag = serializers.IntegerField(required=True)
    cycle_start = serializers.IntegerField(required=True)
    cycle_end = serializers.IntegerField(required=True)
    cycle_freq = serializers.IntegerField(required=True)
    fcst_win = serializers.IntegerField(required=True)


class LoadForecastTabRequestSerializer(CalibrationRunIdSerializer):
    hindcast_only = serializers.BooleanField(allow_null=False, default=False)


class LoadForecastTabResponseSerializer(BaseSerializer):
    forecast_configuration_values = ForecastConfigSerializer(many=True)


class ColdStartJobsResponseSerializer(ColdStartRunIdSerializer):
    cold_start_status = serializers.CharField(required=False, allow_null=True, validators=[enum_validator(StatusEnum, allow_blank=False)])
    cold_start_date = serializers.DateTimeField(required=True, allow_null=False)
    cold_start_cycle_date = serializers.DateTimeField(required=True, allow_null=False)
    cold_start_submit_date = serializers.DateTimeField(required=True, allow_null=False)


class ForecastBaseJobsResponseSerializer(CalibrationRunIdSerializer):
    domain_name = serializers.CharField(required=True)
    configuration = serializers.CharField(required=True, validators=[enum_validator(ForecastConfigEnum)])
    cycle_date = serializers.DateTimeField(required=True, allow_null=False)
    gage_id = serializers.CharField(required=True)
    submit_date = serializers.DateTimeField(required=True, allow_null=True)


class ForecastJobsResponseSerializer(ForecastBaseJobsResponseSerializer, ForecastRunIdSerializer):
    forecast_status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum, allow_blank=False)])
    cold_start = ColdStartJobsResponseSerializer(required=False, allow_null=False)


class HindcastJobsResponseSerializer(ForecastBaseJobsResponseSerializer, HindcastRunIdSerializer):
    hindcast_status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum, allow_blank=False)])
    cold_start = ColdStartJobsResponseSerializer(required=True, allow_null=False)
    interval_cycle = serializers.ChoiceField(choices=[1, 3, 6, 12, 18, 24], required=True)
    num_iterations = serializers.IntegerField(required=True, allow_null=False, validators=[MinValueValidator(1)])


class GetForecastJobsResponseSerializer(BaseSerializer):
    forecast_jobs = ForecastJobsResponseSerializer(many=True, required=True)
    total_count = serializers.IntegerField(required=True)
    date_range = serializers.ListField(child=serializers.DateTimeField(required=True, allow_null=False),
                                       min_length=2, max_length=2, required=False)
    id_range = serializers.ListField(child=serializers.IntegerField(required=True, allow_null=False),
                                     min_length=2, max_length=2, required=False)
    gages = serializers.ListField(child=serializers.CharField(), required=False, allow_empty=True)


class GetHindcastJobsResponseSerializer(BaseSerializer):
    hindcast_jobs = HindcastJobsResponseSerializer(many=True, required=True)
    total_count = serializers.IntegerField(required=True)
    date_range = serializers.ListField(child=serializers.DateTimeField(required=True, allow_null=False),
                                       min_length=2, max_length=2, required=False)
    id_range = serializers.ListField(child=serializers.IntegerField(required=True, allow_null=False),
                                     min_length=2, max_length=2, required=False)
    gages = serializers.ListField(child=serializers.CharField(), required=False, allow_empty=True)


class GetColdStartJobsForConfigurationResponseSerializer(BaseSerializer):
    cold_start_jobs = serializers.ListSerializer(child=ColdStartJobsResponseSerializer(), required=True, allow_empty=True)


##################################
# Verification Tab
##################################


class CreateAndRunVerificationRequestSerializer(ForecastOrHindcastRunIdSerializer):
    logging_config = LoggingConfigSerializer(required=False)


class CreateAndRunVerificationResponseSerializer(GenericMessageWithIdResponseSerializer, ForecastOrHindcastRunIdSerializer, VerificationRunIdSerializer):
    status = serializers.CharField(validators=[enum_validator(StatusEnum, allow_blank=False)], required=True)
    submit_date = serializers.DateTimeField(required=True, allow_null=False)


class GetVerificationStatusResponseSerializer(GenericMessageAndStatusResponseSerializer, VerificationRunIdSerializer):
    status = serializers.CharField(validators=[enum_validator(StatusEnum, allow_blank=False)], required=True)
    warnings = serializers.ListField(required=False, child=serializers.CharField(required=True))
    errors = serializers.ListField(required=False, child=serializers.CharField(required=True))
    submit_date = serializers.DateTimeField(required=False, allow_null=True)
    run_start = serializers.DateTimeField(required=False, allow_null=True)
    run_end = serializers.DateTimeField(required=False, allow_null=True)
    elapsed_time = serializers.DurationField(required=False, allow_null=True)
    performance_metrics = PerformanceMetricsSerializer(required=False)
    failure_messages = serializers.ListField(child=serializers.DictField(), required=False)


class GetVerificationPlotNamesResponseSerializer(VerificationRunIdSerializer):
    plot_names = PlotListStaticSerializer(many=True)
    status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum, allow_blank=False)])


class GetVerificationPlotRequestSerializer(VerificationRunIdSerializer):
    plot_name = serializers.CharField(required=True, allow_null=False)


class GetVerificationPlotResponseSerializer(VerificationRunIdSerializer):
    plot_name = serializers.CharField(required=True, allow_null=False)
    plot_file_path = serializers.CharField(required=False, allow_null=False)
    plot_url = serializers.CharField(required=False, allow_null=False)


class DeleteVerificationJobResponseSerializer(GenericMessageResponseSerializer):
    verification_run_id = serializers.IntegerField(required=True, min_value=1)


##################################
# Import/Export
##################################


# All fields are required, so that the user can see what is missing.
# Any objects will be set to an empty object, {} or []
# Booleans will default to False
# Scalers will be set to None
class ExportResponseSerializer(BaseSerializer):
    metadata = serializers.JSONField(required=False)
    run_after_import = serializers.BooleanField(default=False)
    gage_id = serializers.CharField(required=True, allow_null=True)
    forcing_source = serializers.CharField(required=True, allow_null=True, validators=[enum_validator(ForcingSourceEnum)])
    observational_source = serializers.CharField(required=True, allow_null=True, validators=[enum_validator(ObservationalSourceEnum)])
    geopackage_source = serializers.CharField(required=True, allow_null=True, validators=[enum_validator(GeopackageSourceEnum)])
    modules = serializers.ListField(child=ModuleNameField(required=True), required=False, allow_empty=True)
    module_properties = ModulePropertiesSerializer(many=True, required=False, default=list)
    job_name = serializers.CharField(required=True, allow_null=True, allow_blank=False, validators=[no_space_validator])
    use_sloth = serializers.BooleanField(default=False)
    sloth_parameters = SlothParameters(many=True, default=list)
    automatic_validation = serializers.BooleanField(default=True, validators=[validate_automatic_validation])
    calibration_times = CalibrationTimeControls(required=False, allow_empty=True)
    validation_times = ValidationTimeControls(required=False, allow_empty=True)
    streamflow_threshold = serializers.FloatField(required=False, allow_null=True, validators=[greater_than_zero])
    peak_flow_threshold = serializers.FloatField(required=False, allow_null=True, validators=[greater_than_zero])
    parameters = SaveTuningParametersSerializer(many=True, required=True)
    objective_function = serializers.CharField(required=True, allow_null=True)
    optimization_inputs = OptimizationInputsSerializer(many=True, default=list)
    optimization = serializers.CharField(allow_blank=False, required=True, allow_null=True, validators=[enum_validator(OptimizationEnum)])
    save_plot_iteration_frequency = serializers.IntegerField(min_value=1, required=True, allow_null=True)
    save_output_iteration = serializers.BooleanField(required=True, allow_null=True)
    stop_criteria = serializers.IntegerField(required=True, allow_null=True, min_value=2)
    logging_config = LoggingConfigSerializer(required=False)


class ImportDataSerializer(BaseSerializer):
    run_after_import = serializers.BooleanField(required=False, default=False)
    metadata = serializers.JSONField(required=False)
    gage_id = serializers.CharField(required=False, allow_null=True)
    forcing_source = serializers.CharField(required=False, allow_null=True, validators=[enum_validator(ForcingSourceEnum)])
    forcing_user_dir = serializers.CharField(required=False, allow_null=True, allow_blank=False)
    observational_source = serializers.CharField(required=False, allow_null=True, validators=[enum_validator(ObservationalSourceEnum)])
    geopackage_source = serializers.CharField(required=False, allow_null=True, validators=[enum_validator(GeopackageSourceEnum)])
    modules = serializers.ListField(child=ModuleNameField(required=True), required=False, allow_empty=True)
    module_properties = ModulePropertiesSerializer(many=True, required=False, default=list)
    sloth_parameters = SlothParameters(required=False, many=True, allow_empty=True)
    job_name = serializers.CharField(required=False, allow_null=True, allow_blank=False, validators=[no_space_validator])
    use_sloth = serializers.BooleanField(required=False, default=False)
    automatic_validation = serializers.BooleanField(default=True, validators=[validate_automatic_validation])
    calibration_times = CalibrationTimeControls(required=False, allow_empty=True)
    validation_times = ValidationTimeControls(required=False, allow_empty=True)
    streamflow_threshold = serializers.FloatField(required=False, allow_null=True, validators=[greater_than_zero])
    peak_flow_threshold = serializers.FloatField(required=False, allow_null=True, validators=[greater_than_zero])
    parameters = SaveTuningParametersSerializer(many=True, required=False, allow_missing_bounds=True)
    objective_function = serializers.CharField(required=False, allow_null=True)
    optimization_inputs = OptimizationInputsSerializer(many=True, required=False)
    optimization = serializers.CharField(allow_blank=False, required=False, allow_null=True, validators=[enum_validator(OptimizationEnum)])
    save_plot_iteration_frequency = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    save_output_iteration = serializers.BooleanField(required=False, allow_null=False, default=False)
    stop_criteria = serializers.IntegerField(required=False, allow_null=True, min_value=2)
    logging_config = LoggingConfigSerializer(required=False)

    def validate(self, attrs: dict) -> dict:
        attrs = super().validate(attrs)

        validate_module_properties_against_modules(
            modules=attrs.get("modules") or [],
            props=attrs.get("module_properties") or [],
        )

        return attrs


class ImportSerializer(BaseSerializer):
    calibration_run_id = serializers.IntegerField(required=False, min_value=1)
    data = ImportDataSerializer(required=True)


##################################
# Misc
##################################
class ReportIterationSerializer(CalibrationRunIdSerializer):
    iteration = serializers.IntegerField(required=True, min_value=0)
    worker_name = serializers.CharField(required=True)
    first_iteration_for_worker = serializers.BooleanField(required=True)


class ErrorDetailListField(serializers.ListField):
    child = serializers.CharField()

    def to_representation(self, value):
        # Ensure that the value is a list of ErrorDetail objects
        if not all(isinstance(item, ErrorDetail) for item in value):
            raise serializers.ValidationError("All items must be instances of ErrorDetail.")
        return [str(item) for item in value]

    def to_internal_value(self, data):
        if not isinstance(data, list):
            raise serializers.ValidationError("Expected a list of strings.")
        return [ErrorDetail(item) for item in data]


class ErrorResponseSerializer(BaseSerializer):
    response_type = serializers.CharField(required=True, allow_blank=False, allow_null=False)
    message = serializers.CharField(required=True, allow_blank=False, allow_null=False)
    validation_errors = serializers.JSONField(required=False, allow_null=False)
    errors = serializers.JSONField(required=False, allow_null=False)


##################################
# Evaluation
##################################
class GetZipStatusSerializer(CalibrationRunIdSerializer):
    zip_status = serializers.ChoiceField(choices=['pending', 'done', 'error'], required=True, allow_null=False)
    started_at = serializers.DateTimeField(required=True, allow_null=False)
    s3_object = serializers.CharField(required=True, allow_null=True, allow_blank=False)


class GetZipDownloadUrlResponseSerializer(CalibrationRunIdSerializer):
    download_url = serializers.CharField()
    expires_in_seconds = serializers.IntegerField()


class ParameterDataByIteration(BaseSerializer):
    parameter_name = serializers.CharField(required=True, allow_blank=False, allow_null=False)
    parameter_value = serializers.FloatField(required=True, allow_null=False)


class MetricDataByIteration(BaseSerializer):
    metric_name = serializers.CharField(required=True, allow_blank=False, allow_null=False)
    metric_display_name = serializers.CharField(required=True, allow_blank=False, allow_null=False)
    # Need to allow Null for NaN
    metric_value = serializers.FloatField(required=True, allow_null=True)


class CalibrationDataByIteration(BaseSerializer):
    iteration_num = serializers.IntegerField(required=True, allow_null=False, min_value=0)
    iteration_id = serializers.IntegerField(required=True, allow_null=False)
    validation_run_id = serializers.IntegerField(required=False, min_value=1)
    worker_name = serializers.CharField(required=True, allow_null=False, allow_blank=False)
    best_params = serializers.BooleanField(required=True, allow_null=False)
    objective_function_value = serializers.FloatField(required=True, allow_null=True)
    parameters = ParameterDataByIteration(many=True, required=True)
    metrics = MetricDataByIteration(many=True, required=True)


class RetrospectiveData(BaseSerializer):
    name = serializers.CharField(required=True)
    data = MetricDataByIteration(many=True, required=True)


class GetCalibrationDataByIterationResponseSerializer(GenericMessageResponseSerializer):
    objective_function_metric = serializers.CharField(required=True, allow_null=True)
    iteration_data = CalibrationDataByIteration(many=True, required=True)
    retrospective_data = RetrospectiveData(many=True, required=True)


class ValidationJobsResponseSerializer(ValidationRunIdSerializer):
    submit_date = serializers.DateTimeField(required=True, allow_null=True)
    validation_type = serializers.CharField(required=True)
    iteration_num = serializers.IntegerField(required=True)
    status = serializers.CharField(required=True, validators=[enum_validator(StatusEnum, allow_blank=False)])
    # Can be empty for LSTM
    parameters = ValidationJobsParameter(many=True, required=True)
    best = serializers.BooleanField(required=True)


class GetValidationJobsResponseSerializer(BaseSerializer):
    validation_jobs = ValidationJobsResponseSerializer(many=True, required=True)


class GetLogRequestSerializer(CalibrationOrValidationOrColdStartOrForecastOrHindcastOrVerificationRunIdSerializer):
    log_name = serializers.CharField(required=True, allow_blank=False)
    start = serializers.IntegerField(required=False, default=0, min_value=-1)
    limit = serializers.IntegerField(required=False, default=100, min_value=1)


class GetLogStatusRequestSerializer(CalibrationOrValidationOrColdStartOrForecastOrHindcastOrVerificationRunIdSerializer):
    log_name = serializers.CharField(required=True)
    byte_offset = serializers.IntegerField(required=True, min_value=0)


class LogCategoryDictField(serializers.DictField):
    def __init__(self, **kwargs):
        super().__init__(
            child=serializers.ListField(
                child=serializers.CharField(),
                allow_empty=True,
            ),
            **kwargs,
        )
        self.key_validator = enum_validator(LogCategory, allow_blank=False)

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("Expected a dictionary.")

        if len(data) != 1:
            raise serializers.ValidationError(
                "Each log_names entry must contain exactly one log category."
            )

        for key in data:
            if not isinstance(key, str):
                raise serializers.ValidationError(
                    f"Invalid key type: {type(key)}. Expected string."
                )
            self.key_validator(key)

        return super().to_internal_value(data)


class GetLogNamesResponseSerializer(BaseSerializer):
    log_names = serializers.ListField(child=LogCategoryDictField(), required=True, allow_empty=True)


class GetLogsResponseSerializer(GenericMessageResponseSerializer):
    log_data = serializers.ListField(child=serializers.CharField(allow_blank=True), required=True)
    pagination_metadata = PaginationMetadataSerializer(required=False)
    log_name = serializers.CharField(required=True, allow_blank=False, allow_null=False)
    byte_offset = serializers.IntegerField(required=False)
    # status = serializers.CharField(validators=[enum_validator(StatusEnum)], required=False)


class GetLogStatusResponseSerializer(GenericMessageResponseSerializer):
    file_updated = serializers.BooleanField(required=True)
    # status = serializers.CharField(validators=[enum_validator(StatusEnum)], required=True)


##################################
# Snowdas/SWE/Soil Moisture
##################################
class GetSWEImagesByDateRequestSerializer(ValidationRunIdSerializer):
    date = serializers.DateField(required=True, allow_null=False)


class GetSoilMoistureImagesByDateRequestSerializer(ValidationRunIdSerializer):
    datetime = serializers.DateTimeField(required=True, allow_null=False)

    def validate_datetime(self, value):
        """
        Ensure that the datetime is at hour precision (minutes and seconds are zero).
        Example of valid value: 2025-10-01T12:00:00
        """
        if value.minute != 0 or value.second != 0 or value.microsecond != 0:
            raise serializers.ValidationError(
                "Datetime must be at the top of the hour (e.g., 2025-10-01T12:00:00)."
            )
        return value


class GetImagesByDateResponseSerializer(GenericMessageResponseSerializer):
    lumped_map = serializers.CharField(required=True, allow_null=False)
    raw_map = serializers.CharField(required=True, allow_null=False)
    sim_map = serializers.CharField(required=True, allow_null=False)


class GetTimeseriesDataResponseSerializer(GenericMessageResponseSerializer):
    timeseries_image = serializers.CharField(required=True, allow_null=False)
    timeseries_data = serializers.JSONField(required=True, allow_null=False)


##################################
# Slurm
##################################
class SlurmSubmitResponseSerializer(BaseSerializer):
    slurm_job_id = serializers.IntegerField(required=False, allow_null=False)
