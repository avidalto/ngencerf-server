import io
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import MAXYEAR, MINYEAR, datetime, timezone
from typing import Literal
from urllib.parse import urlparse

import pandas as pd
from datetimerange import DateTimeRange
from django.conf import settings
from django.db import transaction
from django.db.models import QuerySet, Prefetch
from drf_spectacular.utils import OpenApiParameter, extend_schema, OpenApiResponse
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import StatusEnum
from calibration.enums_vanilla import JobType
from calibration.models import CalibrationFormulation, CalibrationParameter, CalibrationRun
from calibration.util import cloud_util
from calibration.util.caching import get_cached_module_by_name, have_LSTM, get_cached_modules_by_id
from calibration.util.calibration_validators import CalibrationRunIdSerializer, SaveTuningRequestSerializer, LoadTuningResponseSerializer, \
    ErrorResponseSerializer, UploadUserParameterFile, UserParameterFileUploadResponse, \
    ValidateParametersResponseSerializer, SaveTuningResponseSerializer
from calibration.util.ngen_locations import get_forcing_dir_for_job
from calibration.views import ngen_cal_input
from calibration.views.called_from import get_caller_name
from calibration.views.common import get_calibration_run, ResponseError, handle_exceptions, validate_response, CerfException, validate_request, \
    get_valid_path, format_datetime, get_user_email, get_elapsed_str, readonly_transaction, ErrorReport
from calibration.views.data_services import should_use_bmi_forcing, get_observational_date_range_from_data_services

logger = logging.getLogger(__name__)

MIN_TIME = datetime(MAXYEAR, 12, 31, 11, 59, 59).replace(tzinfo=timezone.utc)
MAX_TIME = datetime(MINYEAR, 1, 1, 0, 0, 0).replace(tzinfo=timezone.utc)


@extend_schema(
    request=CalibrationRunIdSerializer,
    responses={
        200: LoadTuningResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    parameters=[
        OpenApiParameter(name='calibration_run_id', description='ID of the calibration run', required=True, type=int)
    ],
    description="Load tuning tab data for a calibration run"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def load_tuning_tab(request: Request) -> Response:
    """
    API endpoint to load tuning tab data for a calibration run.

    Splits read-heavy operations into a read-only transaction,
    then persists time_range if it was newly computed. Calls
    ready_to_run() outside the read-only block so that updates
    to run.status are persisted and reflected in the response.

    :param request: Django HTTP request, containing parameters in the body for POST or query params for GET.
    :return: Response containing the tuning tab data, including time ranges, modules, and formulations.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunIdSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')

    # Phase 1: Read-only section (heavy reads)
    with readonly_transaction():
        run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=list(StatusEnum))
        if error_return:
            return error_return
        assert run is not None

        # Compute time range without persisting
        time_range = compute_time_range(run)
        calibration_times, validation_times = get_times(run)

        formulations = (
            CalibrationFormulation.objects
            .filter(calibration_run=run)
            .select_related('module')
            .prefetch_related(
                Prefetch(
                    'calibrationparameter_set',
                    queryset=CalibrationParameter.objects.only(
                        'calibration_formulation_id',
                        'name', 'minimum', 'maximum', 'initial_value',
                        'units', 'data_type', 'description', 'user_selected_for_tuning'
                    ),
                    to_attr='prefetched_params',
                )
            )
        )

        # For each module, get the Parameters and Output Variables
        module_list = get_parameters(formulations)

    # Phase 2: Write section (ready_to_run + optional persist_time_range)
    ngen_cal_input.ready_to_run(run)

    if time_range and (not run.time_range_start or not run.time_range_end):
        with transaction.atomic():
            persist_time_range(run, time_range)

    # Phase 3: Build response with updated run.status
    response = {
        'calibration_run_id': run.id,
        'status': run.status.name,  # reflects updated status
        'modules': module_list,
        'time_range': time_range,
        'calibration_times': calibration_times,
        'validation_times': validation_times
    }

    response_validator, error_response = validate_response(LoadTuningResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(response_validator.data)}'
    )

    return Response(response_validator.data)


def has_user_selected_tuning_parameters(formulations: QuerySet[CalibrationFormulation]) -> bool:
    """
    Check whether any parameters tied to the provided calibration formulations
    are marked user_selected_for_tuning.

    :param formulations: QuerySet of CalibrationFormulation objects for a single run.
    :return: True if at least one parameter in these formulations is marked
             user_selected_for_tuning, otherwise False.
    """
    return CalibrationParameter.objects.filter(
        calibration_formulation__in=formulations,
        user_selected_for_tuning=True
    ).exists()


def get_parameters(modules: QuerySet[CalibrationFormulation]) -> list[dict[str, str | list[dict[str, str | float | int]]]]:
    """
    Retrieves the calibration parameters for each module in the specified calibration formulation.

    Uses `select_related('module')` and prefetched CalibrationParameter objects to avoid DB hits.

    :param modules: QuerySet of CalibrationFormulation objects, built with
                    select_related('module') and Prefetch for calibrationparameter_set.
    :return: List of dicts with the module name and its parameters.
    """
    module_list: list[dict] = []

    # 'modules' must be built with select_related('module') and the Prefetch above.
    for formulation in modules:
        module = formulation.module  # Already populated by select_related

        # Use the prefetched list (no DB hits here)
        params = [
            {
                'name': p.name,
                'minimum': p.minimum,
                'maximum': p.maximum,
                'initial_value': p.initial_value,
                'units': p.units,
                'data_type': p.data_type,
                'description': p.description,
                'user_selected_for_tuning': p.user_selected_for_tuning,
            }
            for p in getattr(formulation, 'prefetched_params', [])
        ]

        module_list.append({
            'name': module.name,
            'parameters': params,
        })

    return module_list


def get_parameters_for_export(run: CalibrationRun) -> list[dict]:
    """
    Export calibration parameters for all modules in the given calibration run that have been selected by the user
    Uses cached modules to resolve names instead of hitting DB for Module.

    :param run: The CalibrationRun to export parameters from.
    :return: List of parameter dicts for export.
    """
    modules_by_id = get_cached_modules_by_id()

    # Query parameters linked to formulations by module_id
    params = (
        CalibrationParameter.objects
        .filter(calibration_formulation__calibration_run=run, user_selected_for_tuning=True)
        .values(
            "name", "initial_value", "minimum", "maximum",
            "calibration_formulation__module_id"
        )
    )

    result = []
    for p in params:
        module_id = p["calibration_formulation__module_id"]
        module_name = modules_by_id[module_id].name
        result.append({
            "name": p["name"],
            "initial_value": p["initial_value"],
            "minimum": p["minimum"],
            "maximum": p["maximum"],
            "module": module_name,
        })
    return result


def compute_time_range(run: CalibrationRun) -> dict[str, datetime]:
    """
    Compute the intersection of observational and forcing data ranges for the given run,
    without persisting anything to the database.

    Behavior:
      - If the run already has a persisted time range (both start and end), that exact range is returned.
      - If required data is missing, returns an empty dict.
      - If both sources are available, computes the intersection and returns a dictionary with:
          * 'start_time': datetime (UTC, timezone-aware),
          * 'end_time': datetime (UTC, timezone-aware).
      - If there is no valid overlap between observational and forcing ranges, returns None.

    :param run: CalibrationRun instance.
    :return: A dictionary containing 'start_time' and 'end_time', or {} if unavailable.
    """
    if run.time_range_start and run.time_range_end:
        logger.info("Time range is already set")
        return {'start_time': run.time_range_start, 'end_time': run.time_range_end}

    forcing_path = get_valid_path(
        run.forcing_eds_dir_path,
        lambda: get_forcing_dir_for_job(run)
    )

    use_bmi = should_use_bmi_forcing(run)

    # Explicitly log the resolved paths
    logger.info(
        f"get_time_range: "
        f"forcing_path={forcing_path},"
        f"use_bmi_forcing={use_bmi}"
    )

    # TODO More cleanup when we are exclusively using bmi forcing
    # For CSV forcing, forcing_path is also required
    if not use_bmi and not forcing_path:
        return {}

    # If both paths are available, calculate intersection and update run
    daterange_intersection_start = time.perf_counter()

    daterange = get_date_range_intersection(
        run,
        None if use_bmi else forcing_path
    )

    logger.info(f"Date range intersection completed in "
                f"{time.perf_counter() - daterange_intersection_start:.2f}s")

    if daterange:
        return {
            'start_time': daterange.start_datetime,
            'end_time': daterange.end_datetime
        }

    return {}


def persist_time_range(run: CalibrationRun, time_range: dict[str, datetime]) -> None:
    """
    Persist the computed time range to the database if values are provided.

    :param run: CalibrationRun instance to update.
    :param time_range: Dictionary containing both 'start_time' and 'end_time'.
                       Assumes these keys are present and valid datetimes.
    """
    run.time_range_start = time_range['start_time']
    run.time_range_end = time_range['end_time']
    run.save(update_fields=['time_range_start', 'time_range_end'])


def get_times(run: CalibrationRun) -> tuple[dict[str, datetime], dict[str, datetime]]:
    """
    Retrieves calibration and validation time periods for a given calibration run.

    :param run: The CalibrationRun instance containing time period information.
    :return: A tuple containing two dictionaries:
             - The first dictionary holds calibration time periods.
             - The second dictionary holds validation time periods (if automatic validation is enabled).
    """
    calibration_times = {}
    validation_times = {}

    # If calibration times exist, assume all related fields are present
    if run.calibration_start_period:
        calibration_times = {
            'simulation_start_time': run.calibration_start_period,
            'simulation_end_time': run.calibration_end_period,
            'calibration_start_time': run.calibration_eval_start_period,
            'calibration_end_time': run.calibration_eval_end_period
        }

    # If automatic validation is enabled and validation times exist, populate validation times
    if run.automatic_validation and run.validation_start_period:
        validation_times = {
            'simulation_start_time': run.validation_start_period,
            'simulation_end_time': run.validation_end_period,
            'validation_start_time': run.validation_eval_start_period,
            'validation_end_time': run.validation_eval_end_period
        }
    return calibration_times, validation_times


@extend_schema(
    request=SaveTuningRequestSerializer,
    responses={
        200: SaveTuningResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Save tuning tab data"
)
@api_view(['POST'])
@handle_exceptions
def save_tuning_tab(request: Request) -> Response:
    """
    Saves tuning settings for a calibration run, including parameters, output variables, and time periods.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(SaveTuningRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    automatic_validation = validator.get('automatic_validation')
    calibration_times = validator.get('calibration_times')
    validation_times = validator.get('validation_times')
    parameters = validator.get('parameters')

    run, error_return = get_calibration_run(calibration_run_id, request.user)
    if error_return:
        return error_return
    assert run is not None

    # Require at least one module selected for this job (i.e., at least one formulation exists)
    has_any_modules = CalibrationFormulation.objects.filter(calibration_run=run).exists()
    if not has_any_modules:
        return ResponseError("You must select at least one module before selecting tuning parameters")

    # --- Validate parameter selection rules ---
    module_names_for_job = set(
        CalibrationFormulation.objects
        .filter(calibration_run=run)
        .values_list("module__name", flat=True)
    )

    selected_module_names = {p["module"] for p in (parameters or []) if p.get("module")}

    parameter_rule_report = ErrorReport()
    validate_parameter_rules(
        module_names_for_job=module_names_for_job,
        selected_module_names=selected_module_names,
        have_LSTM_flag=have_LSTM(run),
        has_any_params=bool(selected_module_names),
        error_object=parameter_rule_report,
    )

    if have_LSTM(run) and parameters:
        return ResponseError('You cannot specify parameters when using LSTM')

    run.automatic_validation = automatic_validation

    error_message = validate_and_save_times(run, calibration_times, validation_times)
    if error_message:
        return ResponseError(error_message)

    if parameters and not run.gage:
        return ResponseError('Parameters cannot be specified without a gage')

    # The UI already does the parameter validation, so we don't have to bother sending the warnings
    parameter_errors, _ = validate_parameter_values(run, parameters)
    if parameter_errors:
        return ResponseError(parameter_errors)

    with transaction.atomic():
        save_parameters(run, parameters)

    run.save()

    ngen_cal_input.ready_to_run(run)

    response = {'message': f'Calibration Job {run.id} updated', 'calibration_run_id': run.id, 'status': run.status.name}

    if parameter_rule_report.has_warnings():
        response["parameter_warnings"] = parameter_rule_report.warnings
    if parameter_rule_report.has_errors():
        response["parameter_errors"] = parameter_rule_report.errors

    response_validator, error_response = validate_response(SaveTuningResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)


@extend_schema(
    request=UploadUserParameterFile,
    responses={
        200: UserParameterFileUploadResponse,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Allow user to upload a starting parameter file"
)
@api_view(['POST'])
@handle_exceptions
def upload_user_parameters(request: Request) -> Response:
    """
    Allows the user to upload a parameter file for tuning, validating its structure
    and content, and then attaching it to the specified calibration run.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(UploadUserParameterFile, data, context={'request': request})
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')

    run, error_return = get_calibration_run(calibration_run_id, request.user)
    if error_return:
        return error_return
    assert run is not None

    files = request.FILES.getlist('user_parameter_file')
    if not files:
        return ResponseError('No file uploaded under field "user_parameter_file".')
    if len(files) > 1:
        logger.warning(f'{get_caller_name()}() multiple files uploaded; using the first one')

    # Process the first file in the list
    parameter_file = files[0]
    try:
        file_contents = parameter_file.read().decode('utf-8')
    except Exception as exc:
        logger.exception('Failed to read/decode uploaded file as UTF-8')
        return ResponseError(f'Failed to read file as UTF-8: {exc}')

    if not file_contents.strip():
        return ResponseError('Uploaded file is empty.')

    # Detect delimiter type by checking the first few rows
    first_line = file_contents.splitlines()[0]

    if ',' in first_line:
        delimiter = ','
        logger.debug("Detected comma delimiter.")
    elif '\t' in first_line:
        delimiter = '\t'
        logger.debug("Detected tab delimiter.")
    else:
        delimiter = r'\s+'
        logger.debug("Detected space delimiter.")

    # Expected columns
    required_columns = ['param', 'min', 'max', 'init', 'model']
    expected_cols = len(required_columns)

    # Pre-validate consistent column counts when we have a simple delimiter
    # (csv.reader can't handle regex separators, so we skip this for r'\s+')
    if delimiter in (',', '\t'):
        import csv
        lines = file_contents.splitlines()
        # Header check (strict match on header names after trim)
        header_cols = [c.strip() for c in next(csv.reader([lines[0]], delimiter=delimiter))]
        if header_cols != required_columns:
            return ResponseError(
                f'Header mismatch. Expected: {required_columns}, Found: {header_cols}'
            )
        # Validate each data line has exactly the expected number of columns
        for i, row in enumerate(lines[1:], start=2):  # human line numbers
            cols = next(csv.reader([row], delimiter=delimiter))
            if len(cols) != expected_cols:
                return ResponseError(
                    f'Row {i} has {len(cols)} fields; expected {expected_cols}. Offending row: {row}'
                )

    # Parse with pandas; enforce dtypes so we fail fast on bad numerics
    try:
        # Handle file parsing based on detected delimiter
        df = pd.read_csv(
            io.StringIO(file_contents),
            sep=delimiter,
            engine='python',
            skipinitialspace=True,
            dtype={'param': str, 'min': float, 'max': float, 'init': float, 'model': str},
        )
    except pd.errors.ParserError as exc:
        logger.debug(f'Pandas parser error: {exc}')
        return Response({'error': f'Could not parse file with detected delimiter: {exc}'}, status=400)
    except ValueError as exc:
        # Typically raised when dtype conversion fails with informative message
        logger.debug(f'Pandas dtype error: {exc}')
        return Response({'error': f'Invalid data types in file: {exc}'}, status=400)

    # Strip any leading/trailing whitespace in the column headers
    df.columns = df.columns.str.strip()

    # Log detected columns for debugging
    logger.debug(f"Detected columns: {df.columns.tolist()}")

    # Ensure that the DataFrame contains the correct columns
    missing_cols = [col for col in required_columns if col not in df.columns]
    if missing_cols:
        # Log the actual DataFrame to inspect it
        logger.debug(f"DataFrame content:\n{df.head()}")
        return ResponseError(f'Missing required columns: {missing_cols}')

    # Ensure no unexpected columns (common when a row has too many fields and pandas shifts things)
    unexpected = [c for c in df.columns if c not in required_columns]
    if unexpected:
        return ResponseError(f'Unexpected columns present: {unexpected}. Expected only {required_columns}.')

    # Ensure there is at least one data row
    if df.empty:
        return ResponseError('No data rows found. Provide at least one parameter row.')

    # Validate numeric columns and report exact offending lines/values
    invalid_details: dict[str, list[dict[str, object]]] = {}
    for col in ['min', 'max', 'init']:
        # Re-coerce to catch NaN in case dtype enforcement was bypassed by space sep quirks
        coerced = pd.to_numeric(df[col], errors='coerce')
        bad_mask = pd.isna(coerced)
        if bad_mask.any():
            bad_rows = df[bad_mask]
            # +2 => header is line 1; df index 0 is line 2
            invalid_details[col] = [
                {
                    'line': offset + 2,
                    'param': str(row['param']),
                    'value': row.get(col)
                }
                for offset, (_, row) in enumerate(bad_rows.iterrows())
            ]

    if invalid_details:
        logger.debug(f"Invalid numeric values: {invalid_details}")
        return Response({'error': 'Invalid numeric values', 'details': invalid_details}, status=400)

    # Range checks: min <= max and init within [min, max]
    range_errors = {}

    bad_minmax_mask = df['min'] > df['max']
    if bad_minmax_mask.any():
        rows = df[bad_minmax_mask]
        range_errors['min_gt_max'] = [
            {
                'line': offset + 2,
                'param': str(row['param']),
                'min': row['min'],
                'max': row['max']
            }
            for offset, (_, row) in enumerate(rows.iterrows())
        ]

    bad_init_low = df['init'] < df['min']
    if bad_init_low.any():
        rows = df[bad_init_low]
        range_errors.setdefault('init_lt_min', [])
        range_errors['init_lt_min'].extend(
            {
                'line': offset + 2,
                'param': str(row['param']),
                'init': row['init'],
                'min': row['min']
            }
            for offset, (_, row) in enumerate(rows.iterrows())
        )

    bad_init_high = df['init'] > df['max']
    if bad_init_high.any():
        rows = df[bad_init_high]
        range_errors.setdefault('init_gt_max', [])
        range_errors['init_gt_max'].extend(
            {
                'line': offset + 2,
                'param': str(row['param']),
                'init': row['init'],
                'max': row['max']
            }
            for offset, (_, row) in enumerate(rows.iterrows())
        )

    if range_errors:
        logger.debug(f"Range validation errors: {range_errors}")
        return Response({'error': 'Range validation failed', 'details': range_errors}, status=400)

    logger.debug(f"Parsed DataFrame after stripping and numeric conversion: \n{df}")

    # Convert DataFrame to a list of dictionaries
    parsed_data = df.to_dict(orient='records')

    # Persist filename on the run
    run.user_parameter_filename = parameter_file.name
    run.save(update_fields=['user_parameter_filename'])

    response = {
        'message': f"Parameter file '{parameter_file.name}' saved for Calibration Job {run.id}",
        'calibration_run_id': run.id,
        'user_parameter_file': parsed_data
    }

    response_validator, error_response = validate_response(UserParameterFileUploadResponse, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)


@extend_schema(
    request=CalibrationRunIdSerializer,
    responses={
        200: ValidateParametersResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Validate the tuning parameter selection rules"
)
@api_view(['POST'])
@handle_exceptions
def validate_parameters(request: Request) -> Response:
    """
    Validate the selected tuning parameters for a calibration run.

    Applies parameter selection rules (e.g., LSTM/Topoflow requirements) and returns any
    warnings/errors in the same style as validate_formulation_tab.

    :param request: Django REST Framework request with calibration_run_id.
    :return: Response containing optional parameter_warnings and parameter_errors lists.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunIdSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')

    run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return
    assert run is not None

    # --- Gather what validate_parameter_selection_rules needs ---
    module_names_for_job = set(
        CalibrationFormulation.objects
        .filter(calibration_run=run)
        .values_list("module__name", flat=True)
    )

    selected_module_names = set(
        CalibrationParameter.objects
        .filter(calibration_formulation__calibration_run=run, user_selected_for_tuning=True)
        .values_list("calibration_formulation__module__name", flat=True)
        .distinct()
    )

    error_object = ErrorReport()

    validate_parameter_rules(
        module_names_for_job=module_names_for_job,
        selected_module_names=selected_module_names,
        have_LSTM_flag=have_LSTM(run),
        has_any_params=bool(selected_module_names),
        error_object=error_object,
    )

    response: dict[str, object] = {}
    if error_object.has_warnings():
        response["parameter_warnings"] = error_object.warnings
    if error_object.has_errors():
        response["parameter_errors"] = error_object.errors

    response_validator, error_response = validate_response(ValidateParametersResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(response_validator.data)}'
    )

    return Response(response_validator.data)


def validate_simulation_within_range(
        data_start: datetime,
        data_end: datetime,
        simulation_start: datetime,
        simulation_end: datetime,
        job_type: Literal[JobType.CALIBRATION, JobType.VALIDATION]
) -> str | None:
    """
    Validates that the simulation period falls within the available data range.

    :param data_start: The start date of the available data range.
    :param data_end: The end date of the available data range.
    :param simulation_start: The start date of the simulation period.
    :param simulation_end: The end date of the simulation period.
    :param job_type: Specifies whether this is a calibration or validation job (used in the error message).
    :return: An error message if the simulation period is out of bounds; otherwise, None.
    """
    if simulation_start < data_start or simulation_end > data_end:
        return (
            f"{job_type.value.capitalize()} simulation times must be within the intersection of forcing data and "
            f"observational data - {format_datetime(data_start)} to {format_datetime(data_end)}"
        )
    return None


def validate_time_range_against_data(
        run: CalibrationRun,
        calibration_times: dict[str, datetime] | None = None,
        validation_times: dict[str, datetime] | None = None
) -> str | None:
    """
    Ensures that calibration and validation times fall within the observational and forcing data range of the run.

    :param run: CalibrationRun instance.
    :param calibration_times: Dictionary containing calibration start and end times.
    :param validation_times: Dictionary containing validation start and end times.
    :return: Error message if validation fails; otherwise, None.
    """
    if not (run.time_range_start and run.time_range_end):
        return None

    data_start, data_end = run.time_range_start, run.time_range_end

    # Retrieve calibration period from either provided dictionary or `run`
    calibration_start = calibration_times.get('simulation_start_time') if calibration_times else run.calibration_start_period
    calibration_end = calibration_times.get('simulation_end_time') if calibration_times else run.calibration_end_period

    if calibration_start and calibration_end:
        error_message = validate_simulation_within_range(data_start, data_end, calibration_start, calibration_end, JobType.CALIBRATION)
        if error_message:
            return error_message

    # Retrieve validation period from either provided dictionary or `run`
    validation_start = validation_times.get('simulation_start_time') if validation_times else run.validation_start_period
    validation_end = validation_times.get('simulation_end_time') if validation_times else run.validation_end_period

    if run.automatic_validation and validation_start and validation_end:
        return validate_simulation_within_range(data_start, data_end, validation_start, validation_end, JobType.VALIDATION)

    return None


def validate_and_save_times(run: CalibrationRun, calibration_times: dict[str, datetime], validation_times: dict[str, datetime]) -> str | None:
    """
    Validates calibration and validation time ranges, ensuring they fall within the allowable data range.
    If valid, updates the `CalibrationRun` instance with the provided times.

    :param run: The calibration run being validated and updated.
    :param calibration_times: Dictionary containing calibration start, end, and evaluation periods.
    :param validation_times: Dictionary containing validation start, end, and evaluation periods.
    :return: A list of error messages if validation fails; otherwise, an empty list.
    """

    messages = []

    # Validation against forcing and observational data intersection
    error_message = validate_time_range_against_data(run, calibration_times, validation_times)
    if error_message:
        messages.append(error_message)

    if calibration_times:
        error_message, calibration_simulation_range = validate_time_range(
            calibration_times.get('simulation_start_time'),
            calibration_times.get('simulation_end_time'),
            'calibration simulation'
        )
        if error_message:
            messages.append(error_message)

        error_message, calibration_evaluation_range = validate_time_range(
            calibration_times.get('calibration_start_time'),
            calibration_times.get('calibration_end_time'),
            'calibration evaluation'
        )
        if error_message:
            messages.append(error_message)
    else:
        calibration_simulation_range = None
        calibration_evaluation_range = None

    if validation_times:
        error_message, validation_simulation_range = validate_time_range(
            validation_times.get('simulation_start_time'),
            validation_times.get('simulation_end_time'),
            'validation simulation'
        )
        if error_message:
            messages.append(error_message)

        error_message, validation_evaluation_range = validate_time_range(
            validation_times.get('validation_start_time'),
            validation_times.get('validation_end_time'),
            'validation evaluation'
        )
        if error_message:
            messages.append(error_message)
    else:
        validation_simulation_range = None
        validation_evaluation_range = None

    # If any of the ranges are invalid, return JSON list immediately
    if messages:
        return json.dumps(messages)

    # Define full evaluation range from minimum and maximum evaluation start/end times
    full_evaluation_start_date: datetime | None = None
    full_evaluation_end_date: datetime | None = None

    # Define the expanded evaluation range from the minimum and maximum evaluation start/end times
    if validation_evaluation_range is not None and calibration_evaluation_range is not None:
        full_evaluation_start_date, full_evaluation_end_date = get_full_evaluation_date_range_from_ranges(
            calibration_evaluation_range,
            validation_evaluation_range
        )

    # Ensure calibration simulation range contains the calibration evaluation range
    if calibration_evaluation_range and calibration_simulation_range:
        start_outside_range = calibration_evaluation_range[0] < calibration_simulation_range[0]
        end_outside_range = calibration_evaluation_range[1] > calibration_simulation_range[1]

        if start_outside_range or end_outside_range:
            messages.append(
                f'Calibration simulation range from {format_datetime(calibration_simulation_range[0])} to '
                f'{format_datetime(calibration_simulation_range[1])} must contain the calibration evaluation range from '
                f'{format_datetime(calibration_evaluation_range[0])} to {format_datetime(calibration_evaluation_range[1])}.'
            )

    # Ensure validation simulation range contains both the calibration and validation evaluation ranges
    if validation_simulation_range:
        valid_simulation_start, valid_simulation_end = validation_simulation_range

        if full_evaluation_start_date is not None and full_evaluation_end_date is not None:
            if valid_simulation_start > full_evaluation_start_date or valid_simulation_end < full_evaluation_end_date:
                messages.append(
                    f'Validation simulation range from {format_datetime(valid_simulation_start)} to '
                    f'{format_datetime(valid_simulation_end)} must contain the calibration and validation evaluation ranges from '
                    f'{format_datetime(full_evaluation_start_date)} to {format_datetime(full_evaluation_end_date)}.'
                )

    # Check for overlap between calibration and validation evaluation ranges
    if validation_evaluation_range and calibration_evaluation_range:
        overlap_exists = (
                validation_evaluation_range[0] <= calibration_evaluation_range[1] and
                validation_evaluation_range[1] >= calibration_evaluation_range[0]
        )

        if overlap_exists:
            messages.append(
                f"Calibration evaluation range from {format_datetime(calibration_evaluation_range[0])} to "
                f"{format_datetime(calibration_evaluation_range[1])} cannot intersect the validation evaluation range from "
                f"{format_datetime(validation_evaluation_range[0])} to {format_datetime(validation_evaluation_range[1])}."
            )

    # Save times if no errors found
    if not messages:
        if calibration_times:
            run.calibration_start_period = calibration_times.get('simulation_start_time')
            run.calibration_end_period = calibration_times.get('simulation_end_time')
            run.calibration_eval_start_period = calibration_times.get('calibration_start_time')
            run.calibration_eval_end_period = calibration_times.get('calibration_end_time')

        if run.automatic_validation and validation_times:
            run.validation_start_period = validation_times.get('simulation_start_time')
            run.validation_end_period = validation_times.get('simulation_end_time')
            run.validation_eval_start_period = validation_times.get('validation_start_time')
            run.validation_eval_end_period = validation_times.get('validation_end_time')

    return None


def get_full_evaluation_date_range_from_ranges(
        calibration_evaluation_range: tuple[datetime, datetime],
        validation_evaluation_range: tuple[datetime, datetime]
) -> tuple[datetime, datetime]:
    """
    Determines the full evaluation date range by identifying the earliest start time and latest end time
    across both calibration and validation evaluation ranges.

    :param calibration_evaluation_range: Tuple containing calibration evaluation start and end times.
    :param validation_evaluation_range: Tuple containing validation evaluation start and end times.
    :return: A tuple containing the start and end times of the full evaluation range.
    """
    start_date = min(calibration_evaluation_range[0], validation_evaluation_range[0])
    end_date = max(calibration_evaluation_range[1], validation_evaluation_range[1])
    return start_date, end_date


def get_full_evaluation_date_range(
        calibration_evaluation_start_time: datetime,
        calibration_evaluation_end_time: datetime,
        validation_evaluation_start_time: datetime,
        validation_evaluation_end_time: datetime
) -> tuple[datetime, datetime]:
    """
    Calculates the overall evaluation date range by taking the earliest start time and latest end time
    from both calibration and validation evaluation periods.

    :param calibration_evaluation_start_time: Start time of the calibration evaluation period.
    :param calibration_evaluation_end_time: End time of the calibration evaluation period.
    :param validation_evaluation_start_time: Start time of the validation evaluation period.
    :param validation_evaluation_end_time: End time of the validation evaluation period.
    :return: A tuple containing the start and end times of the combined evaluation period.
    """
    start_date = min(calibration_evaluation_start_time, validation_evaluation_start_time)
    end_date = max(calibration_evaluation_end_time, validation_evaluation_end_time)
    return start_date, end_date


def validate_time_range(
        start_time: datetime | None,
        end_time: datetime | None,
        field_name: str
) -> tuple[str | None, tuple[datetime | None, datetime | None] | None]:
    """
    Validates a given time range, ensuring that both start and end times are provided and that the start time
    is not later than the end time.

    :param start_time: The start time of the range.
    :param end_time: The end time of the range.
    :param field_name: The name of the field being validated, used in error messages.
    :return: A tuple where the first element is an error message (or None if valid),
             and the second element is a tuple of valid start and end times (or None if invalid).
    """
    if start_time is None or end_time is None:
        return f'{field_name.capitalize()} requires both start and end times', None

    # Check if start time is earlier than or equal to end time
    if start_time > end_time:
        return (f'{field_name.capitalize()} must have a start time earlier than or equal to the end time - '
                f'{format_datetime(start_time)} > {format_datetime(end_time)}'), None

    # If all validations pass, return the valid range
    return None, (start_time, end_time)


def validate_parameter_values(run: CalibrationRun, parameters: list[dict[str, str | float]]) -> tuple[list[str], list[str]]:
    """
    Validate user-specified parameter values against the parameters available for this run.

    Returns (errors, warnings):
      - errors: invalid module names or invalid parameter names for a valid module
      - warnings: initial_value outside [minimum, maximum] when all three values are provided

    :param run: CalibrationRun being validated.
    :param parameters: List of parameter dicts (expects keys: module, name, minimum, maximum, initial_value).
    :return: (error_messages, warning_messages)
    """
    if not parameters:
        return [], []

    # Retrieve all parameters for this run with only the fields we need
    existing_parameters = list(
        CalibrationParameter.objects.filter(
            calibration_formulation__calibration_run=run
        ).values('name', 'calibration_formulation__module_id')
    )

    # Get cached modules keyed by ID
    modules_by_id = get_cached_modules_by_id()

    # Build lookup dict: (module_name, parameter_name) → CalibrationParameter (as dict)
    parameter_lookup = {
        (modules_by_id[p['calibration_formulation__module_id']].name, p['name']): p
        for p in existing_parameters
    }

    # Validate provided parameters
    invalid_parameters = []
    invalid_modules = []
    value_out_of_bounds = []

    # Validate each provided parameter
    for p in parameters:
        module_name: str = p['module']
        key = (module_name, p['name'])
        if key not in parameter_lookup:
            # Check if module is valid
            if get_cached_module_by_name(module_name):
                invalid_parameters.append(key)
            else:
                invalid_modules.append(key)
        else:
            min_val = p.get('minimum')
            max_val = p.get('maximum')
            initial = p.get('initial_value')

            # Only check range if all values are provided
            if min_val is not None and max_val is not None and initial is not None:
                if not (min_val <= initial <= max_val):
                    msg = (
                        f"Initial value {initial} for parameter '{p['name']}' in module '{module_name}' "
                        f"is outside the range [{min_val}, {max_val}]"
                    )
                    logger.warning(msg)
                    value_out_of_bounds.append(msg)

    # Construct messages
    error_messages = []
    warning_messages = []

    if invalid_parameters:
        error_messages.extend(
            f"Invalid parameter '{name}' for module '{module}'"
            for module, name in invalid_parameters
        )
    if invalid_modules:
        error_messages.extend(
            f"Invalid module '{module}' for parameter '{name}'"
            for module, name in invalid_modules
        )
    if value_out_of_bounds:
        for m in value_out_of_bounds:
            logger.warning(m)
        warning_messages.extend(value_out_of_bounds)

    return error_messages, warning_messages


def save_parameters(run: CalibrationRun, parameters: list[dict[str, str | float]], allow_nulls: bool = False) -> None:
    """
    Saves or updates calibration parameters for a given calibration run.
    Turns off user_selected_for_tuning for parameters not in the new list.

    :param run: The calibration run being updated.
    :param parameters: A list of dictionaries containing parameter details.
    :param allow_nulls: Determines how missing values are handled:
        - If `allow_nulls` is False (default), user-provided values override the Data Services defaults,
          even if some values are missing.
        - If `allow_nulls` is True, user-provided values override the defaults only if they are not None,
          allowing missing values to retain their defaults.
    """
    # Handle the case where the user clears all parameters
    if not parameters:
        # If no parameters are provided, turn off all user_selected_for_tuning flags
        CalibrationParameter.objects.filter(
            calibration_formulation__calibration_run=run,
            user_selected_for_tuning=True
        ).update(user_selected_for_tuning=False)
        return

    # Fetch all parameters for this run efficiently
    existing_parameters = list(
        CalibrationParameter.objects.filter(
            calibration_formulation__calibration_run=run
        ).values(
            'id', 'name', 'minimum', 'maximum', 'initial_value',
            'user_selected_for_tuning', 'calibration_formulation__module_id'
        )
    )

    modules_by_id = get_cached_modules_by_id()

    # Build lookup keyed by (module_name, parameter_name)
    parameter_lookup = {
        (modules_by_id[p['calibration_formulation__module_id']].name, p['name']): p
        for p in existing_parameters
    }

    selected_for_tuning = {(p['module'], p['name']) for p in parameters}
    parameters_to_update = []

    # Update existing parameters
    for p in parameters:
        key = (p['module'], p['name'])
        existing = parameter_lookup.get(key)
        if not existing:
            continue  # Ignore unknown parameters

        updates = {}
        if allow_nulls:
            if p.get('minimum') is not None:
                updates['minimum'] = p['minimum']
            if p.get('maximum') is not None:
                updates['maximum'] = p['maximum']
            if p.get('initial_value') is not None:
                updates['initial_value'] = p['initial_value']
        else:
            updates['minimum'] = p.get('minimum')
            updates['maximum'] = p.get('maximum')
            updates['initial_value'] = p.get('initial_value')

        if updates:
            updates['user_selected_for_tuning'] = True
            updates['id'] = existing['id']
            parameters_to_update.append(updates)

    # Bulk update selected parameters
    if parameters_to_update:
        CalibrationParameter.objects.bulk_update(
            [
                CalibrationParameter(
                    id=p['id'],
                    minimum=p.get('minimum'),
                    maximum=p.get('maximum'),
                    initial_value=p.get('initial_value'),
                    user_selected_for_tuning=True,
                )
                for p in parameters_to_update
            ],
            ['minimum', 'maximum', 'initial_value', 'user_selected_for_tuning']
        )

    # Turn off tuning flag for unselected parameters
    unselected_ids = [
        p['id']
        for p in existing_parameters
        if (modules_by_id[p['calibration_formulation__module_id']].name, p['name']) not in selected_for_tuning
           and p['user_selected_for_tuning']
    ]
    if unselected_ids:
        CalibrationParameter.objects.filter(id__in=unselected_ids).update(user_selected_for_tuning=False)


def _as_local_path(path: str) -> str:
    """
    Convert a file:// URL into a local filesystem path.
    For example: file:///ngencerf/data/file.csv -> /ngencerf/data/file.csv
    Leaves non-file URLs unchanged.
    """
    if path.startswith("file://"):
        return urlparse(path).path
    return path


# TODO This is only used to read Forcing iles from S3.  We can get rid of this once we use BMI forcing.  We can also get rid of localize_to_path
def get_csv_daterange(path: str) -> DateTimeRange:
    """
    Reads a CSV file (local or cloud) that is assumed to be sorted by date/time and efficiently determines
    the min and max date values from the first column. Uses caching for remote files so that later operations
    (e.g., copying/subsetting) can reuse the same local file without re-downloading.

    :param path: The file path or cloud URL to the CSV file.
    :return: DateTimeRange representing the min and max datetime values from the file.
    :raises CerfException: If the file does not exist, contains invalid datetime values, or encounters a read error.
    """
    try:
        # Always cache remote files, so subsequent uses don't re-download
        with cloud_util.localize_to_path(path, enable_cache=True, suffix=".csv") as (orig, local_path):
            local_path = _as_local_path(local_path)  # ✅ ensure usable by os.path and open()

            if not os.path.exists(local_path):
                raise CerfException(f"File {path} does not exist")

            # Read first data row (skip header)
            with open(local_path, "r", encoding="utf-8") as f:
                _ = f.readline()  # skip header
                first_line = f.readline()
            if not first_line:
                raise CerfException(f"File {path} does not contain data rows")
            first_time = pd.to_datetime(first_line.strip().split(',', 1)[0], errors="coerce")

            # Read last line efficiently
            try:
                with open(local_path, "rb") as f:
                    f.seek(-2, os.SEEK_END)
                    while f.read(1) != b"\n":
                        f.seek(-2, os.SEEK_CUR)
                    last_line = f.readline().decode("utf-8").strip()
            except OSError:
                # For very small files, fall back to reading all lines
                with open(local_path, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
                    if len(lines) < 2:
                        raise CerfException(f"File {path} does not contain data rows")
                    last_line = lines[-1].strip()

            last_time = pd.to_datetime(last_line.split(",", 1)[0], errors="coerce")

            if pd.isna(first_time) or pd.isna(last_time):
                raise CerfException(f"Invalid datetime values found in {path}")

            # Ensure timestamps are UTC
            return DateTimeRange(
                first_time.replace(tzinfo=timezone.utc),
                last_time.replace(tzinfo=timezone.utc),
            )

    except Exception as e:
        logger.error(f"Error while processing file {path}: {e}")
        raise CerfException(f"Error reading file {path}: {e}")


def get_forcing_date_range(forcing_dir_path: str) -> DateTimeRange | None:
    """
    Computes the encompassing date range for all valid CSV files in a given directory.
    Supports both local paths and cloud URLs.

    :param forcing_dir_path: Directory path or cloud URL containing forcing data files.
    :return: DateTimeRange covering all CSV files, or None if no files found.
    """
    csv_files = cloud_util.list_files(forcing_dir_path, pattern="*.csv")
    if not csv_files:
        return None

    # Use ThreadPoolExecutor for parallel processing
    with ThreadPoolExecutor(max_workers=min(32, (os.cpu_count() or 1) + 4)) as executor:
        ranges = list(executor.map(get_csv_daterange, csv_files))

    # Combine all individual ranges into a single encompassing range
    timerange = None
    for r in ranges:
        timerange = timerange.encompass(r) if timerange else r
    return timerange


def get_date_range_intersection(run: CalibrationRun, forcing_dir_path: str = None) -> DateTimeRange | None:
    """
    Calculates the intersection of date ranges between observational and forcing data.
    Supports both local paths and cloud URLs.

    :param run Calibration Run
    :param forcing_dir_path: Directory path or cloud URL containing forcing data.
    :return: DateTimeRange representing the overlapping period, or None if no overlap.
    """
    # Calculate the date range for the observational data
    obs_range = get_observational_date_range_from_data_services(run)
    logger.debug(f"obs_range: {obs_range}")

    # Calculate the date range for the forcing data
    forcing_range = get_forcing_date_range(forcing_dir_path) if forcing_dir_path else settings.FORCING_BMI_DATE_RANGE
    logger.debug(f"forcing_range: {forcing_range}")

    # Compute the intersection of the two ranges
    if obs_range and forcing_range:
        start_time = max(obs_range.start_datetime, forcing_range.start_datetime)
        end_time = min(obs_range.end_datetime, forcing_range.end_datetime)
        if start_time <= end_time:
            return DateTimeRange(start_time, end_time)
    return None


def validate_parameter_rules(
        *,
        module_names_for_job: set[str],
        selected_module_names: set[str],
        have_LSTM_flag: bool,
        has_any_params: bool,
        error_object: ErrorReport
) -> None:
    """
    Enforce parameter selection rules based on the modules included in the job.

    Rules:
      - If LSTM is present: no parameters may be selected.
      - If Topoflow-Glacier is present: at least one Topoflow-Glacier parameter must be selected.
        If any other non-Topoflow modules are present: at least one non-Topoflow parameter must also be selected.
      - Otherwise: at least one parameter must be selected.

    :param module_names_for_job: Module names included in the job.
    :param selected_module_names: Module names with >= 1 selected parameter.
    :param have_LSTM_flag: True if the job includes LSTM.
    :param has_any_params: True if any parameters are selected.
    :param error_object: ErrorReport to receive warnings/errors.
    :return: None.
    """
    TOPOFLOW = "Topoflow-Glacier"

    has_topoflow = TOPOFLOW in module_names_for_job
    has_non_topoflow_modules = any(name != TOPOFLOW for name in module_names_for_job)

    # Parameter selection rules:
    # - If LSTM is present: MUST have zero selected parameters.
    # - If Topoflow-Glacier is in the job: must select >=1 Topoflow-Glacier parameter.
    #   If any other modules are also in the job: must also select >=1 non-Topoflow-Glacier parameter.
    # - Otherwise (no LSTM, no Topoflow-Glacier): must select >=1 parameter overall.
    if have_LSTM_flag:
        if has_any_params:
            error_object.add_warning("LSTM jobs must not specify any calibration parameters")
        return

    # No params selected at all.
    if not has_any_params:
        if has_topoflow:
            if has_non_topoflow_modules:
                error_object.add_warning(
                    "At least one Topoflow-Glacier parameter and at least one non-Topoflow-Glacier parameter must be specified"
                )
            else:
                error_object.add_warning("At least one Topoflow-Glacier parameter must be specified")
        else:
            error_object.add_warning("At least one parameter must be specified")
        return

    # Parameters selected — ensure they cover required module categories
    has_topoflow_param = TOPOFLOW in selected_module_names
    has_non_topoflow_param = any(name != TOPOFLOW for name in selected_module_names)

    if has_topoflow and not has_topoflow_param:
        error_object.add_warning("At least one Topoflow-Glacier parameter must be specified")

    if has_topoflow and has_non_topoflow_modules and not has_non_topoflow_param:
        error_object.add_warning("At least one non-Topoflow-Glacier parameter must be specified")
