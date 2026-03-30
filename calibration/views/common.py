import base64
import inspect
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import timedelta, datetime
from functools import wraps
from typing import Type, Any, Callable

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction, connection
from django.db.models import QuerySet
from django.http import JsonResponse
from rest_framework import status
from rest_framework.decorators import permission_classes
from rest_framework.exceptions import ValidationError, ParseError
from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import AccessToken

from calibration.enums import StatusEnum, ValidationType, JobGenesis, NgenLogging
from calibration.models import CalibrationRun, ValidationRun, ForecastConfiguration, ForecastRun, ColdStartRun, \
    CalibrationFormulation, VerificationRun
from calibration.models import Iteration
from calibration.models.base_run import BaseRun
from calibration.models.hindcast_run import HindcastRun
from calibration.util.caching import get_cached_modules_by_id
from calibration.util.calibration_validators import ErrorResponseSerializer, BaseSerializer
from calibration.util.cloud_util import path_exists
from calibration.util.ngen_locations import get_forecast_dir, get_output_calibration_run_dir, \
    get_output_validation_run_dir, get_cold_start_dir, get_ngen_logging_file, \
    get_ngen_logging_basename, get_verification_run_dir, \
    get_hindcast_dir

logger = logging.getLogger(__name__)

SLOTH = 'SLoTH'

User = get_user_model()


def validate_run_instance(
        run: BaseRun,
        run_id: int,
        run_status: list[StatusEnum] | None,
        is_archived_field: str,
        include_archived: bool,
        model_name: str,
) -> Response | None:
    run_status = run_status or [StatusEnum.READY, StatusEnum.SAVED]
    allowed_statuses = [s.db_instance for s in run_status]

    is_archived = getattr(run, is_archived_field, False)
    if is_archived and not include_archived:
        return ResponseError(
            f'{model_name} {run_id} is archived and should be unarchived before additional operations can be performed.'
        )

    if run.status not in allowed_statuses:
        allowed_names = [s.name for s in allowed_statuses]
        return ResponseError(
            f'{model_name} {run_id} is not in an allowed status: '
            f'{join_with_or(allowed_names)}. '
            f'Current status: {run.status.name}'
        )

    return None


def get_calibration_runs_bulk(
        calibration_run_ids: list[int],
        user: User | None,
        run_status: list[StatusEnum] | None = None,
        include_archived: bool = False,
) -> tuple[dict[int, CalibrationRun], dict[int, Response]]:
    """
    Bulk retrieve and validate multiple CalibrationRun instances by ID.

    This function performs a single database query to fetch all requested
    CalibrationRun objects, then applies the same validation logic used by
    `get_run_instance` on a per-run basis, including:

    - Optional filtering by owning user (owner_field='owner')
    - Validation of allowed job statuses
    - Enforcement of archived-job access rules (is_archived_field='is_archived')

    The database query itself does NOT filter out archived jobs. Instead, archive
    handling is enforced explicitly during validation so that callers can
    distinguish between:
      - non-existent runs
      - unauthorized access
      - disallowed status
      - archived-but-disallowed runs

    Validation is performed independently for each requested run ID. Results
    preserve per-ID error semantics by returning two mappings:
      - valid runs keyed by run ID
      - error responses keyed by run ID

    This function does not mutate or delete any data.

    :param calibration_run_ids: List of CalibrationRun IDs to retrieve and validate.
                                Order is preserved when iterating results.
    :param user: The user requesting the runs. If provided, only runs owned by
                 this user are considered valid.
    :param run_status: Optional list of allowed StatusEnum values. Defaults to
                       [READY, SAVED] if not provided.
    :param include_archived: Whether archived jobs are allowed. If False, archived
                             runs will return an error response.
    :return: A tuple (runs_by_id, errors_by_id):
             - runs_by_id: dict mapping run_id -> CalibrationRun for all valid runs
             - errors_by_id: dict mapping run_id -> ResponseError for invalid runs
    """
    qs = CalibrationRun.objects.filter(id__in=calibration_run_ids)

    if user:
        qs = qs.filter(owner=user)

    runs = {run.id: run for run in qs}

    runs_by_id: dict[int, CalibrationRun] = {}
    errors_by_id: dict[int, Response] = {}

    for run_id in calibration_run_ids:
        run = runs.get(run_id)
        model_name = "Calibration Job"

        if not run:
            user_info = f' or is not owned by {user.email}' if user else ''
            errors_by_id[run_id] = ResponseError(
                f'{model_name} {run_id} does not exist{user_info}'
            )
            continue

        error = validate_run_instance(
            run=run,
            run_id=run_id,
            run_status=run_status,
            is_archived_field='is_archived',
            include_archived=include_archived,
            model_name=model_name,
        )

        if error:
            errors_by_id[run_id] = error
        else:
            runs_by_id[run_id] = run

    return runs_by_id, errors_by_id


def get_run_instance(
        model: Type[BaseRun],
        run_id: int,
        user: User | None,
        run_status: list[StatusEnum] | None = None,
        owner_field: str = 'owner',
        is_archived_field: str = 'is_archived',
        include_archived: bool = False,
        *,
        select_related_fields: tuple[str, ...] = (),
) -> tuple[BaseRun | None, Response | None]:
    """
    Retrieve and validate a single BaseRun-derived instance by ID.

    This function performs a database lookup for the specified run ID and applies
    common validation logic used across all job types (Calibration, Validation,
    Forecast, Cold Start, Verification), including:

    - Optional filtering by owning user
    - Validation of allowed job statuses
    - Enforcement of archived-job access rules
    - Optional eager-loading of related objects via select_related

    The database query itself does NOT filter out archived jobs. Instead, archive
    handling is enforced explicitly via validation logic so that callers can
    distinguish between:
      - non-existent runs
      - unauthorized access
      - disallowed status
      - archived-but-disallowed runs

    This function does not mutate or delete any data.

    :param model: The BaseRun-derived model class to query.
    :param run_id: The ID of the run to retrieve.
    :param user: The user requesting the run; if None, no filtering by owner is done.
    :param run_status: Optional list of StatusEnum members to filter by.  Defaults to
                       [READY, SAVED] if not provided.
    :param owner_field: The field used to filter by owner (default 'owner').
    :param is_archived_field: ame of the boolean field indicating archived state.
                              May traverse relationships. (default 'is_archived')
    :param include_archived: Whether to include archived jobs.  If False, archived runs will return an error response.
    :param select_related_fields: Optional tuple of related field names to eagerly
                                  load via select_related.
    :return: A tuple (run, error):
             - run: The retrieved model instance, or None if not found
             - error: A ResponseError if validation fails, otherwise None
    """
    run_status = run_status or [StatusEnum.READY, StatusEnum.SAVED]

    # Query without filtering out archived jobs
    query: QuerySet = model.objects.filter(id=run_id)

    if select_related_fields:
        query = query.select_related(*select_related_fields)

    if user:
        query = query.filter(**{f"{owner_field}": user})

    try:
        run = query.get()
    except model.DoesNotExist:
        user_info = f' or is not owned by {user.email}' if user else ''
        model_name = model.__name__.replace("Run", " Job")
        error = f'{model_name} {run_id} does not exist{user_info}'
        return None, ResponseError(error)

    model_name = model.__name__.replace("Run", " Job")

    error = validate_run_instance(
        run=run,
        run_id=run_id,
        run_status=run_status,
        is_archived_field=is_archived_field,
        include_archived=include_archived,
        model_name=model_name,
    )

    if error:
        return run, error

    return run, None


def get_calibration_run(
        calibration_run_id: int,
        user: User | None,
        run_status: list[StatusEnum] | None = None,
        include_archived: bool = False
) -> tuple[CalibrationRun | None, Response | None]:
    """
    Retrieve a CalibrationRun by ID, optionally filtering by owner and status.

    :param calibration_run_id: The ID of the CalibrationRun.
    :param user: User requesting the CalibrationRun; if None, no owner filtering.
    :param run_status: Allowed statuses for the CalibrationRun.
    :param include_archived: Include archived jobs if True.
    :return: Tuple of CalibrationRun or None, and Response if error or None.
    """
    return get_run_instance(
        CalibrationRun,
        calibration_run_id,
        user,
        run_status,
        owner_field='owner',
        is_archived_field='is_archived',
        include_archived=include_archived,
        select_related_fields=('status', 'performance_metrics', 'owner'),
    )


def get_validation_run(
        validation_run_id: int,
        user: User | None,
        run_status: list[StatusEnum] | None = None
) -> tuple[ValidationRun | None, Response | None]:
    """
    Retrieve a ValidationRun by ID, optionally filtering by owner and status.

    :param validation_run_id: The ID of the ValidationRun.
    :param user: User requesting the ValidationRun; if None, no owner filtering.
    :param run_status: Allowed statuses for the ValidationRun.
    :return: Tuple of ValidationRun or None, and Response if error or None.
    """
    return get_run_instance(
        ValidationRun,
        validation_run_id,
        user,
        run_status,
        owner_field='calibration_run__owner',
        is_archived_field='calibration_run__is_archived',
        select_related_fields=(
            'status',
            'performance_metrics',
            'calibration_run',
            'calibration_run__owner',
        ),
    )


def get_cold_start_run(
        cold_start_run_id: int,
        user: User | None,
        run_status: list[StatusEnum] | None = None
) -> tuple[ColdStartRun | None, Response | None]:
    """
    Retrieve a ColdStartRun by ID, optionally filtering by owner and status.

    :param cold_start_run_id: The ID of the ColdStartRun.
    :param user: User requesting the ColdStartRun; if None, no owner filtering.
    :param run_status: Allowed statuses for the ColdStartRun.
    :return: Tuple of ColdStartRun or None, and Response if error or None.
    """
    return get_run_instance(
        ColdStartRun,
        cold_start_run_id,
        user,
        run_status,
        owner_field='calibration_run__owner',
        is_archived_field='calibration_run__is_archived',
        select_related_fields=(
            'status',
            'performance_metrics',
            'calibration_run',
            'calibration_run__owner',
        ),

    )


def get_forecast_run(
        forecast_run_id: int,
        user: User | None,
        run_status: list[StatusEnum] | None = None
) -> tuple[ForecastRun | None, Response | None]:
    """
    Retrieve a ForecastRun by ID, optionally filtering by owner and status.

    :param forecast_run_id: The ID of the ForecastRun.
    :param user: User requesting the ForecastRun; if None, no owner filtering.
    :param run_status: Allowed statuses for the ForecastRun.
    :return: Tuple of ForecastRun or None, and Response if error or None.
    """
    return get_run_instance(
        ForecastRun,
        forecast_run_id,
        user,
        run_status,
        owner_field='calibration_run__owner',
        is_archived_field='calibration_run__is_archived',
        select_related_fields=(
            'status',
            'performance_metrics',
            'calibration_run',
            'calibration_run__owner',
            'configuration',
            'cold_start_run',
            'cold_start_run__status',
            'cold_start_run__performance_metrics',
        ),
    )


def get_hindcast_run(
        hindcast_run_id: int,
        user: User | None,
        run_status: list[StatusEnum] | None = None
) -> tuple[HindcastRun | None, Response | None]:
    """
    Retrieve a HindcastRun by ID, optionally filtering by owner and status.

    :param hindcast_run_id: The ID of the HindcastRun.
    :param user: User requesting the HindcastRun; if None, no owner filtering.
    :param run_status: Allowed statuses for the HindcastRun.
    :return: Tuple of HindcastRun or None, and Response if error or None.
    """
    return get_run_instance(
        HindcastRun,
        hindcast_run_id,
        user,
        run_status,
        owner_field='calibration_run__owner',
        is_archived_field='calibration_run__is_archived',
        select_related_fields=(
            'status',
            'performance_metrics',
            'calibration_run',
            'calibration_run__owner',
            'configuration',
            'cold_start_run',
            'cold_start_run__status',
            'cold_start_run__performance_metrics',
        ),
    )


def get_verification_run(
        verification_run_id: int,
        user: User | None,
        run_status: list[StatusEnum] | None = None
) -> tuple[VerificationRun | None, Response | None]:
    """
    Retrieve a VerificationRun by ID, optionally filtering by owner and status.

    :param verification_run_id: The ID of the VerificationRun.
    :param user: User requesting the VerificationRun; if None, no owner filtering.
    :param run_status: Allowed statuses for the VerificationRun.
    :return: Tuple of VerificationRun or None, and Response if error or None.
    """
    return get_run_instance(
        VerificationRun,
        verification_run_id,
        user,
        run_status,
        owner_field='forecast_run__calibration_run__owner',
        is_archived_field='is_archived',
        select_related_fields=(
            'status',
            'performance_metrics',
            'forecast_run',
            'forecast_run__status',
            'forecast_run__performance_metrics',
            'forecast_run__configuration',
            'forecast_run__calibration_run',
            'forecast_run__calibration_run__owner',
        )
    )


def join_with_or(items: list[str]) -> str:
    """
    Join strings into a comma-separated string, using 'or' before the last item.

    :param items: A list of strings.
    :return: Joined string.
    """
    if not items:
        return ''
    elif len(items) == 1:
        return items[0]
    else:
        return ', '.join(items[:-1]) + ' or ' + items[-1]


# Helper function to format datetime in a readable way
def format_datetime(dt: datetime | None) -> str:
    """
    Format datetime to a string, or return 'N/A' if None.

    :param dt: A datetime or None.
    :return: Formatted string or 'N/A'.
    """
    return dt.strftime('%Y-%m-%d %H:%M:%S') if dt else 'N/A'


def png_to_base64_url(png_file_path: str) -> str:
    """
    Converts a PNG file to a base64-encoded URL string.

    :param png_file_path: Path to the PNG file.
    :return: Base64 URL string if successful.
    :raises CerfException: If the file does not exist or cannot be read.
    """
    if png_file_path and os.path.exists(png_file_path):
        try:
            with open(png_file_path, "rb") as png_file:
                return png_str_to_base64_url(png_file.read())
        except IOError as e:
            raise CerfException(f"Failed to read PNG file: {e}")
    else:
        raise CerfException(f"File '{png_file_path}' does not exist")


def png_str_to_base64_url(png_str: bytes | None) -> str | None:
    """
    Convert PNG bytes to a base64-encoded data URL.

    :param png_str: PNG image bytes.
    :return: Base64-encoded data URL or None if input is empty.
    """
    if png_str:
        base64_str = base64.b64encode(png_str).decode('utf-8')
        return f'data:image/png;base64,{base64_str}'
    else:
        return None


def create_calibration_run_internal(user: User, genesis: JobGenesis | None = None) -> CalibrationRun:
    """
    Create a new CalibrationRun for the given user.

    :param user: Owner of the calibration run.
    :param genesis: Origin of the job (optional).
    :return: New CalibrationRun instance.
    """
    run = CalibrationRun.objects.create(
        is_active=True,
        owner=user,
        status=StatusEnum.SAVED.db_instance,
        job_genesis=genesis.value if genesis else JobGenesis.GUI.value,
    )

    # Just get the user part, before the @ sign
    username = run.owner.username.split('@')[0]
    run.job_data_dir = os.path.join(settings.NGEN_CAL_RUN_DIR, f"{run.id}_{username}")

    # Clean up any existing directory if it already exists (should not happen in production)
    if os.path.exists(run.job_data_dir):
        # Append timestamp to existing directory name to avoid overwriting
        new_name = f"{run.job_data_dir}_{datetime.now().isoformat()}"
        os.rename(run.job_data_dir, new_name)

    # Create directory (uses worker umask=022 enforced by Gunicorn)
    os.makedirs(run.job_data_dir, exist_ok=True)

    # Determine actual permissions
    mode = os.stat(run.job_data_dir).st_mode & 0o777

    logger.info(f"Directory created: {run.job_data_dir} | perms={oct(mode)}")

    # This is always true
    run.automatic_validation = True
    run.save(update_fields=['job_data_dir', 'automatic_validation'])
    return run


def create_validation_run_internal(
        calibration_run: CalibrationRun,
        iteration_id: int | None,
        validation_type: ValidationType = None
) -> ValidationRun:
    """
    Create a new ValidationRun object for the given CalibrationRun.

    :param calibration_run: The calibration run that this validation run is associated with.
    :param iteration_id: Iteration id of Calibration Run whose parameters we want to start with.
    :param validation_type: Optional value to store in Validation Run object.
    :return: The newly created ValidationRun instance.
    """
    validation_type = validation_type or ValidationType.VALID_ITERATION

    iteration_object = None
    if validation_type == ValidationType.VALID_ITERATION:
        if iteration_id is None:
            raise CerfException(f"Values must be supplied for both iteration_id")

        try:
            iteration_object = Iteration.objects.get(calibration_run_id=calibration_run.id, id=iteration_id)
        except Iteration.DoesNotExist:
            raise CerfException(f"Cannot find Iteration Id {iteration_id} for Calibration Job {calibration_run.id}")

    validation_run = ValidationRun.objects.create(
        status=StatusEnum.SAVED.db_instance,
        calibration_run_id=calibration_run.id,
        validation_type=validation_type.value,
        iteration=iteration_object
    )
    logger.info(f"Creating Validation Job {validation_run.id} for Calibration Job {calibration_run.id} with validation_type {validation_type}")

    return validation_run


def create_cold_start_run_internal(
        calibration_run: CalibrationRun,
        configuration: ForecastConfiguration,
        cold_start_date: datetime,
        cycle_date: datetime
) -> ColdStartRun:
    """
    Create a new ColdStartRun object for the given CalibrationRun.

    :param calibration_run: The calibration run that this cold start run is associated with.
    :param configuration: The configuration for this cold start
    :param cold_start_date: An optional date to cold start the cold start before the cycle date
    :param cycle_date: The date to start the cycle
    :return: The newly created ColdStartRun instance.
    """

    cold_start_run = ColdStartRun.objects.create(
        status=StatusEnum.SAVED.db_instance,
        calibration_run_id=calibration_run.id,
        configuration_id=configuration.id,
        cold_start_date=cold_start_date,
        cycle_date=cycle_date
    )
    os.makedirs(get_cold_start_dir(cold_start_run))
    logger.info(f"Creating {get_job_description(cold_start_run)}")

    return cold_start_run


def create_forecast_run_internal(
        calibration_run: CalibrationRun,
        cold_start_run: ColdStartRun,
        configuration: ForecastConfiguration,
        cycle_date: datetime
) -> ForecastRun:
    """
    Create a new ForecastRun object for the given CalibrationRun.

    :param calibration_run: The calibration run that this forecast run is associated with.
    :param cold_start_run: (optional) The cold start run that this forecast run is associated with.
    :param configuration: The configuration for this forecast
    :param cycle_date: The date to start the cycle
    :return: The newly created ForecastRun instance.
    """

    forecast_run = ForecastRun.objects.create(
        status=StatusEnum.SAVED.db_instance,
        calibration_run_id=calibration_run.id,
        cold_start_run=cold_start_run,
        configuration_id=configuration.id,
        cycle_date=cycle_date
    )
    os.makedirs(get_forecast_dir(forecast_run))
    logger.info(f"Creating {get_job_description(forecast_run)}")

    return forecast_run


def create_hindcast_run_internal(
        calibration_run: CalibrationRun,
        cold_start_run: ColdStartRun,
        configuration: ForecastConfiguration,
        cycle_date: datetime,
        interval_cycle: int,
        num_iterations: int
) -> HindcastRun:
    """
    Create a new HindcastRun object for the given CalibrationRun.

    :param calibration_run: The calibration run that this hindcast run is associated with.
    :param cold_start_run: (optional) The cold start run that this hindcast run is associated with.
    :param configuration: The configuration for this hindcast
    :param cycle_date: The date to start the cycle
    :param interval_cycle: The interval to start each forecast cycle
    :param num_iterations: The number of iterations to run
    :return: The newly created HindcastRun instance.
    """

    hindcast_run = HindcastRun.objects.create(
        status=StatusEnum.SAVED.db_instance,
        calibration_run_id=calibration_run.id,
        cold_start_run=cold_start_run,
        configuration_id=configuration.id,
        cycle_date=cycle_date,
        interval_cycle=interval_cycle,
        num_iterations=num_iterations
    )
    os.makedirs(get_hindcast_dir(hindcast_run))
    logger.info(f"Creating {get_job_description(hindcast_run)}")

    return hindcast_run


def create_verification_run_internal(forecast_run: ForecastRun) -> VerificationRun | Response:
    """
    Create a new VerificationRun for the given user.

    - Calls create_verification_input(verification_run) to generate the config

    :param forecast_run Forecast Job to associate with this verification run
    :return: New VerificationRun instance.
    """
    verification_run = VerificationRun.objects.create(
        status=StatusEnum.SAVED.db_instance,
        forecast_run=forecast_run)

    os.makedirs(get_verification_run_dir(verification_run))
    logger.info(f"Creating {get_job_description(verification_run)}")

    return verification_run


TOKEN_SLURM_SCOPE = 'slurm_callback'
TOKEN_NGEN_SCOPE = 'ngen'


def generate_custom_token(user: User, scope: str) -> str:
    """
    Generate a JWT access token for a user with custom scope and 24-hour expiration.

    :param user: User for whom to generate the token.
    :param scope: Custom scope for the token.
    :return: JWT token string.
    """
    access = AccessToken.for_user(user)
    # Set the expiration to 30 days from now
    access.set_exp(lifetime=timedelta(days=30))

    # Set our custom scope
    access['scope'] = scope

    return str(access)


def auth_scope_required(scope):
    """
    Custom decorator to require a specific token scope.
    """
    return permission_classes([lambda: CheckTokenScope(scope)])


class CheckTokenScope(BasePermission):
    """
    Permission class to check if the provided JWT token contains a specific scope.
    """

    def __init__(self, required_scope):
        self.required_scope = required_scope

    def has_permission(self, request, view):
        # Ensure that the user is authenticated and has a valid token
        if not request.user or not request.auth:
            logger.debug(f"No token or user provided - user: {request.user}, auth: {request.auth}")
            return False

        # We should already have a validated token in request.auth
        token = request.auth

        # Log the available scopes and the required one
        token_scope = token.get('scope', '').split()
        logger.debug(f"Validating token: Token scope: {token_scope}, Required scope: {self.required_scope}")

        # Make sure we have our custom scope
        if self.required_scope not in token_scope:
            logger.debug(f"Permission denied: required scope '{self.required_scope}' not in token scope {token_scope}")
            return False

        return True


# Function wrapper to implement common exception handling
def handle_exceptions(view_func):
    """
    A decorator to wrap view functions and handle common exceptions.
    Logs the exception and returns a formatted error response when an exception occurs.

    :param view_func: The view function to wrap.
    :return: The wrapped view function with exception handling.
    """

    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        original_logger = logging.getLogger(view_func.__module__)
        try:
            response = view_func(request, *args, **kwargs)

            # Hook into Django's response rendering
            if isinstance(response, Response):
                original_render = response.render

                def safe_render():
                    """ Wrap response rendering to catch JSON serialization errors """
                    try:
                        return original_render()
                    except ValueError as d1:
                        original_logger.error(f"JSON serialization error in {view_func.__name__}: {str(d1)}")
                        return JsonResponse(
                            {
                                "message": "JSON serialization error in response. Response contains non-JSON-compliant values (d1.g., Infinity, NaN).",
                                "response_type": "json_serialization_error",
                                "validation_errors": str(d1),
                            },
                            status=500
                        )
                    except TypeError as d1:
                        original_logger.error(f"Non-serializable data error in {view_func.__name__}: {str(d1)}")
                        return JsonResponse(
                            {
                                "message": "Response contains non-serializable data (d1.g., custom objects, functions).",
                                "response_type": "json_serialization_error",
                                "validation_errors": str(d1),
                            },
                            status=500
                        )

                response.render = safe_render  # Override render method

            return response

        except ParseError as e:
            message = f"{type(e).__name__} - {str(e)} - while running {view_func.__module__}.{view_func.__name__}"
            original_logger.exception(message)
            return ResponseError(message, response_type='parse_error')
        except CerfException as e:
            message = f"{type(e).__name__} - {str(e)} - while running {view_func.__module__}.{view_func.__name__}"
            original_logger.exception(message)
            return ResponseError(message, response_type='error')
        except Exception as e:
            message = f"{type(e).__name__} - {str(e)} - while running {view_func.__module__}.{view_func.__name__}"
            original_logger.exception(f"Unhandled exception in handle_exceptions: {message}")
            return ResponseError(message, response_type='exception')

    return _wrapped_view


def get_valid_path(eds_path, get_path_func):
    """
    Determine the valid file path by checking the job-specific path first,
    then falling back to the provided EDS path if the job-specific file does not exist.

    :param eds_path: The EDS path (can be local or cloud URL).
    :param get_path_func: A function to retrieve the job-specific path.
    :return: The path to the existing file, either job-specific or EDS; otherwise, None if neither exists.
    """
    job_specific_file = get_path_func()

    # if job_specific_file is there, then always use it
    # If it's not there, then use the EDS file
    if job_specific_file and os.path.exists(job_specific_file):
        return job_specific_file

    # Fall back to the EDS path if the job-specific file is not found
    # TODO Might be cloud url.  change to os.path.exists once we are exclusiving using BMI Forcing
    if eds_path and path_exists(eds_path):
        return eds_path

    return None


def truncate_large_fields(data, fields_to_truncate=None, max_length=100):
    """
    Truncate large fields (lists, dicts, strings) in the data to prevent logging large values.

    :param data: Dictionary of response data.
    :param fields_to_truncate: List of fields to truncate in logs.
    :param max_length: The maximum number of characters/items to display before truncating.
    :return: Redacted dictionary for logging.
    """
    if fields_to_truncate is None:
        fields_to_truncate = []

    truncated_data = data.copy()
    for field in fields_to_truncate:
        if field in truncated_data:
            value = truncated_data[field]
            # Truncate strings if they exceed max_length
            if isinstance(value, str) and len(value) > max_length:
                truncated_data[field] = f"{value[:max_length]}... (truncated)"
            # Truncate lists if they exceed max_length
            elif isinstance(value, list) and len(value) > max_length:
                truncated_data[field] = value[:max_length] + [f"... (truncated, {len(value)} total items)"]
            # Truncate dicts by taking the first max_length key-value pairs
            elif isinstance(value, dict) and len(value) > max_length:
                truncated_dict = {k: value[k] for k in list(value)[:max_length]}
                truncated_dict["..."] = f"(truncated, {len(value)} total keys)"
                truncated_data[field] = truncated_dict
    return truncated_data


def ResponseError(message, response_type='error', validation_errors=None, errors=None, http_status=status.HTTP_400_BAD_REQUEST):
    """
    Return a standardized error response, with optional validation errors.

    :param message: The error message to include.
    :param response_type: The type of error (default is 'error').
    :param validation_errors: Optional validation errors to include.
    :param errors: Optional fatal errors to include which causes the job to fail
    :param http_status: The HTTP status code for the response (default is 400).
    :return: A formatted Response object with the error details.
    """
    response = {'response_type': response_type, 'message': message}
    if validation_errors:
        response['validation_errors'] = validation_errors
    if errors:
        response['errors'] = errors
    serializer = ErrorResponseSerializer(response)
    logger.error(serializer.data)
    return Response(serializer.data, status=http_status)


def validate_request(serializer_class, data, context=None):
    """
    Validate request data using the specified serializer class.
    Returns the validated data or an error response if validation fails.

    :param serializer_class: The serializer class to use for validation.
    :param data: The data to be validated.
    :param context: Optional context for the serializer.
    :return: The validated data or an error response.
    """
    validator = serializer_class(data=data, context=context)
    try:
        validator.is_valid(raise_exception=True)
        return validator.validated_data, None
    except ValidationError as e:
        calling_function = inspect.stack()[1].function  # Get the name of the calling function
        message = f"called from {calling_function}, validated by {validator.__class__.__name__}"
        validation_errors = validator.errors if validator else str(e)
        return None, ResponseError(message, response_type='validation_error', validation_errors=validation_errors)


def validate_response(serializer_class, data, fields_to_truncate=None, max_length=100):
    """
    Validate the response data using the specified serializer class.
    Logs validation errors if any and returns the validator or an error response.

    :param serializer_class: The serializer class to use for validation.
    :param data: The data to be validated.
    :param fields_to_truncate: Optional list of fields to truncate in logs.
    :param max_length: Maximum length for truncation.
    :return: The validated data or an error response.
    """
    validator = serializer_class(data=data)
    try:
        validator.is_valid(raise_exception=True)

        # Redact large fields before logging
        # logger.debug(f'Validated response data: {truncate_large_fields(data, fields_to_truncate, max_length)}')

        return validator, None
    except ValidationError as e:
        # Log the full data and errors in case of validation failure
        logger.error(f"Validation error with data: {truncate_large_fields(data, fields_to_truncate, max_length)}")
        logger.error(f"Validation errors: {str(e)}")

        # Note that an exception here is most likely due to a coding error
        calling_function = inspect.stack()[1].function  # Get the name of the calling function
        message = f"Data format error in response returning from {calling_function} - validated by {validator.__class__.__name__}"
        validation_errors = validator.errors if validator else str(e)
        return None, ResponseError(message, response_type='validation_error_response', validation_errors=validation_errors)


def validate_response_data(serializer_class: Type[BaseSerializer], data: dict[str, Any], error_message: str) -> dict[str, Any]:
    """
    Validates response data and raises an exception if validation fails.

    :param serializer_class: The serializer class for validation.
    :param data: The data to validate.
    :param error_message: Error message if validation fails.
    :return: Validated data.
    """
    try:
        formatted_data = json.dumps(data)  # Attempt JSON formatting
    except (TypeError, ValueError):
        formatted_data = str(data)  # Fallback to string representation

    validator = serializer_class(data=data)
    if not validator.is_valid():
        logger.error(f"Response data: {formatted_data}")
        raise CerfException(f'{error_message} - Validated by {validator.__class__.__name__} -- {validator.errors}')
    return validator.data


class CerfException(Exception):
    """
    Custom exception class for handling specific exceptions with optional details.
    """

    def __init__(self, message=None, details=None):
        self.message = message
        self.details = details
        super().__init__(self.message)

    def __str__(self):
        if self.details:
            return f"{self.message}: {self.details}"
        return self.message


def get_job_description(run: BaseRun) -> str:
    """
    Get a descriptive string identifying the job type and owner.

    :param run: Job instance (CalibrationRun, ValidationRun, ForecastRun, VerificationRun).
    :return: Description of the job.
    """
    if isinstance(run, CalibrationRun):
        return f"Calibration Job {run.id}, user: {run.owner.username}"
    elif isinstance(run, ValidationRun):
        return f"Validation Job {run.id} for Calibration Job {run.calibration_run.id}, type: {run.validation_type}, user: {run.calibration_run.owner.username}"
    elif isinstance(run, ForecastRun):
        cold_start_data = f' using Cold Start Job {run.cold_start_run.id}' if run.cold_start_run else ''
        return f"Forecast Job {run.id} for Calibration Job {run.calibration_run.id}{cold_start_data}, user: {run.calibration_run.owner.username}"
    elif isinstance(run, HindcastRun):
        cold_start_data = f' using Cold Start Job {run.cold_start_run.id}' if run.cold_start_run else ''
        return f"Hindcast Job {run.id} for Calibration Job {run.calibration_run.id}{cold_start_data}, user: {run.calibration_run.owner.username}"
    elif isinstance(run, ColdStartRun):
        return f"Cold Start Job {run.id} for Calibration Job {run.calibration_run.id}, user: {run.calibration_run.owner.username}"
    elif isinstance(run, VerificationRun):
        return f"Verification Job {run.id} for Forecast Job {run.forecast_run.id} for Calibration Job {run.forecast_run.calibration_run.id}, user: {run.forecast_run.calibration_run.owner.username}"

    raise ValueError(f"Unknown job type: {type(run).__name__}")


# Regular expression pattern to match directories like "ngen_xxxxxxx_worker"
worker_directory_pattern = re.compile(r'ngen_\w+_worker')


def process_worker_dirs(
        run: CalibrationRun | ValidationRun,
        worker_lambda: Callable[[str, CalibrationRun | ValidationRun], bool]
) -> None:
    """
    Iterate over worker directories for a calibration or validation run.

    The callback is invoked once per worker directory.

    Return semantics:
    - Return True to stop iterating immediately.
    - Return False to continue scanning remaining workers.

    :param run: The CalibrationRun or ValidationRun instance.
    :param worker_lambda: A callback function applied to each worker directory.
                          Return True to stop iterating, False to continue.
    """

    if isinstance(run, CalibrationRun):
        output_run_dir = get_output_calibration_run_dir(run)
    elif isinstance(run, ValidationRun):
        output_run_dir = get_output_validation_run_dir(run.calibration_run)
    else:
        raise ValueError(f"Invalid run object: {type(run).__name__}. Expected CalibrationRun or ValidationRun.")

    if not os.path.exists(output_run_dir):
        raise CerfException(f"Cannot find expected data at {output_run_dir}")

    job_description = get_job_description(run)

    for item in os.listdir(output_run_dir):
        worker_dir = os.path.join(output_run_dir, item)
        # Check if the item is a directory and matches the pattern
        if os.path.isdir(worker_dir) and worker_directory_pattern.match(item):
            logger.debug(f"Processing worker directory: {worker_dir} for {job_description}")
            should_stop = worker_lambda(worker_dir, run)
            # Stop only on an explicit True; any other value continues iteration
            if should_stop is True:  # noqa
                break


def find_validation_worker_with_matching_id(
        validation_run: ValidationRun,
        worker_name: str | None = None,
        iteration_num: int | None = None
) -> str | None:
    """
    Searches the worker directories of a validation run to locate the worker directory
    containing the matching worker_id file. The matching criteria depend on the validation type:
    - For VALID_ITERATION: Matches the worker name and iteration number.
    - For VALID_BEST or VALID_CONTROL: Matches the validation type only.

    :param validation_run: The validation run object to process.
    :param worker_name: The worker name to match in the ngen.log file (only for VALID_ITERATION).
    :param iteration_num: The iteration number to match in the ngen.log file (only for VALID_ITERATION).
    :return: The name of the worker directory containing the matching worker_id file, or None if not found.
    """
    matching_worker_name = None
    validation_type = ValidationType(validation_run.validation_type)

    # Determine the expected first line of the log based on validation type
    if validation_type == ValidationType.VALID_ITERATION:
        if not worker_name or iteration_num is None:
            raise ValueError("worker_name and iteration_num are required for VALID_ITERATION.")
        expected_first_line = f"Valid_{worker_name}_iter{iteration_num}"
    elif validation_type in {ValidationType.VALID_BEST, ValidationType.VALID_CONTROL}:
        expected_first_line = f"{validation_type.value.capitalize()}"
    else:
        raise ValueError(f"Unsupported validation type: {validation_type}")

    # Custom function to check worker directories for the ngen.log file
    def check_worker(worker_dir: str, _run: ValidationRun) -> bool:
        nonlocal matching_worker_name
        worker_id_filename = 'worker_id.txt'
        worker_id_path = os.path.join(worker_dir, worker_id_filename)

        # Check if worker_id file exists in the current worker directory
        if os.path.isfile(worker_id_path):
            # Read the first (and only) line of the file
            with open(worker_id_path, 'r') as file:
                first_line = file.readline().strip()

            logger.debug(f"check_worker: {worker_id_path} -> '{first_line}' (expected '{expected_first_line}')")

            # Check if the line matches the expected format (case-insensitive)
            if first_line.casefold() == expected_first_line.casefold():
                matching_worker_name = os.path.basename(worker_dir)
                return True  # stop searching
        else:
            logger.error(f"Could not find {worker_id_filename} file in {worker_dir}")

        return False  # keep searching

    # Call process_worker_dirs to iterate through the worker directories
    process_worker_dirs(validation_run, check_worker)

    if not matching_worker_name:
        logger.error(f"Could not find worker corresponding to {validation_run}")
    else:
        logger.info(
            f"Matched worker directory '{matching_worker_name}' for Validation Job {validation_run.id} "
            f"(type: {ValidationType(validation_run.validation_type).value})"
        )

    return matching_worker_name


def get_user_email(request: Request) -> str:
    """
    Returns the email address of the authenticated user associated with the request.

    This function checks whether the user is authenticated and has an 'email' attribute.
    If both conditions are satisfied, it returns the email address.
    Otherwise, it returns 'Anonymous'.

    We use a function because Pycharm gives a warnings if we use request.user.email directly
    since we are using a CustomUser object

    :param request: The incoming DRF Request object.
    :return: User's email address or 'Anonymous' if not available.
    """
    user = request.user

    # Check if the user is authenticated and has an 'email' attribute
    if getattr(user, "is_authenticated", False) and hasattr(user, "email"):
        return user.email

    # Fallback if unauthenticated or missing 'email'
    return "Anonymous"


def generate_ngen_logging_config(run: CalibrationRun | ValidationRun | ForecastRun | HindcastRun, logging_config_param: dict = None) -> dict:
    """
    Generate the JSON logging configuration for a calibration, validation, or forecast run.

    The generated config includes:
    - All valid modules (based on cached definitions)
    - Special cases of 'ngen' and 'forcing'
    - Default log levels set to INFO, unless overridden
    - Top-level flags for logging_enabled and split_logs_by_module

    Overrides are applied in this order:
    1. A previously imported logging config file, if it exists
    2. The config used in a previous run, if it exists
    3. The provided `logging_config_param` dictionary

    All module names are treated case-insensitively and stored in lowercase in the output.

    :param run: A CalibrationRun, ValidationRun, or ForecastRun instance for which to generate the logging config.
    :param logging_config_param: A dictionary with optional overrides, e.g.:
        {
            "logging_enabled": False,
            "split_logs_by_module": True,
            "modules": {"cfe-s": "DEBUG"}
        }
    :return: A dictionary representing the final logging config.
    """
    if not logging_config_param:
        logging_config_param = {}

    # Only use modules in our formulation
    calibration_run = run if isinstance(run, CalibrationRun) else run.calibration_run
    formulations = CalibrationFormulation.objects.filter(calibration_run=calibration_run).only("module_id")
    modules_by_id = get_cached_modules_by_id()
    module_names = {modules_by_id[f.module_id].name for f in formulations}

    # Get our module names in lowercase, plus special-case 'ngen' and 'forcing'
    valid_modules = {m.lower() for m in module_names}
    valid_modules.add('ngen')
    valid_modules.add('forcing')

    # Default all modules to INFO lvl
    module_levels = {name: NgenLogging.INFO.value for name in valid_modules}
    logging_enabled = True
    split_logs_by_module = False

    # Helper to apply overrides from a config file if it exists
    def apply_config_file(path: str) -> None:
        nonlocal logging_enabled, split_logs_by_module, module_levels
        if not os.path.exists(path):
            return

        with open(path, "r") as f:
            config = json.load(f)

        logging_enabled = config.get("logging_enabled", logging_enabled)
        split_logs_by_module = config.get("split_logs_by_module", split_logs_by_module)

        for name, lvl in config.get("modules", {}).items():
            module_levels[name.lower()] = lvl

    # Apply from imported config (used during import/update)
    apply_config_file(get_ngen_logging_file(run, import_flag=True))

    # Apply from config used in previous run, if any
    apply_config_file(get_ngen_logging_file(run, import_flag=False))

    # Apply overrides from the provided logging_config_param (used by UI during run_calibration_job)
    logging_enabled = logging_config_param.get("logging_enabled", logging_enabled)
    split_logs_by_module = logging_config_param.get("split_logs_by_module", split_logs_by_module)

    for name, level in logging_config_param.get("modules", {}).items():
        module_levels[name.lower()] = level

    return {
        "logging_enabled": logging_enabled,
        "split_logs_by_module": split_logs_by_module,
        "modules": module_levels
    }


def write_ngen_logging_file(run: CalibrationRun | ValidationRun | ForecastRun | HindcastRun | ColdStartRun, logging_config_param: dict) -> None:
    """
    Generate and write the logging config to a JSON file on disk, and create a symbolic link pointing
    to it using a consistent base name.

    This wraps the logic of generating the config and writing it to disk.

    :param run: A CalibrationRun, ValidationRun, ForecastRun, or ColdStartRun instance.
    :param logging_config_param: A dictionary with optional overrides, e.g.:
        {
            "logging_enabled": False,
            "split_logs_by_module": True,
            "modules": {"cfe-s": "DEBUG"}
        }
        This is currently only provided by the UI when calling run_calibration_job.
    """
    logging_config = generate_ngen_logging_config(run, logging_config_param)

    # Write logging config to disk
    output_path = get_ngen_logging_file(run, import_flag=False)
    with open(output_path, "w") as f:
        json.dump(logging_config, f, indent=4)

    # Replace or create a symbolic link with a consistent name - All files created in the job root directory
    job_data_dir = run.job_data_dir if isinstance(run, CalibrationRun) else run.calibration_run.job_data_dir
    symlink_path = os.path.join(job_data_dir, f'{get_ngen_logging_basename()}.json')

    if os.path.islink(symlink_path) or os.path.exists(symlink_path):
        os.remove(symlink_path)

    # Since both are in the same directory, relative link = basename
    os.symlink(os.path.basename(output_path), symlink_path)


class ErrorReport:
    """
    Container for collecting error and fatal validation messages during run preparation.

    - `errors`: Recoverable issues the user can fix (e.g., missing input data).
    - `fatal`: Irrecoverable issues that typically require system admin or developer intervention.
    """

    def __init__(self) -> None:
        """
        Initialize an empty ErrorReport.
        """
        self._warnings: list[str] = []
        self._errors: list[str] = []

    def add_warning(self, message: str) -> None:
        """
        Add a user-fixable error message.

        :param message: The error message to add.
        """
        self._warnings.append(message)

    def add_error(self, message: str) -> None:
        """
        Add a fatal error message.

        :param message: The fatal error message to add.
        """
        self._errors.append(message)

    def has_warnings(self) -> bool:
        return bool(self._warnings)

    def has_errors(self) -> bool:
        return bool(self._errors)

    @property
    def warnings(self) -> list[str]:
        return self._warnings

    @property
    def errors(self) -> list[str]:
        return self._errors

    def __str__(self) -> str:
        """
        Return a human-readable string representation of the error report.
        """
        output = []
        if self._warnings:
            output.append("Warnings:")
            output.extend(f"  - {w}" for w in self._warnings)
        if self._errors:
            output.append("Errors:")
            output.extend(f"  - {e}" for e in self._errors)
        if not output:
            return "No warnings or errors."
        return "\n".join(output)


def get_elapsed_str(request: Request) -> str:
    """
    Compute elapsed time since the request started using `request._request._start_time`.

    This is intended to be called near the end of a view for logging purposes.

    :param request: The DRF Request object.
    :return: A string like " in 0.253s", or "" if unavailable.
    """
    raw_request = getattr(request, '_request', None)
    start_time = getattr(raw_request, '_start_time', None)

    if start_time is None:
        return ""

    elapsed = time.perf_counter() - start_time
    return f" in {elapsed:.3f}s"


@contextmanager
def readonly_transaction():
    """
    Context manager to enforce a read-only transaction.
    Use this for functions that only query the database.
    Prevents write locks and reduces contention.
    """
    with transaction.atomic(savepoint=False):
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
        yield
