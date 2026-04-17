import json
import os
from pathlib import Path

from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import StatusEnum, LogCategory, ValidationType
from calibration.models import CalibrationRun
from calibration.util.calibration_validators import CalibrationOrValidationOrColdStartOrForecastOrHindcastOrVerificationRunIdSerializer, \
    GetLogNamesResponseSerializer, ErrorResponseSerializer, GetLogRequestSerializer, GetLogsResponseSerializer, GetLogStatusRequestSerializer, \
    GetLogStatusResponseSerializer
from calibration.util.ngen_locations import get_validation_control_stdout_file, get_validation_best_stdout_file, get_validation_iteration_stdout_file, \
    get_forecast_ngen_log_dir, get_ngen_log_dir, get_hindcast_ngen_log_dir, \
    get_output_validation_run_dir, get_forecast_dir, get_cold_start_dir, get_calibration_ngen_logs, \
    get_output_calibration_run_dir, get_gage_dir, get_verification_run_dir, get_hindcast_dir, get_cold_start_ngen_log_dir
from calibration.views.calibration_evaluation_views import logger
from calibration.views.calibration_run_views import map_path_to_host
from calibration.views.called_from import get_caller_name
from calibration.views.common import get_validation_run, get_forecast_run, get_verification_run, get_calibration_run, handle_exceptions, \
    get_user_email, validate_request, validate_response, get_elapsed_str, CerfException, truncate_large_fields, get_hindcast_run, \
    find_validation_worker_with_matching_id, worker_directory_pattern


@extend_schema(
    request=CalibrationOrValidationOrColdStartOrForecastOrHindcastOrVerificationRunIdSerializer,
    responses={
        200: GetLogNamesResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Retrieve available log names for a calibration, validation, forecast, or verification run"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_log_names(request: Request) -> Response:
    """
    Retrieves a list of available log names for a specific calibration, validation, forecast, or verification run.

    - Handles request validation and user permissions.
    - Returns log files as a list where each entry is a single-key object
      mapping one log category to its list of log files.
    - Within each category, files are sorted by filename only, not full path.

    :param request: The HTTP request object containing one run ID.
    :return: JSON response with log names or error details.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationOrValidationOrColdStartOrForecastOrHindcastOrVerificationRunIdSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    validation_run_id = validator.get('validation_run_id')
    forecast_run_id = validator.get('forecast_run_id')
    hindcast_run_id = validator.get('hindcast_run_id')
    verification_run_id = validator.get('verification_run_id')

    logs_by_category, error_return = get_allowed_logs_for_request(
        calibration_run_id=calibration_run_id,
        validation_run_id=validation_run_id,
        forecast_run_id=forecast_run_id,
        hindcast_run_id=hindcast_run_id,
        verification_run_id=verification_run_id,
        user=request.user,
    )
    if error_return:
        return error_return

    category_order = {
        LogCategory.GENERAL.value: 0,
        LogCategory.VALIDATION.value: 1,
        LogCategory.CALIBRATION.value: 2,
        LogCategory.COLD_START.value: 3,
        LogCategory.FORECAST.value: 4,
        LogCategory.VERIFICATION.value: 5,
    }

    sorted_categories = sorted(
        logs_by_category.items(),
        key=lambda item: (category_order.get(item[0], 999), item[0].lower())
    )

    response = {
        'log_names': [
            {
                log_category: sorted(
                    log_paths,
                    key=lambda path: os.path.basename(path).lower()
                )
            }
            for log_category, log_paths in sorted_categories
            if log_paths
        ]
    }

    response_validator, error_response = validate_response(GetLogNamesResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(response_validator.data)}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=GetLogRequestSerializer,
    responses={
        200: GetLogsResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Retrieve a specific allowed log file with pagination support"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_log(request: Request) -> Response:
    """
    Retrieves a specific allowed log file for a calibration, validation, forecast, or verification run.

    - Supports pagination for large log files.
    - Validates that the requested log path is one of the allowed logs
      for the authenticated user and requested run.
    - A cold start log may also be returned when it is associated with
      the requested forecast run.

    :param request: The HTTP request object containing run and log information.
    :return: JSON response with log file content or error details.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(GetLogRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    validation_run_id = validator.get('validation_run_id')
    forecast_run_id = validator.get('forecast_run_id')
    hindcast_run_id = validator.get('hindcast_run_id')
    verification_run_id = validator.get('verification_run_id')
    log_name = validator.get('log_name')
    start = validator.get('start')
    limit = validator.get('limit')

    logs, error_return = get_allowed_logs_for_request(
        calibration_run_id=calibration_run_id,
        validation_run_id=validation_run_id,
        forecast_run_id=forecast_run_id,
        hindcast_run_id=hindcast_run_id,
        verification_run_id=verification_run_id,
        user=request.user,
    )
    if error_return:
        return error_return

    requested_log_name = normalize_log_path(log_name)
    allowed_logs = {
        path
        for paths in logs.values()
        for path in paths
    }

    if requested_log_name not in allowed_logs:
        raise CerfException(f"Log file not valid for this run: {log_name}")

    # Check if the log file exists
    if not os.path.exists(requested_log_name):
        raise CerfException(f"Log file not found: {requested_log_name}")

    # Get the file size in bytes
    file_size = os.path.getsize(requested_log_name)

    # Count the total number of lines in the file for pagination metadata
    with open(requested_log_name, 'r') as f:
        total_lines = sum(1 for _ in f)

    # Read the requested lines from the log file with null replacement
    with open(requested_log_name, 'r') as file:
        all_lines = [line.replace('\x00', ' ') for line in file]

    if start == -1:
        # Just get the last 'limit' lines
        paginated_lines = all_lines[-limit:]
    else:
        paginated_lines = all_lines[start:start + limit]

    pagination_metadata = {
        'start': start,
        'limit': limit,
        'count': total_lines
    }

    response = {
        'message': f"Log file {requested_log_name} retrieved",
        'log_data': paginated_lines,
        'log_name': map_path_to_host(requested_log_name),
        'byte_offset': file_size,
        'pagination_metadata': pagination_metadata,
        # 'status': get_status_name_for_log(ctx, log_category),
    }

    response_validator, error_response = validate_response(
        GetLogsResponseSerializer,
        response,
        fields_to_truncate=['log_data'], max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["log_data"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=GetLogStatusRequestSerializer,
    responses={
        200: GetLogStatusResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Check whether a specific allowed log file has changed"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_log_status(request: Request) -> Response:
    """
    Checks whether a specific allowed log file has been updated since it was last requested.

    - Validates that the requested log path is one of the allowed logs
      for the authenticated user and requested run.
    - Uses byte_offset to compare the previously returned file size
      to the current file size on disk.

    :param request: The HTTP request object containing run and log information.
    :return: JSON response indicating whether the log file has changed.

    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(GetLogStatusRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    validation_run_id = validator.get('validation_run_id')
    forecast_run_id = validator.get('forecast_run_id')
    hindcast_run_id = validator.get('hindcast_run_id')
    verification_run_id = validator.get('verification_run_id')
    # log_category = LogCategory(validator.get('log_category'))
    log_name = validator.get('log_name')
    byte_offset = validator.get('byte_offset')

    logs, error_return = get_allowed_logs_for_request(
        calibration_run_id=calibration_run_id,
        validation_run_id=validation_run_id,
        forecast_run_id=forecast_run_id,
        hindcast_run_id=hindcast_run_id,
        verification_run_id=verification_run_id,
        user=request.user,
    )
    if error_return:
        return error_return

    requested_log_name = normalize_log_path(log_name)
    allowed_logs = {
        path
        for paths in logs.values()
        for path in paths
    }

    if requested_log_name not in allowed_logs:
        raise CerfException(f"Log file not valid for this run: {log_name}")

    # Get the file size in bytes
    file_size = os.path.getsize(requested_log_name) if os.path.exists(requested_log_name) else 0

    response = {
        'message': f"Log file {map_path_to_host(requested_log_name)} has " +
                   ("changed" if file_size != byte_offset else "not changed"),
        'file_updated': (file_size != byte_offset),
        # 'status': get_status_name_for_log(ctx, log_category)
    }

    response_validator, error_response = validate_response(GetLogStatusResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}'
    )
    return Response(response_validator.data)


def resolve_log_context(
        *,
        calibration_run_id: int | None,
        validation_run_id: int | None,
        forecast_run_id: int | None,
        hindcast_run_id: int | None,
        verification_run_id: int | None,
        user,
):
    """
    Resolves the requested run context for log-related endpoints.

    :return:
        A tuple of (ctx, error_return), where ctx contains:
        - calibration_run
        - validation_run
        - forecast_run
        - hindcast_run
        - cold_start_run
        - verification_run
    """
    ACTIVE_STATUSES = [
        StatusEnum.RUNNING,
        StatusEnum.SUBMITTED,
        StatusEnum.DONE,
        StatusEnum.FAILED,
        StatusEnum.CANCELLED,
        StatusEnum.SERVER_ERROR,
    ]

    validation_run = None
    forecast_run = None
    hindcast_run = None
    cold_start_run = None
    verification_run = None

    if validation_run_id:
        validation_run, error_return = get_validation_run(
            validation_run_id,
            user,
            run_status=ACTIVE_STATUSES,
        )
        if error_return:
            return None, error_return
        assert validation_run is not None

        calibration_run = validation_run.calibration_run

    elif forecast_run_id:
        forecast_run, error_return = get_forecast_run(
            forecast_run_id,
            user,
            # Allow SAVED in case we are looking for cold start logs
            run_status=[*ACTIVE_STATUSES, StatusEnum.SAVED],
        )
        if error_return:
            return None, error_return
        assert forecast_run is not None

        calibration_run = forecast_run.calibration_run
        cold_start_run = forecast_run.cold_start_run

    elif hindcast_run_id:
        hindcast_run, error_return = get_hindcast_run(
            hindcast_run_id,
            user,
            # Allow SAVED in case we are looking for cold start logs
            run_status=[*ACTIVE_STATUSES, StatusEnum.SAVED],
        )
        if error_return:
            return None, error_return
        assert hindcast_run is not None

        calibration_run = hindcast_run.calibration_run
        cold_start_run = hindcast_run.cold_start_run

    elif verification_run_id:
        verification_run, error_return = get_verification_run(
            verification_run_id,
            user,
            run_status=ACTIVE_STATUSES,
        )
        if error_return:
            return None, error_return
        assert verification_run is not None

        calibration_run = verification_run.forecast_run.calibration_run

    else:
        assert calibration_run_id is not None
        calibration_run, error_return = get_calibration_run(
            calibration_run_id,
            user,
            run_status=ACTIVE_STATUSES,
        )
        if error_return:
            return None, error_return

    return {
        "calibration_run": calibration_run,
        "validation_run": validation_run,
        "forecast_run": forecast_run,
        "hindcast_run": hindcast_run,
        "cold_start_run": cold_start_run,
        "verification_run": verification_run,
    }, None


def get_allowed_logs_for_request(
        *,
        calibration_run_id: int | None,
        validation_run_id: int | None,
        forecast_run_id: int | None,
        hindcast_run_id: int | None,
        verification_run_id: int | None,
        user,
) -> tuple[dict[str, list[str]] | None, Response | None]:
    """
    Build the set of log files the authenticated user is allowed to access
    for the specified run.

    - Resolves the requested run in user context.
    - Collects the applicable logs for that run.
    - Normalizes all returned paths to canonical absolute paths before returning.

    :return:
        A tuple of (logs, error_return), where logs is a dict keyed by
        log category value and each value is a list of normalized absolute paths.
    """
    ctx, error_return = resolve_log_context(
        calibration_run_id=calibration_run_id,
        validation_run_id=validation_run_id,
        forecast_run_id=forecast_run_id,
        hindcast_run_id=hindcast_run_id,
        verification_run_id=verification_run_id,
        user=user,
    )
    if error_return:
        return None, error_return

    calibration_run = ctx["calibration_run"]
    validation_run = ctx["validation_run"]
    forecast_run = ctx["forecast_run"]
    hindcast_run = ctx["hindcast_run"]
    cold_start_run = ctx["cold_start_run"]
    verification_run = ctx["verification_run"]

    logs: dict[str, list[str]] = {}

    if validation_run:
        logs[LogCategory.GENERAL.value] = get_general_logs(calibration_run)
        logs[LogCategory.CALIBRATION.value] = get_calibration_logs(validation_run.calibration_run)

        # Collect only the logs for this specific validation run.
        validation_logs: list[str] = []
        validation_type = ValidationType(validation_run.validation_type)

        if validation_type == ValidationType.VALID_CONTROL:
            file = get_validation_control_stdout_file(calibration_run)
            if os.path.exists(file):
                validation_logs.append(file)

        elif validation_type == ValidationType.VALID_BEST:
            file = get_validation_best_stdout_file(calibration_run)
            if os.path.exists(file):
                validation_logs.append(file)

        else:
            file = get_validation_iteration_stdout_file(
                calibration_run,
                validation_run.worker_name,
                validation_run.iteration_num
            )
            if os.path.exists(file):
                validation_logs.append(file)

        # Validation-specific logs are stored in the matching worker directory,
        # both directly under the worker and in its logs subdirectory.
        matching_worker = find_validation_worker_with_matching_id(
            validation_run,
            worker_name=validation_run.worker_name if validation_type == ValidationType.VALID_ITERATION else None,
            iteration_num=validation_run.iteration_num if validation_type == ValidationType.VALID_ITERATION else None,
        )
        if matching_worker:
            validation_run_dir = get_output_validation_run_dir(calibration_run)
            worker_dir = os.path.join(validation_run_dir, matching_worker)

            # Log files directly in the worker directory
            validation_logs.extend(get_log_files_in_directory(worker_dir))

            # Log files in the worker's logs subdirectory
            worker_logs_dir = os.path.join(worker_dir, 'logs')
            validation_logs.extend(get_log_files_in_directory(worker_logs_dir))

        # --- Just in case there are any dups ---
        logs[LogCategory.VALIDATION.value] = validation_logs

    elif forecast_run:
        forecast_logs = []
        forecast_dir = get_forecast_dir(forecast_run)
        forecast_logs.extend(get_log_files_in_directory(forecast_dir))

        ngen_log_dir = get_forecast_ngen_log_dir(forecast_run)
        forecast_logs.extend(get_log_files_in_directory(ngen_log_dir))
        logs[LogCategory.FORECAST.value] = forecast_logs

        if cold_start_run:
            cold_start_dir = get_cold_start_dir(cold_start_run)
            cold_start_logs = []
            cold_start_logs.extend(get_log_files_in_directory(cold_start_dir))

            ngen_log_dir = get_cold_start_ngen_log_dir(cold_start_run)
            cold_start_logs.extend(get_log_files_in_directory(ngen_log_dir))
            logs[LogCategory.COLD_START.value] = cold_start_logs

    elif hindcast_run:
        hindcast_logs = []
        hindcast_dir = get_hindcast_dir(hindcast_run)
        hindcast_logs.extend(get_log_files_in_directory(hindcast_dir))

        ngen_log_dir = get_hindcast_ngen_log_dir(hindcast_run)
        hindcast_logs.extend(get_log_files_in_directory(ngen_log_dir))
        logs[LogCategory.HINDCAST.value] = hindcast_logs

        if cold_start_run:
            cold_start_dir = get_cold_start_dir(cold_start_run)
            cold_start_logs = []
            cold_start_logs.extend(get_log_files_in_directory(cold_start_dir))

            ngen_log_dir = get_cold_start_ngen_log_dir(cold_start_run)
            cold_start_logs.extend(get_log_files_in_directory(ngen_log_dir))
            logs[LogCategory.COLD_START.value] = cold_start_logs

    elif verification_run:
        verification_logs = []
        verification_run_dir = get_verification_run_dir(verification_run)
        verification_logs.extend(get_log_files_in_directory(verification_run_dir))

        logs[LogCategory.VERIFICATION.value] = verification_logs

    else:
        logs[LogCategory.GENERAL.value] = get_general_logs(calibration_run)
        logs[LogCategory.CALIBRATION.value] = get_calibration_logs(calibration_run)
        logs[LogCategory.VALIDATION.value] = get_all_validation_logs(calibration_run)

    normalized_logs = {
        category: [normalize_log_path(path) for path in paths]
        for category, paths in logs.items()
    }

    return normalized_logs, None


def get_general_logs(calibration_run: CalibrationRun) -> list[str]:
    """
    Collect general log files for a calibration run.

    Includes any *.log files found in:
    - the gage directory
    - the bootstrap ngen log directory associated with the calibration run

    :param calibration_run: The calibration run whose general logs should be collected.
    :return: A list of log file paths.
    """
    logs = []

    gage_dir = get_gage_dir(calibration_run)
    logs.extend(get_log_files_in_directory(gage_dir))

    bootstrap_ngen_log_dir = get_ngen_log_dir(calibration_run)
    logs.extend(get_log_files_in_directory(bootstrap_ngen_log_dir))

    return logs


def get_calibration_logs(calibration_run: CalibrationRun) -> list[str]:
    """
    Collect calibration-specific log files for a calibration run.

    Includes any *.log files found in:
    - the calibration ngen log directory
    - the calibration output directory

    :param calibration_run: The calibration run whose calibration logs should be collected.
    :return: A list of log file paths.
    """
    logs = []

    ngen_log_dir = get_calibration_ngen_logs(calibration_run)
    logs.extend(get_log_files_in_directory(ngen_log_dir))

    calibration_run_dir = get_output_calibration_run_dir(calibration_run)
    logs.extend(get_log_files_in_directory(calibration_run_dir))

    return logs


def get_all_validation_logs(calibration_run: CalibrationRun) -> list[str]:
    """
    Collect available validation log files associated with a calibration run.

    Includes:
    - any *.log files found directly in the Validation_Run directory
    - any *.log files found directly in each worker directory under Validation_Run
    - any *.log files found in the `logs` subdirectory of each worker under Validation_Run

    :param calibration_run: The calibration run whose validation logs should be collected.
    :return: A list of log file paths.
    """
    logs = []

    # Top-level validation logs (for example stdout logs) in Validation_Run
    validation_run_dir = get_output_validation_run_dir(calibration_run)
    logs.extend(get_log_files_in_directory(validation_run_dir))

    # Worker-specific logs
    if validation_run_dir and os.path.exists(validation_run_dir):
        for item in os.listdir(validation_run_dir):
            worker_dir = os.path.join(validation_run_dir, item)
            if os.path.isdir(worker_dir) and worker_directory_pattern.match(item):
                # Log files directly in the worker directory
                logs.extend(get_log_files_in_directory(worker_dir))

                # Log files in the worker's logs subdirectory
                worker_logs_dir = os.path.join(worker_dir, 'logs')
                logs.extend(get_log_files_in_directory(worker_logs_dir))

    return logs


def normalize_log_path(path: str) -> str:
    """
    Normalizes a log path to a canonical absolute path for reliable comparison.

    - Resolves relative segments such as '.' and '..'.
    - Uses strict=False so normalization does not fail solely because the file
      does not exist at the time of normalization.

    :param path: The input filesystem path.
    :return: A normalized absolute path string.
    """
    return str(Path(path).resolve(strict=False))


def get_log_files_in_directory(directory: str) -> list[str]:
    """
    Returns all *.log files in the given directory.

    :param directory: Directory to search.
    :return: List of log file paths as strings.
    """
    base_path = Path(directory)

    files = [str(p) for p in base_path.glob("*.log")]
    logger.info(f"Found {len(files)} log file(s) in {directory}")
    return files
