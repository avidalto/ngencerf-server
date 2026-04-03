import errno
import json
import logging
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from django.conf import settings
from django.db import transaction
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import StatusEnum, ValidationType, JobGenesis, ForecastConfigEnum
from calibration.models import CalibrationRun, ValidationRun, ForecastRun
from calibration.run_util.run_common import submit_job
from calibration.util.calibration_validators import FooterResponseSerializer, \
    ErrorResponseSerializer, CreateCalibrationRunResponseSerializer, \
    CalibrationRunIdSerializer, ImportResponseSerializer, \
    CreateAndRunValidationResponseSerializer, CreateValidationRequestSerializer, \
    EmptySerializer, CreateForecastRequestSerializer, CreateAndRunForecastResponseSerializer, \
    ArchiveJobRequestSerializer, GetGitInfoResponseSerializer, CalibrationRunIdList, CalibrationRunListResponse, ImportSerializer, \
    LockJobRequestSerializer, CreateHindcastRequestSerializer, CreateAndRunHindcastResponseSerializer, CreateAndValidateHindcastResponseSerializer
from calibration.util.cloud_util import join_url, copy_tree, s3_prefix_exists, S3CredentialsExpired, normalize_s3_prefix, S3ProfileError, \
    delete_all_s3_objects_under_prefix
from calibration.util.git_util import get_git_info_internal
from calibration.views import ngen_cal_input
from calibration.views.calibration_import_export_views import load_calibration_run_data, import_calibration_run_data
from calibration.views.calibration_run_views import map_path_to_host
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, validate_response, get_calibration_run, create_calibration_run_internal, ResponseError, \
    validate_request, create_validation_run_internal, create_forecast_run_internal, get_user_email, get_elapsed_str, readonly_transaction, \
    format_datetime, create_cold_start_run_internal, get_job_description, get_calibration_runs_bulk, create_hindcast_run_internal, get_cold_start_run

logger = logging.getLogger(__name__)


@extend_schema(
    request=EmptySerializer,
    responses={
        201: CreateCalibrationRunResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Create a new calibration"
)
@api_view(['POST'])
@handle_exceptions
def create_calibration_run(request: Request) -> Response:
    """
    Creates a new calibration run for the requesting user.

    Handles the creation process by accepting calibration details in the request, validating them,
    and creating a new calibration job if the request is valid.

    :param request: The HTTP request object, containing user and calibration run details.
    :return: A Response object with the serialized calibration run data.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} ')

    validator, error_return = validate_request(EmptySerializer, data)
    if error_return:
        return error_return

    with transaction.atomic():
        run = create_calibration_run_internal(request.user)

        response = {
            'message': f'Calibration Job {run.id} created',
            'calibration_run_id': run.id,
            'job_data_dir': map_path_to_host(run.job_data_dir)
        }

        response_validator, error_response = validate_response(CreateCalibrationRunResponseSerializer, response)
        if error_response:
            return error_response

        logger.debug(
            f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
            f'{json.dumps(response_validator.data)}'
        )
        return Response(response_validator.data, status=status.HTTP_201_CREATED)


@extend_schema(
    request=CreateValidationRequestSerializer,
    responses={
        201: CreateAndRunValidationResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Create and run a new validation for a specific iteration"
)
@api_view(['POST'])
@handle_exceptions
def create_and_run_validation(request: Request) -> Response:
    """
    Creates and runs a new validation run for a specified calibration run and iteration.

    Validates the request, checks if a validation job already exists for the specified calibration run
    and iteration, and creates and submits a new validation job if not.

    Additionally, disallows submission if either the VALID_CONTROL or VALID_BEST
    validation job for this calibration run is still RUNNING or SUBMITTED.

    :param request: The HTTP request object containing calibration and iteration details.
    :return: JSON response with validation run details or error information.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} ')

    validator, error_return = validate_request(CreateValidationRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    iteration_id = validator.get('iteration_id')

    calibration_run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.DONE])
    if error_return:
        return error_return
    assert calibration_run is not None

    # ─────────────────────────────────────────────
    # Require BOTH VALID_CONTROL and VALID_BEST to be DONE.
    # Block if EITHER is missing or not DONE.
    # ─────────────────────────────────────────────
    required_types = [
        ValidationType.VALID_CONTROL.value,
        ValidationType.VALID_BEST.value,
    ]

    # Build dict of existing runs
    control_best_dict = {
        vr.validation_type: vr
        for vr in ValidationRun.objects.filter(
            calibration_run=calibration_run,
            validation_type__in=required_types
        )
    }

    # Ensure both exist and both are DONE
    for vt in required_types:
        vr = control_best_dict.get(vt)
        if not vr or vr.status != StatusEnum.DONE.db_instance:
            label = "VALID_CONTROL" if vt == ValidationType.VALID_CONTROL.value else "VALID_BEST"
            status_name = vr.status.name if vr else "MISSING"
            return ResponseError(
                f"Cannot submit a new validation job because {label} is {status_name} for "
                f"Calibration Job {calibration_run.id}. Both VALID_CONTROL and VALID_BEST must be DONE."
            )

    # Check if a ValidationRun already exists for this CalibrationRun and Iteration
    existing_validation_run_id = (
        ValidationRun.objects.filter(
            calibration_run=calibration_run,
            iteration_id=iteration_id,
            status__in=[StatusEnum.DONE.db_instance, StatusEnum.RUNNING.db_instance, StatusEnum.SUBMITTED.db_instance]
        )
        .values_list('id', flat=True)
        .first()
    )
    if existing_validation_run_id:
        return ResponseError(f'Validation Job {existing_validation_run_id} already exists for '
                             f'Calibration Job {calibration_run.id}, iteration id {iteration_id}')

    validation_run = create_validation_run_internal(
        calibration_run,
        iteration_id,
        validation_type=ValidationType.VALID_ITERATION
    )
    submit_job(validation_run)

    response = {
        'message': f'Validation Job {validation_run.id} created and submitted for Calibration Job {calibration_run.id}',
        'calibration_run_id': calibration_run.id,
        'validation_run_id': validation_run.id,
        'status': validation_run.status.name,
        'submit_date': validation_run.submit_date
    }

    response_validator, error_response = validate_response(CreateAndRunValidationResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data, status=status.HTTP_201_CREATED)


@extend_schema(
    request=CreateForecastRequestSerializer,
    responses={
        201: CreateAndRunForecastResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Create and run a new forecast with optional cold start"
)
@api_view(['POST'])
@handle_exceptions
def create_and_run_forecast(request: Request) -> Response:
    """
    Creates and runs a new forecast run with an optional cold start for a specified calibration run and cycle_name name.

    :param request: The HTTP request object containing calibration and iteration details.
    :return: JSON response with validation run details or error information.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} ')

    validator, error_return = validate_request(CreateForecastRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    configuration_name = validator.get('configuration_name')
    cycle_date = validator.get('cycle_date')
    cold_start_date = validator.get('cold_start_date')
    logging_config = validator.get('logging_config')

    run_cold_start = cold_start_date is not None

    calibration_run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.DONE])
    if error_return:
        return error_return
    assert calibration_run is not None

    forecast_errors = []

    configuration = ForecastConfigEnum.get_instance(configuration_name)
    if configuration.domain != calibration_run.gage.domain:
        forecast_errors.append(
            f"{configuration_name} is not a valid configuration for domain "
            f"{calibration_run.gage.domain.name}"
        )

    # Define the supported forecast window. Forecast cycles before min_cycle_date
    # are invalid, and cycles after max_cycle_date may not yet be available.
    min_cycle_date = datetime(2022, 1, 1, tzinfo=timezone.utc)
    future_forecast_availability = configuration.availability_lag or 0
    max_cycle_date = datetime.now(tz=timezone.utc) - timedelta(hours=future_forecast_availability)

    # Validate the requested forecast cycle date against the configured schedule.
    validate_forecast_cycle_date(
        check_date=cycle_date,
        configuration=configuration,
        min_cycle_date=min_cycle_date,
        max_cycle_date=max_cycle_date,
        errors=forecast_errors,
        label="Cycle",
    )

    # If a cold start date is provided, it must be earlier than the requested
    # forecast cycle date and still within the supported forecast window.
    if run_cold_start:
        if cold_start_date >= cycle_date:
            forecast_errors.append("Cold start date must be earlier than cycle date")
        if cold_start_date < min_cycle_date:
            forecast_errors.append(f"Cold start cannot be before {format_datetime(min_cycle_date)}")

    if forecast_errors:
        return ResponseError("Error submitting forecast", errors=forecast_errors)

    cold_start_run = create_cold_start_run_internal(
        calibration_run,
        configuration,
        cold_start_date=cold_start_date,
        cycle_date=cycle_date
    ) if run_cold_start else None

    forecast_run = create_forecast_run_internal(
        calibration_run,
        cold_start_run,
        configuration,
        cycle_date
    )

    if cold_start_run is not None:
        # The Forecast Job will be submitted automatically after the Cold Start Job finishes.
        submit_job(cold_start_run, logging_config=logging_config)
    else:
        submit_job(forecast_run, logging_config=logging_config)

    if cold_start_run is not None:
        msg = (
            f'{get_job_description(cold_start_run)} created and submitted, '
            f'followed by {get_job_description(forecast_run)}'
        )
        submit_date = cold_start_run.submit_date
    else:
        msg = f'{get_job_description(forecast_run)} created and submitted'
        submit_date = forecast_run.submit_date

    response = {
        'message': msg,
        'calibration_run_id': calibration_run.id,
        'forecast_run_id': forecast_run.id,
        'cold_start_run_id': cold_start_run.id if cold_start_run is not None else None,
        'submit_date': submit_date
    }

    response_validator, error_response = validate_response(CreateAndRunForecastResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data, status=status.HTTP_201_CREATED)


@extend_schema(
    request=CreateHindcastRequestSerializer,
    responses={
        201: OpenApiResponse(
            response={
                "oneOf": [
                    CreateAndRunHindcastResponseSerializer,
                    CreateAndValidateHindcastResponseSerializer,
                ]
            },
            description="Created and submitted hindcast, or validation-only response"
        ),
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Create and run a new hindcast with optional cold start, or validate only"
)
@api_view(['POST'])
@handle_exceptions
def create_and_run_hindcast(request: Request) -> Response:
    """
    Creates and runs a new hindcast run with an optional cold start for a specified calibration run and cycle_name name.

    :param request: The HTTP request object containing calibration and iteration details.
    :return: JSON response with validation run details or error information.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} ')

    validator, error_return = validate_request(CreateHindcastRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    configuration_name = validator.get('configuration_name')
    cycle_date = validator.get('cycle_date')
    interval_cycle = validator.get('interval_cycle')
    num_iterations = validator.get('num_iterations')
    cold_start_date = validator.get('cold_start_date')
    cold_start_run_id = validator.get('cold_start_run_id')
    logging_config = validator.get('logging_config')
    validate_only = validator.get('validate_only')

    if cold_start_run_id:
        if cold_start_date or cycle_date:
            return ResponseError(
                "You must specify either an existing cold start id, or both cycle date "
                "and cold start date, but not both"
            )
    else:
        if not cold_start_date or not cycle_date:
            return ResponseError(
                "You must specify either an existing cold start id, or both cycle date "
                "and cold start date"
            )

    calibration_run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.DONE])
    if error_return:
        return error_return
    assert calibration_run is not None

    hindcast_errors = []

    configuration = ForecastConfigEnum.get_instance(configuration_name)

    if not configuration.supports_hindcast:
        hindcast_errors.append(f"Configuration '{configuration_name}' does not support hindcast")

    if configuration.domain != calibration_run.gage.domain:
        hindcast_errors.append(
            f"'{configuration_name}' is not a valid configuration for domain "
            f"{calibration_run.gage.domain.name}"
        )

    cold_start_run = None
    if cold_start_run_id:
        cold_start_run, error_return = get_cold_start_run(cold_start_run_id, request.user, run_status=[StatusEnum.DONE])
        if error_return:
            return error_return
        assert cold_start_run is not None

        cold_start_date = cold_start_run.cold_start_date
        cycle_date = cold_start_run.cycle_date

    # Define the supported forecast window used to validate both the requested
    # hindcast cycle date and the furthest projected cycle date.
    min_cycle_date = datetime(2022, 1, 1, tzinfo=timezone.utc)

    # Forecast data may not be available immediately. Shift the latest allowed
    # cycle date backward by the configuration's availability lag.
    future_forecast_availability = configuration.availability_lag or 0
    max_cycle_date = datetime.now(tz=timezone.utc) - timedelta(hours=future_forecast_availability)

    # Validate the requested hindcast starting cycle date using the same
    # schedule rules as forecast.
    validate_forecast_cycle_date(
        check_date=cycle_date,
        configuration=configuration,
        min_cycle_date=min_cycle_date,
        max_cycle_date=max_cycle_date,
        errors=hindcast_errors,
        label="Cycle",
    )

    # Hindcast must also validate the furthest cycle date that could be reached
    # after advancing by interval_cycle hours for num_iterations steps.
    max_projected_cycle_date = cycle_date + timedelta(hours=interval_cycle * num_iterations)

    # Validate the furthest projected cycle date against the same schedule rules
    # as the starting cycle date.
    validate_forecast_cycle_date(
        check_date=max_projected_cycle_date,
        configuration=configuration,
        min_cycle_date=min_cycle_date,
        max_cycle_date=max_cycle_date,
        errors=hindcast_errors,
        label="Maximum projected cycle",
    )

    # Cold start date must be earlier than the requested
    # hindcast cycle date and still within the supported forecast window.
    if cold_start_date >= cycle_date:
        hindcast_errors.append("Cold start date must be earlier than cycle date")
    if cold_start_date < min_cycle_date:
        hindcast_errors.append(f"Cold start cannot be before {format_datetime(min_cycle_date)}")

    if hindcast_errors:
        return ResponseError("Error submitting hindcast", errors=hindcast_errors)

    if validate_only:
        response = {
            'message': 'Hindcast request is valid',
            'calibration_run_id': calibration_run.id,
            'cold_start_run_id': cold_start_run.id if cold_start_run is not None else None,
        }

        response_validator, error_response = validate_response(CreateAndValidateHindcastResponseSerializer, response)
        if error_response:
            return error_response

        logger.debug(
            f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
        return Response(response_validator.data)

    run_cold_start = False

    # Need to create a new cold start if we don't already have one
    if not cold_start_run:
        cold_start_run = create_cold_start_run_internal(
            calibration_run,
            configuration,
            cold_start_date=cold_start_date,
            cycle_date=cycle_date
        )
        run_cold_start = True

    hindcast_run = create_hindcast_run_internal(
        calibration_run,
        cold_start_run,
        configuration,
        cycle_date,
        interval_cycle,
        num_iterations
    )

    if run_cold_start:
        # The Hindcast Job will be submitted automatically after the Cold Start Job finishes.
        submit_job(cold_start_run, logging_config=logging_config)
    else:
        submit_job(hindcast_run, logging_config=logging_config)

    if run_cold_start:
        msg = (
            f'{get_job_description(cold_start_run)} created and submitted, '
            f'followed by {get_job_description(hindcast_run)}'
        )
        submit_date = cold_start_run.submit_date
    else:
        msg = f'{get_job_description(hindcast_run)} created and submitted'
        submit_date = hindcast_run.submit_date

    response = {
        'message': msg,
        'calibration_run_id': calibration_run.id,
        'hindcast_run_id': hindcast_run.id,
        'cold_start_run_id': cold_start_run.id,
        'submit_date': submit_date
    }

    response_validator, error_response = validate_response(CreateAndRunHindcastResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data, status=status.HTTP_201_CREATED)


def validate_forecast_cycle_date(
        check_date: datetime,
        configuration,
        min_cycle_date: datetime,
        max_cycle_date: datetime,
        errors: list[str],
        label: str = "Cycle",
) -> None:
    """
    Validate a forecast-style cycle datetime against the configured forecast schedule.

    This helper validates:
    1. The datetime is not earlier than the minimum supported forecast date.
    2. The datetime is not later than the latest date that should be available,
       after accounting for the configuration's availability lag.
    3. The hour aligns with the configured cycle frequency relative to cycle_start.

    :param check_date: The datetime being validated.
    :param configuration: Forecast configuration enum instance.
    :param min_cycle_date: Earliest allowed forecast datetime.
    :param max_cycle_date: Latest allowed forecast datetime after applying availability lag.
    :param errors: List to append validation error messages to.
    :param label: Human-readable label for the datetime being validated
        (for example "Cycle" or "Maximum projected cycle").
    """
    # Reject dates earlier than the earliest supported forecast cycle date.
    if check_date < min_cycle_date:
        errors.append(
            f"{label} cannot start before {format_datetime(min_cycle_date)} "
            f"(received {format_datetime(check_date)})."
        )

    # Reject dates that are too recent to guarantee forecast availability.
    # max_cycle_date is computed by the caller as:
    #     now - configuration.availability_lag
    if check_date > max_cycle_date:
        future_forecast_availability = configuration.availability_lag or 0
        errors.append(
            f"{label} {format_datetime(check_date)} is not guaranteed to be available "
            f"because forecast availability may lag by up to "
            f"{future_forecast_availability} hours."
        )

    # Validate that the hour aligns with the configured cycle frequency.
    # Hour offset from cycle_start must divide evenly by cycle_freq.
    if (check_date.hour - configuration.cycle_start) % configuration.cycle_freq != 0:
        errors.append(
            f"{label} {format_datetime(check_date)} is not available. "
            f"Forecasts are available every {configuration.cycle_freq} hours "
            f"from {configuration.cycle_start}:00 to {configuration.cycle_end}:00."
        )


@extend_schema(
    request=EmptySerializer,
    responses={
        200: FooterResponseSerializer,
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get footer data"
)
@api_view(['POST', 'GET'])
@handle_exceptions
@permission_classes([AllowAny])
def get_footer(request: Request) -> Response:
    """
    Retrieve footer data such as version and contact email.

    :param request: The HTTP request object.
    :return: A Response object with version and contact information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()

    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(EmptySerializer, data)
    if error_return:
        return error_return

    response = {
        "ngenCerf_version": settings.NGENCERF_VERSION,
        "ngenCerf_date": settings.NGENCERF_DATE,
        "ngenCerf_copyright": settings.NGENCERF_COPYRIGHT,
        "contact_email": settings.CONTACT_EMAIL
    }

    response_validator, error_response = validate_response(FooterResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)


@extend_schema(
    request=EmptySerializer,
    responses={
        200: GetGitInfoResponseSerializer,
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get footer data"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_git_info(request: Request) -> Response:
    """
    Retrieve git info for all components

    :param request: The HTTP request object.
    :return: A Response object with version and contact information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()

    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(EmptySerializer, data)
    if error_return:
        return error_return

    start_time = settings.DJANGO_START_TIME
    uptime = datetime.now(tz=timezone.utc) - start_time  # timedelta
    response = {
        "server_start": start_time,
        "server_uptime": uptime,
        "git_info": get_git_info_internal()
    }

    response_validator, error_response = validate_response(GetGitInfoResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data, default=str)}')
    return Response(response_validator.data)


@extend_schema(
    request=CalibrationRunIdSerializer,
    responses={
        200: ImportResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Clone a calibration job"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def clone_job(request: Request) -> Response:
    """
    Clone an existing calibration job, creating a new calibration run with identical parameters.

    Read-heavy parts (load_calibration_run_data) are executed in a read-only block,
    followed by the write-heavy import step in a separate transaction.

    :param request: The HTTP request object.
    :return: A Response object with the cloned calibration run data.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunIdSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')

    # -------------------------------------------------------------
    # Read-only block: get the source run and prepare export data
    # -------------------------------------------------------------
    with readonly_transaction():
        run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=list(StatusEnum))
        if error_return:
            return error_return
        assert run is not None

        calibration_run_data, _ = load_calibration_run_data(run, export=True)

    # -------------------------------------------------------------
    # Write block: import a new run from the exported data
    # -------------------------------------------------------------
    new_run, _, fatal_error = import_calibration_run_data(request, calibration_run_data, JobGenesis.CLONE)
    if fatal_error:
        return fatal_error
    assert new_run is not None

    # Set the new status to Saved and then we check it
    new_run.status = StatusEnum.SAVED.db_instance
    warnings = None
    errors = None
    if new_run.status in [StatusEnum.SAVED.db_instance, StatusEnum.RUNNING.db_instance]:
        error_object, _ = ngen_cal_input.ready_to_run(new_run)
        if error_object is not None:
            if error_object.has_warnings():
                warnings = error_object.warnings
            if error_object.has_errors():
                errors = error_object.errors

    # noinspection PyUnresolvedReferences
    response = {'message': f'Calibration Job {run.id} has been cloned to Calibration Job {new_run.id}',
                'calibration_run_id': new_run.id,
                'status': new_run.status.name}
    # I agree that the message handling got out of hand
    if warnings:
        response['warnings'] = warnings
    if errors:
        response['errors'] = errors

    response_validator, error_response = validate_response(ImportResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@lru_cache
def get_running_statuses() -> list:
    return [
        StatusEnum.RUNNING.db_instance,
        StatusEnum.SUBMITTED.db_instance,
    ]


def has_running_associated_jobs(run: CalibrationRun) -> str | None:
    """
    Checks if the given calibration job or any associated jobs are currently running.

    This function checks if the calibration run itself, or any of its associated validation, forecast, or forcing download jobs, are running.

    :param run: The CalibrationRun instance to check.
    :return: A message indicating if the job or its associated jobs are running, or None if there are no running jobs.
    """
    running_statuses = get_running_statuses()

    # Check if the calibration run itself is running
    if run.status in running_statuses:
        return f'Calibration Job {run.id} is running. Cannot proceed while the job is running.'

    # Check if any associated validation jobs are running
    if ValidationRun.objects.filter(calibration_run=run, status__in=running_statuses).exists():
        return f'Calibration Job {run.id} has associated validation jobs that are still running. Cannot proceed until they are completed.'

    # Check if any associated forecast jobs are running
    if ForecastRun.objects.filter(calibration_run=run, status__in=running_statuses).exists():
        return f'Calibration Job {run.id} has associated forecast jobs that are still running. Cannot proceed until they are completed.'

    # # Check if any associated forcing download jobs are running
    # if ForecastForcingDownloadRun.objects.filter(forecast_run__calibration_run=run, status__in=running_statuses).exists():
    #     return f'Calibration Job {run.id} has associated forcing download jobs that are still running. Cannot proceed until they are completed.'

    # No running jobs found
    return None


@extend_schema(
    request=CalibrationRunIdList,
    responses={
        200: CalibrationRunListResponse,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Delete a list of calibration jobs"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def delete_jobs(request: Request) -> Response:
    """
    Permanently delete multiple calibration jobs.

    :param request: The HTTP request object.
    :return: A Response object with the deletion status for each calibration job.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunIdList, data)
    if error_return:
        return error_return

    calibration_run_ids = validator.get('calibration_run_ids')

    job_results = []

    # Bulk fetch all the calibration jobs
    runs_by_id, errors_by_id = get_calibration_runs_bulk(
        calibration_run_ids=calibration_run_ids,
        user=request.user,
        run_status=list(StatusEnum),
        include_archived=False,
    )

    # Process each calibration_run_id in the list
    for calibration_run_id in calibration_run_ids:

        # Handle validation / access errors
        error = errors_by_id.get(calibration_run_id)
        if error:
            job_results.append({
                "message": error.data.get('message'),
                "calibration_run_id": calibration_run_id,
                "success": False,
            })
            continue

        run = runs_by_id[calibration_run_id]

        # Can't delete if the job is locked
        if run.is_locked:
            job_results.append({
                "message": f'Calibration Job {calibration_run_id} is locked for archiving/deleting',
                "calibration_run_id": calibration_run_id,
                "success": False
            })
            continue

        # Check for any running jobs (including the calibration job itself)
        running_jobs_error = has_running_associated_jobs(run)
        if running_jobs_error:
            job_results.append({
                "message": running_jobs_error,
                "calibration_run_id": calibration_run_id,
                "success": False
            })
            continue

        # Proceed with deletion
        hard_delete(run)

        job_results.append({
            "message": f"Calibration Job {calibration_run_id} and associated records have been deleted",
            "calibration_run_id": calibration_run_id,
            "success": True
        })

    response = {"jobs": job_results}

    response_validator, error_response = validate_response(CalibrationRunListResponse, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}()'
        f'{get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=ArchiveJobRequestSerializer,
    responses={
        200: CalibrationRunListResponse,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Archive or unarchive a list of calibration jobs"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def archive_jobs(request: Request) -> Response:
    """
    Archive or unarchive multiple calibration jobs.

    Archiving copies the local job directory to S3 and then removes the active
    local path. After the copy succeeds, the local directory is first renamed to
    a quarantine path on the same filesystem. This allows the archive to succeed
    once the active path is gone, even if immediate recursive deletion of the
    quarantined directory is delayed by NFS/EFS open-file behavior.

    Unarchiving restores the job directory from S3 back to local storage and
    then deletes the archived S3 objects only after the restore succeeds.

    A job is marked archived only after the requested operation reaches a safe
    completion point:
    - Archive: S3 copy completed and active local path moved out of the way.
    - Unarchive: Local restore verified and archived S3 objects removed.

    Note:
    Quarantined directories are not deleted in the request path after archive.
    They are left for a scheduled background cleanup process.

    :param request: The HTTP request object.
    :return: A Response object with per-job archive or unarchive results.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(ArchiveJobRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_ids = validator.get('calibration_run_ids')
    archive = validator.get('archive')

    if not settings.NGENCERF_ARCHIVE_S3_PATH:
        return ResponseError("NGENCERF_ARCHIVE_S3_PATH is undefined")

    # Make sure the configured archive root is a valid S3 prefix.
    try:
        s3_prefix = normalize_s3_prefix(settings.NGENCERF_ARCHIVE_S3_PATH)
    except ValueError as e:
        return ResponseError(f"NGENCERF_ARCHIVE_S3_PATH is invalid: {e}")

    try:
        exists = s3_prefix_exists(
            s3_prefix,
            profile_name=settings.NGENCERF_RW_PROFILE,
        )
    except S3CredentialsExpired as e:
        return ResponseError(str(e))
    except S3ProfileError as e:
        return ResponseError(str(e))
    except PermissionError as e:
        return ResponseError(str(e))

    if not exists:
        return ResponseError(
            f"NGENCERF_ARCHIVE_S3_PATH does not exist on S3: {s3_prefix}"
        )

    job_results = []

    # Bulk fetch all requested runs, including archived ones.
    runs_by_id, errors_by_id = get_calibration_runs_bulk(
        calibration_run_ids=calibration_run_ids,
        user=request.user,
        run_status=list(StatusEnum),
        include_archived=True,
    )

    # Process each calibration_run_id in the request.
    for calibration_run_id in calibration_run_ids:

        # Validation / access errors
        error = errors_by_id.get(calibration_run_id)
        if error:
            job_results.append({
                "message": error.data.get('message'),
                "calibration_run_id": calibration_run_id,
                "success": False
            })
            continue

        run = runs_by_id[calibration_run_id]

        # Can't archive or unarchive if the job is locked.
        if run.is_locked:
            job_results.append({
                "message": f'Calibration Job {calibration_run_id} is locked for archiving/deleting',
                "calibration_run_id": calibration_run_id,
                "success": False
            })
            continue

        # Reject requests that do not change the current archive state.
        if archive == run.is_archived:
            job_results.append({
                "message": f'Calibration Job {calibration_run_id} is {"already" if archive else "not"} archived',
                "calibration_run_id": calibration_run_id,
                "success": False
            })
            continue

        try:
            # ===============================
            # ARCHIVE (EFS → S3)
            # ===============================
            if archive:
                # Prevent archiving while the run or any child jobs are active.
                running_jobs_error = has_running_associated_jobs(run)
                if running_jobs_error:
                    job_results.append({
                        "message": running_jobs_error,
                        "calibration_run_id": calibration_run_id,
                        "success": False
                    })
                    continue

                src_path = run.job_data_dir  # e.g. /ngencerf/data/.../1_peter
                dst_prefix = join_url(
                    s3_prefix,
                    os.path.basename(src_path),
                )

                start = time.perf_counter()
                logger.info(f"Archiving Calibration Job {calibration_run_id}: copy {src_path} -> {dst_prefix}")

                # Archive copy:
                # - upload files to S3
                # - write manifest from source-side hashes only
                # - do not hash-read uploaded S3 objects back
                copied = copy_tree(
                    src_path,
                    dst_prefix,
                    verify=True,
                    profile_name=settings.NGENCERF_RW_PROFILE,
                )

                elapsed = time.perf_counter() - start
                logger.info(
                    f"Archived {copied} files for Calibration Job {calibration_run_id} "
                    f"to {dst_prefix} in {elapsed:.2f}s"
                )

                # ---- MOVE LOCAL DIRECTORY OUT OF THE ACTIVE PATH ----
                # Move the original active path out of the way before attempting deletion.
                # On NFS/EFS, open file handles (often exposed as '.nfs*' files) can prevent
                # immediate recursive deletion. By renaming first, the active job path is
                # removed deterministically, allowing the archive to succeed even if some
                # files cannot yet be deleted.
                quarantined_path = None
                if os.path.isdir(src_path):
                    try:
                        quarantined_path = move_tree_out_of_active_path(src_path)
                        logger.info(
                            f"Moved local directory out of active path after archive: "
                            f"{src_path} -> {quarantined_path}"
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to move local directory out of active path {src_path}: {e}"
                        )
                        raise

                # Do not synchronously delete quarantined archive directories here.
                # They are safe once moved out of the active path and will be
                # cleaned up later by the background cleanup job.
                if quarantined_path:
                    logger.info(
                        f"Deferred deletion of quarantined local directory to background cleanup: "
                        f"{quarantined_path}"
                    )


            # ===============================
            # UNARCHIVE (S3 → EFS)
            # ===============================
            else:
                src_cloud_prefix = join_url(
                    s3_prefix,
                    os.path.basename(run.job_data_dir)
                )

                dest_dir = run.job_data_dir

                # Remove any existing destination directory before restore.
                if os.path.isdir(dest_dir):
                    delete_tree_with_retries(dest_dir, attempts=3, delay_seconds=0.5)

                # Recreate the destination directory before copying into it.
                os.makedirs(dest_dir, exist_ok=True)

                start = time.perf_counter()
                logger.info(
                    f"Unarchiving Calibration Job {calibration_run_id}: "
                    f"copy {src_cloud_prefix} -> {dest_dir}"
                )

                # ---- COPY CLOUD → LOCAL ----
                copied = copy_tree(
                    src_cloud_prefix,
                    dest_dir,
                    verify=True,
                    profile_name=settings.NGENCERF_RW_PROFILE,
                )

                elapsed = time.perf_counter() - start
                logger.info(
                    f"Unarchived {copied} files for Calibration Job {calibration_run_id} "
                    f"into {dest_dir} in {elapsed:.2f} seconds"
                )

                # ---- DELETE CLOUD DIRECTORY AFTER SUCCESS ----
                try:
                    deleted = delete_all_s3_objects_under_prefix(
                        s3_dir_uri=src_cloud_prefix,
                        profile_name=settings.NGENCERF_RW_PROFILE,
                    )
                    logger.info(
                        f"Deleted {deleted} cloud object(s) after unarchive under: {src_cloud_prefix}"
                    )
                except Exception as e:
                    logger.error(f"Failed to delete cloud directory {src_cloud_prefix}: {e}")
                    raise

            # ------------------------------------------------------------
            # Update run flags + archive timestamp
            # ------------------------------------------------------------
            run.is_archived = archive
            run.archive_status_updated_at = datetime.now(tz=timezone.utc)
            run.save(update_fields=['is_archived', 'archive_status_updated_at'])

            job_results.append({
                'message': f'Calibration Job {calibration_run_id} has been '
                           f'{"archived" if archive else "unarchived"}',
                "calibration_run_id": calibration_run_id,
                "success": True
            })

        except Exception as e:
            logger.exception(f"Failed to {'archive' if archive else 'unarchive'} Calibration Job {calibration_run_id}: {e}")
            job_results.append({
                "message": f"Failed to {'archive' if archive else 'unarchive'} Calibration Job {calibration_run_id}: {e}",
                "calibration_run_id": calibration_run_id,
                "success": False
            })
            continue

    response = {"jobs": job_results}

    response_validator, error_response = validate_response(CalibrationRunListResponse, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(response_validator.data)}'
    )

    return Response(response_validator.data)


@extend_schema(
    request=LockJobRequestSerializer,
    responses={
        200: CalibrationRunListResponse,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Lock or unlock a list of calibration jobs"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def lock_jobs(request: Request) -> Response:
    """
    Lock or unlock multiple calibration jobs.  Locking a job prevents it from being deleted

    :param request: The HTTP request object.
    :return: A Response object with the lock/unlock status for each calibration job.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(LockJobRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_ids = validator.get('calibration_run_ids')
    lock = validator.get('lock')

    job_results = []

    # Bulk fetch all the calibration jobs
    runs_by_id, errors_by_id = get_calibration_runs_bulk(
        calibration_run_ids=calibration_run_ids,
        user=request.user,
        run_status=list(StatusEnum),
        include_archived=False,
    )

    # Process each calibration_run_id in the list
    for calibration_run_id in calibration_run_ids:
        # Validation / access errors
        error = errors_by_id.get(calibration_run_id)
        if error:
            job_results.append({
                "message": error.data.get('message'),
                "calibration_run_id": calibration_run_id,
                "success": False
            })
            continue

        run = runs_by_id[calibration_run_id]

        # Already locked / unlocked?
        if lock == run.is_locked:
            job_results.append({
                "message": f'Calibration Job {calibration_run_id} is {"already" if lock else "not"} locked',
                "calibration_run_id": calibration_run_id,
                "success": False
            })
            continue

        run.is_locked = lock
        run.save(update_fields=['is_locked'])

        job_results.append({
            'message': f'Calibration Job {calibration_run_id} has been {"locked" if lock else "unlocked"}',
            "calibration_run_id": calibration_run_id,
            "success": True
        })

    response = {"jobs": job_results}

    response_validator, error_response = validate_response(CalibrationRunListResponse, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}()'
        f'{get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


def hard_delete(run: CalibrationRun) -> None:
    """
    Perform a hard delete on a calibration run and its related records.

    This deletes the database record first, then attempts to remove the local
    job directory if it still exists.

    :param run: The CalibrationRun instance to be deleted.
    """
    job_data_dir = run.job_data_dir  # Stash before delete

    logger.debug(f"Deleting (hard delete) Calibration Job {run.id}, associated records and files")
    run.delete()

    logger.debug(f'Deleting directory {job_data_dir} for Calibration Job {run.id}')
    if job_data_dir and os.path.isdir(job_data_dir):
        try:
            delete_tree_with_retries(job_data_dir, attempts=3, delay_seconds=0.5)
        except Exception:
            logger.exception(
                f"Failed to delete job directory {job_data_dir} for Calibration Job {run.id}"
            )


@extend_schema(
    request=ImportSerializer,
    responses={
        200: ImportResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Import a job"
)
@api_view(['POST'])
@handle_exceptions
def import_job(request: Request) -> Response:
    """
    API endpoint to import (create) a calibration job or update an existing job.
     It validates input data, imports calibration run data, and optionally submits a job.

    :param request: Django HTTP request containing job import data.
    :return: HTTP response indicating success or error status.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')
    user_agent = request.META.get('HTTP_USER_AGENT', '')
    is_cli = user_agent.startswith(('curl', 'python-requests'))

    validator, error_return = validate_request(ImportSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    data = validator.get('data')
    run_after_import = data.get('run_after_import', False)

    if calibration_run_id:
        calibration_run, error_return = get_calibration_run(calibration_run_id, request.user)
        if error_return:
            return error_return
    else:
        calibration_run = None

    run, messages, errors = import_calibration_run_data(request, data, JobGenesis.IMPORT, run=calibration_run, is_cli=is_cli)
    if errors:
        return errors
    assert run is not None

    imported_and_submitted = 'updated' if calibration_run_id else 'imported'

    error_object, config_file = ngen_cal_input.ready_to_run(run)

    if run_after_import and (
            error_object is not None and
            not error_object.warnings and
            not error_object.errors
    ):
        error_response = submit_job(run)
        if error_response:
            return error_response
        imported_and_submitted = f"{imported_and_submitted} and submitted"

    response = {
        'message': f'Calibration Job {run.id} {imported_and_submitted}',
        'calibration_run_id': run.id,
        'status': run.status.name
    }

    if messages:
        response['messages'] = messages

    if error_object is not None:
        if error_object.warnings:
            response['warnings'] = error_object.warnings
        if error_object.errors:
            response['errors'] = error_object.errors

    response_validator, error_response = validate_response(ImportResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}'
    )
    return Response(response_validator.data)


def delete_tree_with_retries(path: str, attempts: int = 3, delay_seconds: float = 1.0) -> None:
    """
    Delete a directory tree with bounded retries for common NFS/EFS cleanup races.

    This helper is intended for cases where shutil.rmtree() may fail because a
    file in the tree is still open by another process. On NFS/EFS, that can
    surface as retryable errors such as ENOTEMPTY or EBUSY, often involving
    temporary '.nfs*' placeholder files.

    A missing path is treated as success. This avoids races where the directory
    disappears between an existence check and the delete attempt.

    On failure, this helper logs only the single path reported by the exception
    instead of recursively scanning the remaining tree, which keeps the failure
    path cheap to evaluate even for very large directories.

    :param path: The directory tree to delete.
    :param attempts: Maximum number of delete attempts.
    :param delay_seconds: Delay between retry attempts in seconds.
    :raises OSError: Re-raises the final deletion error if the tree cannot be removed.
    """
    last_exception: OSError | None = None

    # Treat an already-missing path as success.
    if not os.path.exists(path):
        return

    for attempt in range(1, attempts + 1):
        try:
            shutil.rmtree(path)
            return
        except OSError as exc:
            # The directory may have disappeared after the existence check or
            # between retry attempts. That is equivalent to successful deletion.
            if exc.errno == errno.ENOENT or not os.path.exists(path):
                return

            last_exception = exc

            # Retry only for the NFS/EFS-style cases that may clear once another
            # process releases an open file handle.
            is_retryable = exc.errno in {
                errno.ENOTEMPTY,
                errno.EBUSY,
            }

            failed_path = getattr(exc, 'filename', None)
            full_failed_path = (
                os.path.join(path, failed_path)
                if failed_path and not os.path.isabs(failed_path)
                else failed_path or path
            )

            is_nfs_placeholder = os.path.basename(full_failed_path).startswith('.nfs')

            logger.warning(
                f"Delete attempt {attempt}/{attempts} failed for {path}: "
                f"[Errno {exc.errno}] {exc}. "
                f"Failed path: {full_failed_path}. "
                f"NFS placeholder: {is_nfs_placeholder}"
            )

            # For non-retryable errors, or after the final attempt, let the caller
            # handle the failure and report the blocking path from this exception.
            if not is_retryable or attempt == attempts:
                raise

            time.sleep(delay_seconds)

    if last_exception:
        raise last_exception


def get_archived_pending_delete_path(path: str) -> str:
    """
    Build a sibling quarantine path used after a successful archive copy.

    The returned path stays on the same filesystem so that os.replace() can
    perform an atomic rename. A timestamp is included to reduce the risk of
    collisions if cleanup from an earlier archive attempt is still present.

    :param path: The original active job directory path.
    :return: A sibling quarantine path for the archived directory.
    """
    parent_dir = os.path.dirname(path)
    base_name = os.path.basename(path)
    timestamp = datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%S_%f')
    return os.path.join(parent_dir, f'{base_name}.__archived_pending_delete__.{timestamp}')


def move_tree_out_of_active_path(path: str) -> str:
    """
    Rename a directory to a quarantine path on the same filesystem.

    This is used after a successful archive copy so that the original active
    path is no longer present even if immediate recursive deletion of the
    directory may still fail due to NFS/EFS open-file behavior.

    :param path: The original active job directory path.
    :return: The new quarantine path.
    :raises FileNotFoundError: If the source path does not exist.
    :raises OSError: If the rename fails.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f'Path does not exist: {path}')

    quarantine_path = get_archived_pending_delete_path(path)

    # os.replace() performs an atomic rename on the same filesystem.
    os.replace(path, quarantine_path)

    return quarantine_path
