import json
import logging
import os
import shutil

from django.db import transaction
from drf_spectacular.utils import OpenApiParameter, extend_schema, OpenApiResponse
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import ForecastConfigEnum, StatusEnum
from calibration.models import ColdStartRun
from calibration.run_util.run_common import submit_job
from calibration.util.calibration_validators import ErrorResponseSerializer, LoadForecastTabResponseSerializer, \
    ForecastRunIdSerializer, CreateAndRunForecastResponseSerializer, DeleteForecastRunResponseSerializer, ForecastRunDataResponseSerializer, \
    LoadForecastTabRequestSerializer, HindcastRunIdSerializer, CreateAndRunHindcastResponseSerializer, \
    DeleteHindcastRunResponseSerializer, GetColdStartJobsForConfigurationResponseSerializer, \
    HindcastConfigurationSerializer, GetHindcastTimeseriesRequestSerializer, GetHindcastIterationsResponseSerializer
from calibration.util.ngen_locations import get_forecast_dir, get_forecast_output_file, get_cold_start_output_file, \
    get_hindcast_dir, get_hindcast_output_file
from calibration.views.calibration_secondary_data_views import read_csv_as_json
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, validate_response, validate_request, get_forecast_run, create_forecast_run_internal, \
    ResponseError, get_user_email, get_elapsed_str, readonly_transaction, get_calibration_run, truncate_large_fields, \
    create_hindcast_run_internal, get_hindcast_run

logger = logging.getLogger(__name__)


@extend_schema(
    request=LoadForecastTabRequestSerializer,
    responses={
        200: LoadForecastTabResponseSerializer,
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
    description="Load forecast tab data"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def load_forecast_tab(request: Request) -> Response:
    """
    Load data for the forecast tab, including forecast cycles with associated
    data sources and time ranges.

    If hindcast_only is True, only return configurations that are valid
    for hindcast. Otherwise, return all forecast configurations for the domain.

    Runs inside a read-only transaction since no writes are performed.

    :param request: HTTP request containing calibration_run_id
    :return: JSON response with forecast cycle values.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(LoadForecastTabRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    hindcast_only = validator.get('hindcast_only', False)

    calibration_run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.DONE])
    if error_return:
        return error_return
    assert calibration_run is not None

    extra_filter = {
        'domain': calibration_run.gage.domain,
    }

    # Hindcast can only use configurations explicitly marked as supported.
    # Forecast can use all active configurations for the domain.
    if hindcast_only:
        extra_filter['supports_hindcast'] = True

    with readonly_transaction():
        configuration_values = ForecastConfigEnum.get_choices_with_fields(
            fields=[
                'name',
                'data_sources',
                'cycle_start',
                'cycle_end',
                'cycle_freq',
                'fcst_win',
                'availability_lag',
                'order'  # included ONLY so we can sort
            ],
            extra_filter=extra_filter,
        )

    # Sort with NULLs at bottom
    configuration_values.sort(
        key=lambda x: (x['order'] is None, x['order'])
    )

    # Remove 'order' before returning to client
    for item in configuration_values:
        item.pop('order', None)

    response = {'forecast_configuration_values': configuration_values}

    response_validator, error_response = validate_response(
        LoadForecastTabResponseSerializer,
        response,
        fields_to_truncate=['forecast_configuration_values'],
        max_length=5
    )
    if error_response:
        return error_response

    logger.debug(
        f'{get_caller_name()}() request from {get_user_email(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["forecast_configuration_values"], max_length=5))}'
    )

    return Response(response_validator.data)


@extend_schema(
    request=ForecastRunIdSerializer,
    responses={
        200: CreateAndRunForecastResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Clone and submit a forecast job"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def clone_and_run_forecast_job(request: Request) -> Response:
    """
    Clone an existing forecast job, creating a new forecast run with identical parameters.

    :param request: The HTTP request object.
    :return: A Response object with the cloned forecast run data.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(ForecastRunIdSerializer, data)
    if error_return:
        return error_return

    forecast_run_id = validator.get('forecast_run_id')

    run, error_return = get_forecast_run(forecast_run_id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return
    assert run is not None

    new_forecast_run = create_forecast_run_internal(
        run.calibration_run,
        run.cold_start_run,
        run.configuration,
        run.cycle_date
    )
    submit_job(new_forecast_run)

    response = {
        'message': f'Forecast Job {new_forecast_run.id} cloned from Job {run.id} and submitted for Calibration Job {new_forecast_run.calibration_run.id}',
        'calibration_run_id': new_forecast_run.calibration_run.id,
        'forecast_run_id': new_forecast_run.id,
        'submit_date': new_forecast_run.submit_date
    }

    response_validator, error_response = validate_response(CreateAndRunForecastResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=HindcastRunIdSerializer,
    responses={
        200: CreateAndRunHindcastResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Clone and submit a hindcast job"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def clone_and_run_hindcast_job(request: Request) -> Response:
    """
    Clone an existing hindcast job, creating a new hindcast run with identical parameters.

    :param request: The HTTP request object.
    :return: A Response object with the cloned hindcast run data.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(HindcastRunIdSerializer, data)
    if error_return:
        return error_return

    hindcast_run_id = validator.get('hindcast_run_id')

    run, error_return = get_hindcast_run(hindcast_run_id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return
    assert run is not None

    new_hindcast_run = create_hindcast_run_internal(
        run.calibration_run,
        run.cold_start_run,
        run.configuration,
        run.cycle_date,
        run.interval_cycle,
        run.num_iterations
    )
    submit_job(new_hindcast_run)

    response = {
        'message': f'Hindcast Job {new_hindcast_run.id} cloned from Job {run.id} and submitted for Calibration Job {new_hindcast_run.calibration_run.id}',
        'calibration_run_id': new_hindcast_run.calibration_run.id,
        'hindcast_run_id': new_hindcast_run.id,
        'submit_date': new_hindcast_run.submit_date
    }

    response_validator, error_response = validate_response(CreateAndRunHindcastResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=ForecastRunIdSerializer,
    responses={
        200: ForecastRunDataResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Return the forecast timeseries data"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_forecast_timeseries_data(request: Request) -> Response:
    """
    Load results for a forecast job (and related cold start job).

    :param request: HTTP request containing forecast_run_id
    :return: JSON response with forecast timeseries values.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(ForecastRunIdSerializer, data)
    if error_return:
        return error_return

    forecast_run_id = validator.get('forecast_run_id')

    run, error_return = get_forecast_run(forecast_run_id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return
    assert run is not None

    # Read the output data from forecast (and possibly cold start)
    forecast_output = get_forecast_output_file_path(run)
    if not os.path.exists(forecast_output):
        raise FileNotFoundError(f"File not found: {forecast_output}")

    # Explicitly enforce the exact keys we want instead of inheriting CSV header
    forecast_data = read_csv_as_json(forecast_output, keys=["Time", "sim_flow"])

    cold_start_output = get_cold_start_output_file(run)
    if cold_start_output and os.path.exists(cold_start_output):
        cold_start_data = read_csv_as_json(cold_start_output, keys=["Time", "cold_start_flow"])

        data = []
        # Append all cold start rows first (cold_start_flow populated, sim_flow None),
        # then append all forecast rows (sim_flow populated, cold_start_flow None).
        for row in cold_start_data:
            data.append({
                "time": row["Time"],
                "cold_start_flow": row["cold_start_flow"],
                "sim_flow": None
            })
        for row in forecast_data:
            data.append({
                "time": row["Time"],
                "cold_start_flow": None,
                "sim_flow": row["sim_flow"]
            })

    else:
        # No cold start — forecast only
        data = [
            {"time": row["Time"], "cold_start_flow": None, "sim_flow": row["sim_flow"]}
            for row in forecast_data
        ]

    response = {
        'forecast_run_id': forecast_run_id,
        'timeseries_data': data,
    }
    # Validate and return response
    response_validator, error_response = validate_response(
        ForecastRunDataResponseSerializer, response,
        fields_to_truncate=['timeseries_data'], max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["timeseries_data"], max_length=10))}'
    )

    return Response(response_validator.data)


@extend_schema(
    request=HindcastRunIdSerializer,
    responses={
        200: ForecastRunDataResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Return the hindcast timeseries data for all iterations"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_hindcast_timeseries_data(request: Request) -> Response:
    """
    Load results for a hindcast job for all iterations.

    Rows with the same time are grouped together. Each iteration value is stored
    under its own key such as hindcast_0, hindcast_1, hindcast_2, etc.

    :param request: HTTP request containing hindcast_run_id
    :return: JSON response with hindcast timeseries values for all iterations.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(HindcastRunIdSerializer, data)
    if error_return:
        return error_return

    hindcast_run_id = validator.get('hindcast_run_id')

    run, error_return = get_hindcast_run(hindcast_run_id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return
    assert run is not None

    iterations = _get_hindcast_iterations(run.interval_cycle, run.num_iterations)

    timeseries_by_time: dict[str, dict[str, object]] = {}

    for iteration in iterations:
        hindcast_output = get_hindcast_output_file(run, iteration)
        if not os.path.exists(hindcast_output):
            raise FileNotFoundError(f"File not found: {hindcast_output}")

        # Explicitly enforce the exact keys we want instead of inheriting CSV header
        hindcast_data = read_csv_as_json(hindcast_output, keys=["Time", "sim_flow"])

        iteration_key = f"hindcast_{iteration}"

        for row in hindcast_data:
            time_value = row["Time"]

            if time_value not in timeseries_by_time:
                timeseries_by_time[time_value] = {"time": time_value}

            timeseries_by_time[time_value][iteration_key] = row["sim_flow"]

    timeseries_data = list(timeseries_by_time.values())

    response = {
        'hindcast_run_id': hindcast_run_id,
        'timeseries_data': timeseries_data,
    }

    response_validator, error_response = validate_response(
        ForecastRunDataResponseSerializer,
        response,
        fields_to_truncate=['timeseries_data'],
        max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["timeseries_data"], max_length=10))}'
    )

    return Response(response_validator.data)


def _get_hindcast_iterations(interval_cycle: int, num_iterations: int) -> list[int]:
    """
    Generate the list of hindcast iteration values.

    Each iteration advances by `interval_cycle`, starting at 0.

    Example:
        interval_cycle=3, num_iterations=4 -> [0, 3, 6, 9]

    :param interval_cycle: Step size between hindcast iterations.
    :param num_iterations: Number of iterations to generate.
    :return: List of hindcast iteration values.
    """
    return [i * interval_cycle for i in range(num_iterations)]


@extend_schema(
    request=ForecastRunIdSerializer,
    responses={
        200: DeleteForecastRunResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Delete a forecast job"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def delete_forecast_job(request: Request) -> Response:
    """
    Delete a forecast job along with the associated cold start job. 
    In the future, we shouldn't delete the cold start job with the forecast.  It should be treated independently

    :param request: The HTTP request object.
    :return: A Response object with the deletion confirmation.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(ForecastRunIdSerializer, data)
    if error_return:
        return error_return

    forecast_run_id = validator.get('forecast_run_id')

    run, error_return = get_forecast_run(forecast_run_id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return
    assert run is not None

    if run.status in [StatusEnum.RUNNING.db_instance, StatusEnum.SUBMITTED.db_instance]:
        return ResponseError(f'Forecast Job {run.id} is running.  Cannot delete a running job')

    run_id = run.id
    forecast_dir = get_forecast_dir(run)  # Save before delete

    with transaction.atomic():
        cold_start_run = run.cold_start_run

        # Delete the Forecast Run
        run.delete()

        # Delete the Cold Start Run if linked
        if cold_start_run:
            cold_start_run.delete()

        logger.info(f"Deleting directory {forecast_dir}")
        shutil.rmtree(forecast_dir, ignore_errors=True)

        shutil.rmtree(get_forecast_dir(run), ignore_errors=True)

    message = f"Forecast Job {run_id} has been deleted"
    if cold_start_run:
        message += f" along with Cold Start Run {cold_start_run.id}"

    response = {'message': message, 'forecast_run_id': run_id}

    response_validator, error_response = validate_response(DeleteForecastRunResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=HindcastRunIdSerializer,
    responses={
        200: DeleteHindcastRunResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Delete a hindcast job"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def delete_hindcast_job(request: Request) -> Response:
    """
    Delete a hindcast job

    :param request: The HTTP request object.
    :return: A Response object with the deletion confirmation.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(HindcastRunIdSerializer, data)
    if error_return:
        return error_return

    hindcast_run_id = validator.get('hindcast_run_id')

    run, error_return = get_hindcast_run(hindcast_run_id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return
    assert run is not None

    if run.status in [StatusEnum.RUNNING.db_instance, StatusEnum.SUBMITTED.db_instance]:
        return ResponseError(f'Hindcast Job {run.id} is running.  Cannot delete a running job')

    run_id = run.id
    hindcast_dir = get_hindcast_dir(run)  # Save before delete

    run.delete()

    logger.info(f"Deleting directory {hindcast_dir}")
    shutil.rmtree(hindcast_dir, ignore_errors=True)

    shutil.rmtree(get_hindcast_dir(run), ignore_errors=True)

    message = f"Hindcast Job {run_id} has been deleted"

    response = {'message': message, 'hindcast_run_id': run_id}

    response_validator, error_response = validate_response(DeleteHindcastRunResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


@extend_schema(
    request=HindcastConfigurationSerializer,
    responses={
        200: GetColdStartJobsForConfigurationResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Return a list of cold start jobs that are valid for a configuration"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_cold_start_jobs_for_configuration(request: Request) -> Response:
    """
    Get a list of cold start jobs that are valid to use with a given configuration

    :param request: The HTTP request object.
    :return: A Response object with a list of jobs.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(HindcastConfigurationSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    configuration_name = validator.get('configuration_name')

    calibration_run, error_return = get_calibration_run(
        calibration_run_id, request.user, run_status=[StatusEnum.DONE]
    )
    if error_return:
        return error_return
    assert calibration_run is not None

    configuration = ForecastConfigEnum.get_instance(configuration_name)

    if not configuration.supports_hindcast:
        return ResponseError(f"Configuration '{configuration_name}' does not support hindcast")

    cycle_freq = configuration.cycle_freq

    # Get all cold starts which belong to this user and are DONE.
    candidate_runs = (
        ColdStartRun.objects
        .select_related('status', 'calibration_run', 'calibration_run__owner')
        .filter(
            calibration_run=calibration_run,
            status=StatusEnum.DONE.db_instance
        )
        .order_by('-cold_start_date')
    )

    cold_start_jobs = []
    for run in candidate_runs:
        cold_start_date = run.cold_start_date

        # The cold_start_date should always be on an exact hour.
        if (
                cold_start_date.minute != 0
                or cold_start_date.second != 0
                or cold_start_date.microsecond != 0
        ):
            continue

        # Keep only hours that align with the configuration cycle frequency.
        if cold_start_date.hour % cycle_freq != 0:
            continue

        cold_start_jobs.append({
            'cold_start_run_id': run.id,
            'cold_start_status': run.status.name,  # if run.status else None,
            'cold_start_date': run.cold_start_date,
            'cold_start_cycle_date': run.cycle_date,
            'cold_start_submit_date': run.submit_date,
        })

    response = {'cold_start_jobs': cold_start_jobs}

    response_validator, error_response = validate_response(
        GetColdStartJobsForConfigurationResponseSerializer,
        response
    )
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(response_validator.data)}'
    )

    return Response(response_validator.data)
