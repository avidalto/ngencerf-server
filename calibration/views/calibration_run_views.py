import json
import logging
import os
from datetime import datetime, timezone

import requests
from django.conf import settings
from django.db import transaction
from django.forms import model_to_dict
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiExample
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import StatusEnum, ValidationType
from calibration.enums_vanilla import JobType, SecondaryDataEnum
from calibration.models import Iteration, ValidationRun, ForecastRun, CalibrationRun, Status, ColdStartRun, VerificationRun
from calibration.models.base_run import BaseRun
from calibration.run_util.run_common import cancel_job_common, submit_job
from calibration.run_util.run_ngen_cal_pw import SlurmCallbackStatusEnum, run_calibration_job_callback_pw, run_validation_job_callback_pw, \
    run_forecast_job_callback_pw, run_cold_start_job_callback_pw, run_verification_job_callback_pw
from calibration.util.calibration_validators import CalibrationRunSerializer, GenericResponseSerializer, \
    ErrorResponseSerializer, ReportIterationSerializer, SubmitCalibrationJobResponseSerializer, GetIterationsResponseSerializer, \
    CalibrationJobSlurmCallbackRequestSerializer, ValidationJobSlurmCallbackRequestSerializer, EmptySerializer, \
    GetStatusForCalibrationResponseSerializer, GetStatusForComparisonRequestSerializer, GetStatusForComparisonResponseSerializer, \
    CalibrationOrValidationOrColdStartOrForecastOrVerificationRunSerializer, ForecastJobSlurmCallbackRequestSerializer, CancelJobResponseSerializer, \
    ValidationRunSerializer, GenericResponseSerializerWithValidator, RunCalibrationJob, MPINodesRulesSerializer, MPINodesRulesResponseSerializer, \
    ColdStartJobSlurmCallbackRequestSerializer, VerificationJobSlurmCallbackRequestSerializer, GetStatusForValidationResponseSerializer, \
    GetStatusForForecastResponseSerializer, GetStatusForVerificationResponseSerializer, GetStatusRequestSerializer
from calibration.views import ngen_cal_input
from calibration.views.calibration_secondary_data_views import generate_secondary_ts_data
from calibration.views.called_from import get_caller_name
from calibration.views.common import ResponseError, get_calibration_run, handle_exceptions, validate_response, validate_request, \
    generate_custom_token, TOKEN_SLURM_SCOPE, get_validation_run, get_forecast_run, get_user_email, \
    get_job_description, get_elapsed_str, readonly_transaction, auth_scope_required, get_cold_start_run, get_verification_run, \
    join_with_or, get_calibration_runs_bulk
from calibration.views.end_of_job_processing import read_calibration_output

logger = logging.getLogger(__name__)


def normalize_failure_messages(value) -> list[dict]:
    """
    Normalize failure_messages into a canonical list[dict] form.

    Accepts:
      - None
      - JSON string (dict or list)
      - dict
      - list
      - legacy string

    Returns:
      - list[dict]
    """
    if value is None:
        return []

    # Parse JSON if needed
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return [{
                "source": "legacy",
                "message": value,
            }]

    if isinstance(value, dict):
        return [value]

    if isinstance(value, list):
        return value

    # Defensive fallback
    return [{
        "source": "unknown",
        "message": str(value),
    }]


@extend_schema(
    request=GetStatusRequestSerializer,
    responses={
        200: GetStatusForCalibrationResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Return the status of a calibration job and associated validation and forecast jobs"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_status(request: Request) -> Response:
    """
    Retrieves the status of a calibration, validation, forecast, or verification job.
    Optionally includes performance metrics based on the request parameters.
    Runs in READ ONLY mode to avoid locking contention.

    Read-heavy operations are executed inside a readonly transaction to minimize
    locking. If Slurm reconciliation is required, the necessary database update
    is performed outside the readonly transaction.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(GetStatusRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    validation_run_id = validator.get('validation_run_id')
    forecast_run_id = validator.get('forecast_run_id')
    verification_run_id = validator.get('verification_run_id')

    include_performance_metrics = validator.get('include_performance_metrics')

    if calibration_run_id:
        serializer_class = GetStatusForCalibrationResponseSerializer
    elif validation_run_id:
        serializer_class = GetStatusForValidationResponseSerializer
    elif forecast_run_id:
        serializer_class = GetStatusForForecastResponseSerializer
    else:
        serializer_class = GetStatusForVerificationResponseSerializer

    # Values captured during readonly phase
    run = None
    needs_reconcile = False
    sacct_status = None

    # ─────────────────────────────────────────────────────────────
    # READ-ONLY PHASE
    # ─────────────────────────────────────────────────────────────
    with readonly_transaction():
        if calibration_run_id:
            run, error_return = get_calibration_run(
                calibration_run_id, request.user, run_status=list(StatusEnum)
            )
            if error_return:
                return error_return

            logger.info('Calling check_slurm_reconciliation')
            needs_reconcile, sacct_status = check_slurm_reconciliation(run)
            logger.info(f"Needs_reconcile={needs_reconcile} sacct_status={sacct_status}")

            response = get_status_for_calibration(run, include_performance_metrics)

        elif validation_run_id:
            run, error_return = get_validation_run(
                validation_run_id, request.user, run_status=list(StatusEnum)
            )
            if error_return:
                return error_return

            needs_reconcile, sacct_status = check_slurm_reconciliation(run)
            response = get_status_for_validation(run, include_performance_metrics)

        elif forecast_run_id:
            # Handle cold start
            run, error_return = get_forecast_run(
                forecast_run_id, request.user, run_status=list(StatusEnum)
            )
            if error_return:
                return error_return

            needs_reconcile, sacct_status = check_slurm_reconciliation(run)
            response = get_status_for_forecast(run, include_performance_metrics)

        else:
            run, error_return = get_verification_run(
                verification_run_id, request.user, run_status=list(StatusEnum)
            )
            if error_return:
                return error_return

            needs_reconcile, sacct_status = check_slurm_reconciliation(run)
            response = get_status_for_verification(run, include_performance_metrics)

    # TODO Can we combine these?
    # ---------------------------------------------------
    # WRITE-CAPABLE PHASE (calibration only, conditional)
    # ---------------------------------------------------
    if calibration_run_id:
        if run.status in [StatusEnum.SAVED.db_instance, StatusEnum.READY.db_instance]:
            error_object, _ = ngen_cal_input.ready_to_run(run)
            if error_object:
                # mutate response dict only, not DB objects here
                if error_object.has_warnings():
                    response["warnings"] = error_object.warnings
                if error_object.has_errors():
                    response["errors"] = error_object.errors
    # ─────────────────────────────────────────────────────────────
    # WRITE PHASE (ONLY IF NECESSARY)
    # ─────────────────────────────────────────────────────────────
    if needs_reconcile:
        logger.info('reconciling')

        with transaction.atomic():
            # Re-fetch the row outside readonly_transaction before mutating
            run = type(run).objects.select_for_update().get(id=run.id)
            apply_slurm_reconciliation(run, sacct_status)
            # Update some fields that were placed by get_status_for_xxx
            response["status"] = StatusEnum.SERVER_ERROR.value
            response["message"] = (
                f"{get_job_description(run)} status updated to SERVER_ERROR "
                f"due to Slurm inconsistency"
            )
            response["failure_messages"] = normalize_failure_messages(run.failure_messages)

    response_validator, error_response = validate_response(serializer_class, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(response_validator.data)}'
    )

    return Response(response_validator.data)


def get_status_for_calibration(calibration_run: CalibrationRun, include_performance_metrics: bool) -> dict:
    """
    Return the current status of a single CalibrationRun.

    This includes:
    - Core calibration timing and status fields
    - Failure messages (if any)
    - Performance metrics (only if requested and job is DONE or FAILED)
    - Status summaries for associated BEST and CONTROL ValidationRuns

    All database access is read-only and executed inside a readonly transaction
    to avoid write contention.

    :param calibration_run: The CalibrationRun instance to inspect.
    :param include_performance_metrics: Whether to include performance metrics
        when the run status allows it.
    :return: A dict suitable for GetStatusForCalibrationResponseSerializer.
    """

    # Conditionally retrieve calibration performance metrics
    calibration_metrics = (
        get_performance_metrics(calibration_run.performance_metrics)
        if should_include_metrics(calibration_run.status, include_performance_metrics)
        else None
    )

    # --- Validation runs - only BEST and CONTROL ---
    validation_runs = (
        ValidationRun.objects
        .filter(
            calibration_run_id=calibration_run.id,
            validation_type__in=[ValidationType.VALID_CONTROL.value, ValidationType.VALID_BEST.value]
        )
        .select_related("status", "performance_metrics")
        .order_by("id")
    )

    # --- Validation responses ---
    validation_response = []
    for validation_run in validation_runs:
        validation_data = {
            'validation_run_id': validation_run.id,
            'status': validation_run.status.name,
            'validation_type': validation_run.validation_type,
            'iteration_num': validation_run.iteration_num,
            'submit_date': validation_run.submit_date,
            'sent_date': validation_run.sent_date,
            'run_start': validation_run.run_start,
            'run_end': validation_run.run_end,
        }

        validation_failure_message = normalize_failure_messages(validation_run.failure_messages)
        if validation_failure_message:
            validation_data['failure_messages'] = validation_failure_message

        if validation_run.run_end and validation_run.submit_date:
            validation_data['elapsed_time'] = validation_run.run_end - validation_run.submit_date

        validation_metrics = (
            get_performance_metrics(validation_run.performance_metrics)
            if should_include_metrics(validation_run.status, include_performance_metrics)
            else None
        )
        if validation_metrics:
            validation_data['performance_metrics'] = validation_metrics

        validation_response.append(validation_data)

    calibration_data = {
        'message': f'{get_job_description(calibration_run)}, status is {calibration_run.status.name}',
        'calibration_run_id': calibration_run.id,
        'status': calibration_run.status.name,
        'submit_date': calibration_run.submit_date,
        'sent_date': calibration_run.sent_date,
        'run_start': calibration_run.run_start,
        'run_end': calibration_run.run_end,
        'validations': validation_response,
    }

    calibration_failure_message = normalize_failure_messages(calibration_run.failure_messages)
    if calibration_failure_message:
        calibration_data['failure_messages'] = calibration_failure_message

    if calibration_run.run_end and calibration_run.submit_date:
        calibration_data['elapsed_time'] = calibration_run.run_end - calibration_run.submit_date

    # Conditionally add calibration run performance metrics to calibration_data if requested and status is DONE or FAIL
    if calibration_metrics:
        calibration_data['performance_metrics'] = calibration_metrics

    return calibration_data


def get_status_for_validation(validation_run: ValidationRun, include_performance_metrics: bool) -> dict:
    """
    Return the current status of a single ValidationRun.

    This includes:
    - Validation timing and status fields
    - Failure messages (if any)
    - Performance metrics (only if requested and job is DONE or FAILED)

    All database access is read-only and executed inside a readonly transaction.

    :param validation_run: The ValidationRun instance to inspect.
    :param include_performance_metrics: Whether to include performance metrics
        when the run status allows it.
    :return: A dict suitable for GetStatusForValidationResponseSerializer.
    """

    # Conditionally retrieve performance metrics
    validation_metrics = (
        get_performance_metrics(validation_run.performance_metrics)
        if should_include_metrics(validation_run.status, include_performance_metrics)
        else None
    )

    validation_data = {
        'message': f'{get_job_description(validation_run)}, status is {validation_run.status.name}',
        'calibration_run_id': validation_run.calibration_run.id,
        'validation_run_id': validation_run.id,
        'status': validation_run.status.name,
        'validation_type': validation_run.validation_type,
        'iteration_num': validation_run.iteration_num,
        'submit_date': validation_run.submit_date,
        'sent_date': validation_run.sent_date,
        'run_start': validation_run.run_start,
        'run_end': validation_run.run_end
    }

    validation_failure_message = normalize_failure_messages(validation_run.failure_messages)
    if validation_failure_message:
        validation_data['failure_messages'] = validation_failure_message

    if validation_run.run_end and validation_run.submit_date:
        validation_data['elapsed_time'] = validation_run.run_end - validation_run.submit_date

    # Conditionally add calibration run performance metrics to calibration_data if requested and status is DONE or FAIL
    if validation_metrics:
        validation_data['performance_metrics'] = validation_metrics

    return validation_data


def get_status_for_forecast(forecast_run: ForecastRun, include_performance_metrics: bool) -> dict:
    """
    Return the current status of a single ForecastRun.

    This includes:
    - Forecast timing, configuration, and status fields
    - Failure messages (if any)
    - Performance metrics (only if requested and job is DONE or FAILED)
    - Cold start run status and metrics, if a cold start exists

    All database access is read-only and executed inside a readonly transaction.

    :param forecast_run: The ForecastRun instance to inspect.
    :param include_performance_metrics: Whether to include performance metrics
        when the run status allows it.
    :return: A dict suitable for GetStatusForForecastResponseSerializer.
    """

    forecast_data = {
        'message': f'{get_job_description(forecast_run)}, status is {forecast_run.status.name}',
        'forecast_run_id': forecast_run.id,
        'calibration_run_id': forecast_run.calibration_run_id,
        'status': forecast_run.status.name,
        'configuration': forecast_run.configuration.name,
        'cycle_date': forecast_run.cycle_date,
        'submit_date': forecast_run.submit_date,
        'sent_date': forecast_run.sent_date,
        'run_start': forecast_run.run_start,
        'run_end': forecast_run.run_end
    }

    forecast_failure_message = normalize_failure_messages(forecast_run.failure_messages)
    if forecast_failure_message:
        forecast_data['failure_messages'] = forecast_failure_message

    if forecast_run.run_end and forecast_run.submit_date:
        forecast_data['elapsed_time'] = forecast_run.run_end - forecast_run.submit_date

    forecast_metrics = (
        get_performance_metrics(forecast_run.performance_metrics)
        if should_include_metrics(forecast_run.status, include_performance_metrics)
        else None
    )
    if forecast_metrics:
        forecast_data['performance_metrics'] = forecast_metrics

    # Get the cold start run, if it's there
    cold_start_run = forecast_run.cold_start_run
    if cold_start_run:
        cold_start_data = {
            'cold_start_run_id': cold_start_run.id,
            'status': cold_start_run.status.name,
            'submit_date': cold_start_run.submit_date,
            'sent_date': cold_start_run.sent_date,
            'run_start': cold_start_run.run_start,
            'run_end': cold_start_run.run_end,
        }

        cold_start_failure_message = normalize_failure_messages(cold_start_run.failure_messages)
        if cold_start_failure_message:
            cold_start_data['failure_messages'] = cold_start_failure_message

        if cold_start_run.run_end and cold_start_run.submit_date:
            cold_start_data['elapsed_time'] = cold_start_run.run_end - cold_start_run.submit_date

        cold_start_metrics = (
            get_performance_metrics(cold_start_run.performance_metrics)
            if should_include_metrics(cold_start_run.status, include_performance_metrics)
            else None
        )
        if cold_start_metrics:
            cold_start_data['performance_metrics'] = cold_start_metrics

        forecast_data['cold_start_run'] = cold_start_data

    return forecast_data


def get_status_for_verification(verification_run: VerificationRun, include_performance_metrics: bool) -> dict:
    """
    Return the current status of a single VerificationRun.

    This includes:
    - Verification timing and status fields
    - Failure messages (if any)
    - Performance metrics (only if requested and job is DONE or FAILED)
    - A summarized view of the associated ForecastRun

    All database access is read-only and executed inside a readonly transaction.

    :param verification_run: The VerificationRun instance to inspect.
    :param include_performance_metrics: Whether to include performance metrics
        when the run status allows it.
    :return: A dict suitable for GetStatusForVerificationResponseSerializer.
    """

    verification_data = {
        'message': f'{get_job_description(verification_run)}, status is {verification_run.status.name}',
        'verification_run_id': verification_run.id,
        'calibration_run_id': verification_run.forecast_run.calibration_run_id,
        'status': verification_run.status.name,
        'submit_date': verification_run.submit_date,
        'sent_date': verification_run.sent_date,
        'run_start': verification_run.run_start,
        'run_end': verification_run.run_end
    }

    verification_failure_message = normalize_failure_messages(verification_run.failure_messages)
    if verification_failure_message:
        verification_data['failure_messages'] = verification_failure_message

    if verification_run.run_end and verification_run.submit_date:
        verification_data['elapsed_time'] = verification_run.run_end - verification_run.submit_date

    verification_metrics = (
        get_performance_metrics(verification_run.performance_metrics)
        if should_include_metrics(verification_run.status, include_performance_metrics)
        else None
    )
    if verification_metrics:
        verification_data['performance_metrics'] = verification_metrics

    # Get the forecast run, which should always be there
    forecast_run = verification_run.forecast_run
    forecast_data = {
        'forecast_run_id': forecast_run.id,
        'status': forecast_run.status.name,
        'configuration': forecast_run.configuration.name,
        'cycle_date': forecast_run.cycle_date,
        'submit_date': forecast_run.submit_date,
        'sent_date': forecast_run.sent_date,
        'run_start': forecast_run.run_start,
        'run_end': forecast_run.run_end,
    }

    forecast_failure_message = normalize_failure_messages(forecast_run.failure_messages)
    if forecast_failure_message:
        forecast_data['failure_messages'] = forecast_failure_message

    if forecast_run.run_end and forecast_run.submit_date:
        forecast_data['elapsed_time'] = forecast_run.run_end - forecast_run.submit_date

    forecast_metrics = (
        get_performance_metrics(forecast_run.performance_metrics)
        if should_include_metrics(forecast_run.status, include_performance_metrics)
        else None
    )
    if forecast_metrics:
        forecast_data['performance_metrics'] = forecast_metrics

    verification_data['forecast_run'] = forecast_data

    return verification_data


@extend_schema(
    request=GetStatusForComparisonRequestSerializer,
    responses={
        200: GetStatusForComparisonResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Return the status of a calibration job and associated validation and forecast jobs"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_status_for_comparison(request: Request) -> Response:
    """
    Retrieves the status of multiple calibration jobs, including performance metrics.
    calibration_run_ids should be given as an array.
    Runs in READ ONLY mode to avoid locking contention.

    :param request: HTTP request containing calibration run details.
    :return: JSON response with the status and associated job details.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(GetStatusForComparisonRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_ids = validator.get('calibration_run_ids')

    response = {
        'calibration_run_ids': calibration_run_ids,
        'statuses': [],
        'errors': []
    }

    with readonly_transaction():
        runs_by_id, errors_by_id = get_calibration_runs_bulk(
            calibration_run_ids=calibration_run_ids,
            user=request.user,
            run_status=list(StatusEnum),
            include_archived=False,
        )

        for calibration_run_id in calibration_run_ids:
            if calibration_run_id in errors_by_id:
                response['errors'].append({
                    'calibration_run_id': calibration_run_id,
                    'message': errors_by_id[calibration_run_id],
                })
                continue

            calibration_run = runs_by_id[calibration_run_id]

            calibration_metrics = (
                get_performance_metrics(calibration_run.performance_metrics)
                if calibration_run.status in [StatusEnum.DONE.db_instance, StatusEnum.FAILED.db_instance]
                else None
            )
            # Prepare the response for this job
            status_response = {
                'calibration_run_id': calibration_run.id,
                'formulation_name': calibration_run.user_formulation_name,
                'status': calibration_run.status.name,
                'submit_date': calibration_run.submit_date,
                'run_start': calibration_run.run_start,
                'run_end': calibration_run.run_end,
            }

            if calibration_run.run_end and calibration_run.submit_date:
                status_response['elapsed_time'] = (
                        calibration_run.run_end - calibration_run.submit_date
                )

            if calibration_metrics:
                status_response['performance_metrics'] = calibration_metrics

            response['statuses'].append(status_response)

    response_validator, error_response = validate_response(GetStatusForComparisonResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(response_validator.data)}'
    )

    return Response(response_validator.data)


@extend_schema(
    request=RunCalibrationJob,
    responses={
        200: SubmitCalibrationJobResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Run a calibration"
)
@api_view(['POST'])
@handle_exceptions
def run_calibration(request: Request) -> Response:
    """
    Submits a calibration job for processing.

    :param request: HTTP request containing calibration run details.
    :return: JSON response indicating job submission status.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(RunCalibrationJob, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    logging_config = validator.get('logging_config')

    run, error_return = get_calibration_run(calibration_run_id, request.user)
    if error_return:
        return error_return

    error_response = submit_job(run, logging_config=logging_config)
    if error_response:
        return error_response

    response = {'message': f'Calibration Job {run.id} has been submitted',
                'calibration_run_id': calibration_run_id,
                'status': run.status.name,
                'submit_date': run.submit_date}

    response_validator, error_return = validate_response(SubmitCalibrationJobResponseSerializer, response)
    if error_return:
        return error_return

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


def get_performance_metrics(performance_metrics) -> dict[str, str | int | float | None]:
    """
    Helper function to retrieve selected performance metrics, converting numeric fields to 'K' units.
    """
    if not performance_metrics:
        return {field: None for field in [
            "run_time", "num_cpus", "cpu_time", "max_rss", "max_disk_read", "max_disk_write", "reserved_time", "io_throughput"
        ]}

    # Convert numeric fields to kilobytes
    metrics_dict = model_to_dict(performance_metrics, fields=[
        "run_time", "num_cpus", "cpu_time", "max_rss", "max_disk_read", "max_disk_write", "reserved_time"
    ])
    # Manually add io_throughput since it's a generated field
    metrics_dict["io_throughput"] = performance_metrics.io_throughput

    # Convert relevant fields to 'K' units
    for field in ["max_rss", "max_disk_read", "max_disk_write"]:
        value = metrics_dict.get(field)
        if value is not None:  # Only convert non-null values
            metrics_dict[field] = f"{value:.2f}K"

    # Format io_throughput in 'K/s'
    io_throughput = metrics_dict.get("io_throughput")
    if io_throughput is not None:
        metrics_dict["io_throughput"] = f"{io_throughput:.2f}K/s"

    return metrics_dict


def should_include_metrics(run_status: Status, include_performance_metrics: bool = False) -> bool:
    """
    Determines if performance metrics should be included based on job status and request parameters.
    """
    return include_performance_metrics and run_status in [StatusEnum.DONE.db_instance, StatusEnum.FAILED.db_instance]


@extend_schema(
    request=CalibrationRunSerializer,
    responses={
        200: GenericResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Process the output of a calibration run"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def process_calibration_output(request):
    """
    This endpoint is mostly for testing, to kick off the processing of output for a completed job.
    Normally read_calibration_output() is called automatically when a job completes.
    This endpoint can be used in case the output processing doesn't work.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()

    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')
    validator, error_return = validate_request(CalibrationRunSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')

    run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.DONE, StatusEnum.FAILED])

    if error_return:
        return error_return

    read_calibration_output(run, False)

    response = {'message': f"End of job processing completed for Calibration Job {run.id}",
                'calibration_run_id': run.id,
                'status': run.status.name}

    response_validator, error_response = validate_response(GenericResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=ValidationRunSerializer,
    responses={
        200: GenericResponseSerializerWithValidator,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Process the output of a calibration run"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def process_swe_timeseries(request: Request) -> Response:
    """
    This endpoint is mostly for testing, to kick off the processing of the SWE timeseries for a  completed job.
    Normally generate_secondary_ts_data() is called automatically when a job completes.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()

    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')
    validator, error_return = validate_request(ValidationRunSerializer, data)
    if error_return:
        return error_return

    validation_run_id = validator.get('validation_run_id')

    run, error_return = get_validation_run(validation_run_id, request.user, run_status=[StatusEnum.DONE])

    if error_return:
        return error_return

    generate_secondary_ts_data(run, SecondaryDataEnum.SWE)

    response = {'message': f"SWE Timeseries processing completed for Validation Job {run.id}",
                'validation_run_id': run.id,
                'status': run.status.name}

    response_validator, error_response = validate_response(GenericResponseSerializerWithValidator, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)



@extend_schema(
    request=ReportIterationSerializer,
    responses={
        200: GenericResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Report iteration of a running calibration"
)
# Called by cal-mgr
@api_view(['POST'])
@handle_exceptions
def report_iteration(request):
    """
    Reports an iteration for a running calibration job. This endpoint updates or creates an
    iteration record for a specific worker in the calibration job.

    Concurrency considerations:
    - Each CalibrationRun has a `next_worker_number` counter that is incremented atomically
      under `select_for_update()`. This guarantees that two new workers starting at the same
      time are serialized and each receives a unique worker number.
    - Once assigned, a worker number is reused for all iterations of that worker in the run.
    - The (iteration_num, worker_name, calibration_run) uniqueness constraint ensures that
      a worker cannot report the same iteration twice.

    Transaction strategy:
    - For new workers, the row lock on CalibrationRun ensures safe allocation of a worker number.
    - For existing workers, we only look up their latest iteration to reuse the same worker number.
    - The actual insert (via get_or_create) is inside the same atomic block to prevent duplicates.

    :param request: HTTP request containing iteration details.
    :return: JSON response indicating the success of the operation.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(ReportIterationSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    iteration_number = validator.get('iteration')
    worker_name = validator.get('worker_name')
    first_iteration_for_worker = validator.get('first_iteration_for_worker')

    logger.debug(
        f"Report Iteration for calibration_run_id {calibration_run_id}, iteration number: {iteration_number}, "
        f"worker: {worker_name}, first_iteration: {first_iteration_for_worker}"
    )

    run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.RUNNING])
    if error_return:
        return error_return

    with transaction.atomic():
        if first_iteration_for_worker:
            # Atomically grab the next available worker number
            run = CalibrationRun.objects.select_for_update().get(id=run.id)
            worker_number = run.next_worker_number
            run.next_worker_number += 1
            run.save(update_fields=['next_worker_number'])
            logger.debug(f"Assigned new worker: '{worker_name}' #{worker_number}")
        else:
            # Use get() to fetch the latest iteration for the given worker_name and run
            existing_iteration = (
                Iteration.objects
                .filter(calibration_run=run, worker_name=worker_name)
                .only("worker_number")
                .order_by('-iteration_num').first()
            )
            if existing_iteration:
                worker_number = existing_iteration.worker_number
            else:
                return ResponseError(f"Worker '{worker_name}' not found for calibration run {run.id}.")

        iteration_object, created = Iteration.objects.get_or_create(
            calibration_run=run,
            iteration_num=iteration_number,
            worker_name=worker_name,
            defaults={'worker_number': worker_number}
        )
        if not created:
            return ResponseError(
                f'Iteration object already exists for calibration run {run.id}, worker {worker_name}, iteration {iteration_number}'
            )

    response = {
        'message': f"Iteration {iteration_number} for worker_name '{worker_name}' set for Calibration Job {run.id}",
        'calibration_run_id': run.id,
        'status': run.status.name
    }

    response_validator, error_response = validate_response(GenericResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=CalibrationRunSerializer,
    responses={
        200: GetIterationsResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get iteration of a running calibration"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_iteration(request: Request) -> Response:
    """
    Retrieves the current iteration of a running calibration job.
    Runs in READ ONLY mode to avoid locking contention.

    :param request: HTTP request containing calibration run details.
    :return: JSON response with the current iteration details.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')

    with readonly_transaction():
        run, error_return = get_calibration_run(
            calibration_run_id,
            request.user,
            run_status=[StatusEnum.SUBMITTED, StatusEnum.RUNNING, StatusEnum.DONE,
                        StatusEnum.FAILED, StatusEnum.CANCELLED, StatusEnum.SERVER_ERROR]
        )
        if error_return:
            return error_return

        high_iteration = Iteration.objects.filter(calibration_run=run, worker_number=1).order_by('-iteration_num').first()
        high_iteration_number = high_iteration.iteration_num if high_iteration else None

        response = {'message': f'Calibration Job {run.id} has completed {high_iteration_number} iterations',
                    'calibration_run_id': run.id,
                    'status': run.status.name,
                    'iteration': high_iteration_number}

        response_validator, error_response = validate_response(GetIterationsResponseSerializer, response)
        if error_response:
            return error_response
        logger.debug(
            f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=CalibrationOrValidationOrColdStartOrForecastOrVerificationRunSerializer,
    responses={
        200: GenericResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Cancel a running job"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def cancel_job(request: Request) -> Response:
    """
    Cancel a running job for CalibrationRun, ValidationRun, or ForecastRun.

    :param request: The HTTP request containing the run ID to cancel.
    :return: A Response indicating the cancellation result.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationOrValidationOrColdStartOrForecastOrVerificationRunSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    validation_run_id = validator.get('validation_run_id')
    forecast_run_id = validator.get('forecast_run_id')
    verification_run_id = validator.get('verification_run_id')

    # Determine job type and retrieve the appropriate run instance
    if calibration_run_id:
        run_type = JobType.CALIBRATION.value
        run, error_return = get_calibration_run(
            calibration_run_id, request.user, run_status=[StatusEnum.RUNNING, StatusEnum.SUBMITTED]
        )
        if error_return:
            return error_return

    elif validation_run_id:
        run_type = JobType.VALIDATION.value
        run, error_return = get_validation_run(
            validation_run_id, request.user, run_status=[StatusEnum.RUNNING, StatusEnum.SUBMITTED]
        )
        if error_return:
            return error_return

    elif verification_run_id:
        run_type = JobType.VERIFICATION.value
        run, error_return = get_verification_run(
            verification_run_id, request.user, run_status=[StatusEnum.RUNNING, StatusEnum.SUBMITTED]
        )
        if error_return:
            return error_return

    else:
        # --------------------
        # FORECAST LOGIC ONLY
        # --------------------
        forecast_run, error_return = get_forecast_run(
            forecast_run_id, request.user, run_status=list(StatusEnum)
        )
        if error_return:
            return error_return

        cold_start_run = forecast_run.cold_start_run

        # --- CASE 1: No cold start at all → cancel forecast directly ---
        if cold_start_run is None:
            if forecast_run.status in [StatusEnum.RUNNING.db_instance,
                                       StatusEnum.SUBMITTED.db_instance]:
                run_type = JobType.FORECAST.value
                run = forecast_run
            else:
                error = (
                    f'{ForecastRun.__name__} {forecast_run.id} is not in an allowed status: '
                    f'{join_with_or([StatusEnum.RUNNING.value, StatusEnum.SUBMITTED.value])}. '
                    f'Current status: {forecast_run.status.name}'
                )
                return ResponseError(error)

        else:
            # --- CASE 2: Cold start is running/submitted → cancel cold start ---
            if cold_start_run.status in [StatusEnum.RUNNING.db_instance, StatusEnum.SUBMITTED.db_instance]:
                # Cold start job is running, so cancel it
                run_type = JobType.COLD_START.value
                run = cold_start_run

            # --- CASE 3: Cold start DONE → cancel forecast (if running/submitted) ---
            elif cold_start_run.status == StatusEnum.DONE.db_instance:
                # Cold start is done, check the status of the forecast job
                if forecast_run.status in [StatusEnum.RUNNING.db_instance, StatusEnum.SUBMITTED.db_instance]:
                    run_type = JobType.FORECAST.value
                    run = forecast_run
                else:
                    error = (
                        f'{ForecastRun.__name__} {forecast_run.id} is not in an allowed status: '
                        f'{join_with_or([StatusEnum.RUNNING.value, StatusEnum.SUBMITTED.value])}. '
                        f'Current status: {forecast_run.status.name}'
                    )
                    return ResponseError(error)

            # --- CASE 4: Cold start exists but in an invalid state ---
            else:
                error = (
                    f'{ColdStartRun.__name__} {cold_start_run.id} is not in an allowed status: '
                    f'{join_with_or([StatusEnum.RUNNING.value, StatusEnum.SUBMITTED.value])}. '
                    f'Current status: {cold_start_run.status.name}'
                )
                return ResponseError(error)

    # --------------------
    # COMMON CANCEL LOGIC
    # --------------------
    if not cancel_job_common(run):
        return ResponseError(f"Unable to cancel {run_type.capitalize()} Job {run.id}")

    run.status = StatusEnum.CANCELLED.db_instance
    run.save(update_fields=['status'])

    response = {
        'message': f"{get_job_description(run)} has been canceled",
        f"{run_type}_run_id": run.id,
        'status': run.status.name  # type: ignore[attr-defined]
    }
    response_validator, error_response = validate_response(CancelJobResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


def resolve_job_data_dir(run: CalibrationRun) -> str:
    """
    Resolves the job data directory for the given CalibrationRun object, converting paths if necessary
    based on the current settings.

    :param run: The CalibrationRun object.
    :return: The resolved host path to the job data directory as a plain string.
    :raises ValueError: If the path is not absolute or does not start with the expected root.
    """
    container_job_data_dir: str = run.job_data_dir

    if settings.NGEN_CAL_DATA_PATH and settings.NGEN_CAL_DATA_PATH != settings.NGEN_CAL_MOUNT_POINT:
        # Ensure the absolute path starts with the old root
        if not os.path.isabs(container_job_data_dir):
            raise ValueError(f"The path '{container_job_data_dir}' is not absolute.")
        if not container_job_data_dir.startswith(settings.NGEN_CAL_MOUNT_POINT):
            raise ValueError(f"The path '{container_job_data_dir}' does not start with the old root '{settings.NGEN_CAL_MOUNT_POINT}'.")

        # Replace the old root with the new root
        relative_path = os.path.relpath(container_job_data_dir, start=settings.NGEN_CAL_MOUNT_POINT)
        return os.path.join(settings.NGEN_CAL_DATA_PATH, relative_path)

    return container_job_data_dir


@extend_schema(
    request=CalibrationJobSlurmCallbackRequestSerializer,
    responses={
        202: None,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Callback for Slurm to call when a calibration job ends"
)
@api_view(['POST'])
@handle_exceptions
@auth_scope_required(TOKEN_SLURM_SCOPE)
def calibration_job_slurm_callback(request: Request) -> Response:
    """
    Handles a callback from Slurm to update the status of a calibration job.

    :param request: HTTP request containing Slurm job details and status.
    :return: HTTP 202 response indicating the callback was processed.
    """
    return handle_slurm_callback(
        request,
        CalibrationJobSlurmCallbackRequestSerializer,
        get_calibration_run,
        run_calibration_job_callback_pw
    )


@extend_schema(
    request=ValidationJobSlurmCallbackRequestSerializer,
    responses={
        202: None,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Callback for Slurm to call when a validation job ends"
)
@api_view(['POST'])
@handle_exceptions
@auth_scope_required(TOKEN_SLURM_SCOPE)
def validation_job_slurm_callback(request: Request) -> Response:
    """
    Handles a callback from Slurm to update the status of a validation job.

    :param request: HTTP request containing Slurm job details and status.
    :return: HTTP 202 response indicating the callback was processed.
    """
    return handle_slurm_callback(
        request,
        ValidationJobSlurmCallbackRequestSerializer,
        get_validation_run,
        run_validation_job_callback_pw
    )


@extend_schema(
    request=ColdStartJobSlurmCallbackRequestSerializer,
    responses={
        202: None,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Callback for Slurm to call when a cold start job ends"
)
@api_view(['POST'])
@handle_exceptions
@auth_scope_required(TOKEN_SLURM_SCOPE)
def cold_start_job_slurm_callback(request: Request) -> Response:
    """
    Handles a callback from Slurm to update the status of a cold start job.

    :param request: HTTP request containing Slurm job details and status.
    :return: HTTP 202 response indicating the callback was processed.
    """
    return handle_slurm_callback(
        request,
        ColdStartJobSlurmCallbackRequestSerializer,
        get_cold_start_run,
        run_cold_start_job_callback_pw
    )


@extend_schema(
    request=ForecastJobSlurmCallbackRequestSerializer,
    responses={
        202: None,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Callback for Slurm to call when a forecast job ends"
)
@api_view(['POST'])
@handle_exceptions
@auth_scope_required(TOKEN_SLURM_SCOPE)
def forecast_job_slurm_callback(request: Request) -> Response:
    """
    Handles a callback from Slurm to update the status of a forecast job.

    :param request: HTTP request containing Slurm job details and status.
    :return: HTTP 202 response indicating the callback was processed.
    """
    return handle_slurm_callback(
        request,
        ForecastJobSlurmCallbackRequestSerializer,
        get_forecast_run,
        run_forecast_job_callback_pw
    )


@extend_schema(
    request=VerificationJobSlurmCallbackRequestSerializer,
    responses={
        202: None,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Callback for Slurm to call when a verification job ends"
)
@api_view(['POST'])
@handle_exceptions
@auth_scope_required(TOKEN_SLURM_SCOPE)
def verification_job_slurm_callback(request: Request) -> Response:
    """
    Handles a callback from Slurm to update the status of a verification job.

    :param request: HTTP request containing Slurm job details and status.
    :return: HTTP 202 response indicating the callback was processed.
    """
    return handle_slurm_callback(
        request,
        VerificationJobSlurmCallbackRequestSerializer,
        get_verification_run,
        run_verification_job_callback_pw
    )


def handle_slurm_callback(request: Request, serializer_class, get_run_fn, job_end_callback_fn) -> Response:
    """
    Common handler for Slurm callback endpoints for any run type that inherits from BaseRun.

    :param request: The incoming HTTP request.
    :param serializer_class: The serializer used for validating the incoming data.
    :param get_run_fn: A function that returns the correct run object given its ID.
    :param job_end_callback_fn: A function that handles the job completion logic.
    :return: HTTP 202 Response or error Response.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(serializer_class, data)
    if error_return:
        return error_return

    run_id = validator.get(next(k for k in validator.keys() if k.endswith("_id")))
    job_status = validator.get("job_status")
    slurm_status = SlurmCallbackStatusEnum(job_status)

    # If Slurm is reporting that the job is now starting, we expect to be in Submitted status
    # For any other status changes, we should be Running or Submitted.  We allow Submitted just in case
    #  1) The job doesn't properly transition to Running
    #  2) To allow a submitted job to be canceled
    expected_status = [StatusEnum.SUBMITTED] if slurm_status == SlurmCallbackStatusEnum.STARTING else [StatusEnum.RUNNING, StatusEnum.SUBMITTED]

    run, error_return = get_run_fn(run_id, None, run_status=expected_status)
    if error_return:
        return error_return

    job_description = f"{get_job_description(run)} (slurm_job_id: {run.slurm_job_id})"
    if slurm_status == SlurmCallbackStatusEnum.STARTING:
        logger.info(f'{job_description} is starting')
        run.status = StatusEnum.RUNNING.db_instance
        run.run_start = datetime.now(timezone.utc)
        run.save(update_fields=["status", "run_start"])
    else:
        # Job has ended
        logger.info(f'{job_description} is ending')
        job_end_callback_fn(run, slurm_status)

    logger.debug(f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)}')
    return Response(status=status.HTTP_202_ACCEPTED)


@extend_schema(
    request=EmptySerializer,
    responses={
        200: OpenApiResponse(
            response=OpenApiTypes.OBJECT,  # Indicates the response is an object
            description="Success",
            examples=[
                OpenApiExample(
                    'Example response',
                    value={'access': 'your_access_token_here'}
                )
            ],  # Defines the example using OpenApiExample
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
    description="Return a token for use by Slurm"
)
@api_view(['GET'])
@handle_exceptions
def get_slurm_token(request: Request) -> Response:
    """
    Generates and returns a token for use by Slurm.

    :param request: HTTP request.
    :return: JSON response containing the generated token.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(EmptySerializer, data)
    if error_return:
        return error_return

    return Response({'access': generate_custom_token(request.user, TOKEN_SLURM_SCOPE)})


def check_slurm_reconciliation(run: BaseRun) -> tuple[bool, str | None]:
    """
    Determine whether a run requires Slurm reconciliation.

    A run is eligible for reconciliation only if:
    - it has a slurm_job_id, and
    - its DB status is active (RUNNING or SUBMITTED).

    Reconciliation is needed when the DB says the job is active but Slurm no longer
    reports it as active (squeue empty / job not present).

    Race-condition exception:
    - If Slurm reports the job as not active but sacct_status is "COMPLETED",
      reconciliation is skipped. This indicates the job finished and the Slurm
      callback is expected imminently, so the server status should be left unchanged.

    Fallback behavior note:
    - This relies on the Slurm callback to eventually arrive and update the run.
      If callbacks are not reliably delivered in some environments, this logic
      will need to be extended with a retry or timeout-based reconciliation path
      (e.g., reconcile if the job remains COMPLETED in Slurm for longer than a
      configured grace period).

    This function performs no database writes and is safe to call inside a readonly transaction.

    :param run: The run object (CalibrationRun / ValidationRun / ForecastRun / VerificationRun),
        which must inherit from BaseRun.
    :return: Tuple (needs_reconciliation, sacct_status)
        - needs_reconciliation: True if the DB indicates an active job but Slurm indicates the job is not active
          (excluding the COMPLETED race-condition exception).
        - sacct_status: The terminal status reported by Slurm accounting (sacct), or None/UNKNOWN if indeterminate.
    """
    if not run.slurm_job_id:
        return False, None

    if run.status not in {
        StatusEnum.SUBMITTED.db_instance,
        StatusEnum.RUNNING.db_instance,
    }:
        return False, None

    slurm_is_active, sacct_status = get_slurm_status(run.slurm_job_id)

    logger.debug(
        f"{get_job_description(run)}: "
        f"Slurm active={slurm_is_active}, sacct_status={sacct_status}"
    )

    # Race-condition exception:
    if not slurm_is_active and sacct_status == "COMPLETED":
        logger.info(
            f"{get_job_description(run)}: slurm inactive but sacct_status=COMPLETED; "
            f"skipping reconciliation (awaiting callback)"
        )
        return False, sacct_status

    if not slurm_is_active:
        return True, sacct_status

    return False, None


def apply_slurm_reconciliation(run: BaseRun, sacct_status: str) -> None:
    """
    Escalate a run to SERVER_ERROR due to a Slurm/DB inconsistency.

    This is used when the database indicates the run is active (RUNNING/SUBMITTED),
    but Slurm indicates the job is no longer active. The run is moved to SERVER_ERROR
    and a structured reconciliation entry is appended to failure_messages.

    :param run: The run object to mutate (must inherit from BaseRun). This object is expected
        to be re-fetched under a write-capable transaction (e.g., select_for_update()) by the caller.
    :param sacct_status: The terminal status reported by Slurm accounting (sacct) for the job.
    :return: None
    """
    original_status = run.status.name

    message = (
        f"Slurm job {run.slurm_job_id} not active while DB status was "
        f"{original_status}; sacct_status={sacct_status}"
    )

    logger.error(f"{get_job_description(run)}: {message}")

    # failure_messages is a TEXT field → treat as JSON string
    existing = normalize_failure_messages(run.failure_messages)

    existing.append({
        "source": "slurm",
        "type": "reconciliation",
        "sacct_status": sacct_status,
        "message": message,
    })

    run.status = StatusEnum.SERVER_ERROR.db_instance
    run.failure_messages = json.dumps(existing)

    run.save(update_fields=["status", "failure_messages"])


def get_slurm_status(slurm_id: int) -> tuple[bool, str | None]:
    """
    Query the Slurm status service for the current status of a job.

    Semantics:
    - If squeue indicates the job is present/active, that is authoritative and the job is treated as active.
    - If squeue is empty but sacct provides a terminal status, the job is treated as not active and the
      terminal status is returned.
    - If the response is non-200, not JSON, or missing usable fields, the job is treated as not active
      with status "UNKNOWN" (conservative for reconciliation logic).

    :param slurm_id: Slurm job ID to query.
    :return: Tuple (is_active, sacct_status)
        - is_active: True if the job is currently active (squeue authoritative), False otherwise.
        - sacct_status: The terminal status from sacct when available, or "UNKNOWN"/None if indeterminate.
    """
    # ----------------------------------
    # TODO Get rid of this debug code
    FORCE_SLURM_INACTIVE = False
    if FORCE_SLURM_INACTIVE:
        logger.warning(
            f"FORCE_SLURM_INACTIVE enabled — treating Slurm job {slurm_id} as inactive"
        )
        return False, "FORCED_ERROR"
    # ------------------------------------

    base_url = f"{settings.SLURM_URL.rstrip('/')}/{settings.SLURM_JOB_STATUS_ENDPOINT.lstrip('/')}"

    # Query Slurm for the live job status
    url = f"{base_url}?slurm_job_id={slurm_id}"

    try:
        resp = requests.get(url, timeout=10)

        # Non-200 HTTP responses (including 404) are treated as unknown
        if resp.status_code != 200:
            logger.error(
                f"Non-200 response from Slurm for job {slurm_id}: "
                f"{resp.status_code}\n{resp.text}"
            )
            return False, "UNKNOWN"

        # Try to parse JSON response
        try:
            data = resp.json()
        except ValueError:
            # Log the entire response text when not JSON
            logger.error(
                f"Invalid JSON response from Slurm for job {slurm_id}:\n{resp.text}"
            )
            return False, "UNKNOWN"

        squeue_status = data.get("squeue")
        sacct_status = data.get("sacct")

        if squeue_status:
            # Job still active → squeue authoritative
            return True, squeue_status

        if sacct_status:
            # Job finished → sacct authoritative
            return False, sacct_status

        # Defensive fallback: no usable status provided
        return False, "UNKNOWN"

    except Exception as ex:
        logger.exception(f"Error querying Slurm status for job {slurm_id}: {ex}")
        # Safest assumption: job is gone, status indeterminate
        return False, "UNKNOWN"
