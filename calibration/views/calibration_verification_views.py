import json
import logging
import os
import shutil

import yaml
from django.core.cache import cache
from django.db import transaction
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import StatusEnum
from calibration.enums_vanilla import JobType
from calibration.run_util.run_common import submit_job
from calibration.util.calibration_validators import ErrorResponseSerializer, \
    CreateAndRunVerificationRequestSerializer, CreateAndRunVerificationResponseSerializer, \
    GetVerificationPlotNamesResponseSerializer, GetVerificationPlotRequestSerializer, \
    GetVerificationPlotResponseSerializer, DeleteVerificationJobResponseSerializer, VerificationRunIdSerializer
from calibration.util.ngen_locations import get_verification_run_dir, get_verification_yaml_config_file
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, validate_response, validate_request, \
    get_forecast_run, get_verification_run, ResponseError, get_user_email, get_elapsed_str, \
    create_verification_run_internal, png_to_base64_url, truncate_large_fields, get_job_description

logger = logging.getLogger(__name__)


@extend_schema(
    request=CreateAndRunVerificationRequestSerializer,
    responses={
        201: CreateAndRunVerificationResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Create a new verification"
)
@api_view(['POST'])
@handle_exceptions
def create_and_run_verification_job(request: Request) -> Response:
    """
    Creates a new verification job for the requesting user, and submits it for processing.

    Handles the creation process by accepting verification details in the request, validating them,
    and creating a new verification job if the request is valid.

    :param request: The HTTP request object, containing user and verification job details.
    :return: A Response object with the serialized verification job data.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} ')

    validator, error_return = validate_request(CreateAndRunVerificationRequestSerializer, data)
    if error_return:
        return error_return

    forecast_run_id = validator.get('forecast_run_id')
    logging_config = validator.get('logging_config')

    forecast_run, error_return = get_forecast_run(forecast_run_id, request.user, run_status=[StatusEnum.DONE])
    if error_return:
        return error_return

    verification_run = create_verification_run_internal(forecast_run)

    error_response = submit_job(verification_run, logging_config=logging_config)
    if error_response:
        return error_response

    msg = get_job_description(verification_run) + ' created and submitted'
    response = {
        'message': msg,
        'calibration_run_id': forecast_run.calibration_run.id,
        'forecast_run_id': forecast_run.id,
        'verification_run_id': verification_run.id,
        'submit_date': verification_run.submit_date,
        'status': verification_run.status.name
    }

    response_validator, error_response = validate_response(CreateAndRunVerificationResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data, status=status.HTTP_201_CREATED)


@extend_schema(
    request=VerificationRunIdSerializer,
    responses={
        200: GetVerificationPlotNamesResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get a list of plot names"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_verification_plot_names(request: Request) -> Response:
    """
    Retrieves the list of plot images for a verification job, filtered by applicable optimizations.

    :param request: The request containing either POST data or query parameters.
    :return: A JSON response with the run ID, list of plot images, and run status.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(VerificationRunIdSerializer, data)
    if error_return:
        return error_return

    verification_run_id = validator.get('verification_run_id')

    run, error_return = get_verification_run(verification_run_id, request.user,
                                             run_status=[StatusEnum.RUNNING, StatusEnum.DONE, StatusEnum.CANCELLED, StatusEnum.FAILED,
                                                         StatusEnum.SERVER_ERROR])
    if error_return:
        return error_return

    plot_names = []

    # For now, get verification plots directly from the file system
    try:
        with open(get_verification_yaml_config_file(run), 'r') as file:
            yaml_config_data = yaml.safe_load(file)
            if 'general' in yaml_config_data and 'nwm_configuration' in yaml_config_data['general']:
                verification_plot_location = os.path.join(get_verification_run_dir(run), 'plots', yaml_config_data['general']['nwm_configuration'])
                for root, dirs, files in os.walk(verification_plot_location):
                    if files:
                        for file_name in files:
                            plot_names.append({
                                'name': os.path.relpath(os.path.join(root, file_name), get_verification_run_dir(run)),
                                'display_name': file_name,
                                'description': f'Placeholder description of {file_name}',
                                'timeseries_available': False
                            })
    except Exception as e:
        logger.warning(f"Unable to get plots for {get_job_description(run)} due to error: {e}")

    response = {
        "verification_run_id": run.id,
        'plot_names': plot_names,
        'status': run.status.name
    }

    response_validator, error_response = validate_response(
        GetVerificationPlotNamesResponseSerializer,
        response,
        fields_to_truncate=['plot_names'], max_length=3

    )
    if error_response:
        return error_response
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {json.dumps(response_validator.data)}')
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["plot_names"], max_length=3))}'
    )

    return Response(response_validator.data)


@extend_schema(
    request=GetVerificationPlotRequestSerializer,
    responses={
        200: GetVerificationPlotResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Return a base64 URL for a verification plot image and the associated data"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_verification_plot(request: Request) -> Response:
    """
    Retrieves a specific plot for a verification run, returning the plot file location.

    :param request: The request containing plot name.
    :return: A JSON response with plot details, or an error if the plot is not found.
    :raises ResponseError: If the plot cannot be found or an error occurs.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(GetVerificationPlotRequestSerializer, data)
    if error_return:
        return error_return

    verification_run_id = validator.get('verification_run_id')

    plot_name = validator.get('plot_name')

    # Replace spaces with underscores in plot_name to avoid CacheKeyWarning
    sanitized_plot_name = plot_name.replace(" ", "_").replace("/", "_")
    # Base cache key common part
    cache_key_base = f"{sanitized_plot_name}_{verification_run_id}"
    cache_key_plot_url = f"plot_url_{cache_key_base}"

    run, error_return = get_verification_run(verification_run_id, request.user, run_status=[StatusEnum.RUNNING, StatusEnum.DONE])
    if error_return:
        return error_return

    # Just retrieve the file for now
    plot_file_path = os.path.join(get_verification_run_dir(run), plot_name)
    logger.info(f'Plot file path: {plot_file_path}')
    if os.path.exists(plot_file_path):
        plot_url = png_to_base64_url(plot_file_path)
        logger.info(f'Retrieving plot from {plot_file_path}')

        # Cache the plot_url
        cache.set(cache_key_plot_url, plot_url, timeout=3600)
    else:
        return ResponseError(
            f"Error while checking existence of plot '{plot_name}' for {JobType.VERIFICATION.value.capitalize()} {run.id}: File Not Found")

    response = {
        'plot_name': plot_name,
        'plot_url': plot_url,
        'plot_file_path': plot_file_path,
        'verification_run_id': verification_run_id
    }

    # Validate and return response
    response_validator, error_response = validate_response(
        GetVerificationPlotResponseSerializer, response,
        fields_to_truncate=['plot_url', 'plot_data'], max_length=10
    )
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["plot_url"], max_length=10))}'
    )

    return Response(response_validator.data)


@extend_schema(
    request=VerificationRunIdSerializer,
    responses={
        200: DeleteVerificationJobResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Delete a verification job"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def delete_verification_job(request: Request) -> Response:
    """
    Delete a verification job.

    :param request: The HTTP request object.
    :return: A Response object with the deletion confirmation.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(VerificationRunIdSerializer, data)
    if error_return:
        return error_return

    verification_run_id = validator.get('verification_run_id')

    run, error_return = get_verification_run(verification_run_id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return

    if run.status in [StatusEnum.RUNNING.db_instance, StatusEnum.SUBMITTED.db_instance]:
        return ResponseError(f'Verification Job {run.id} is running.  Cannot delete a running job')

    run_id = run.id
    verification_dir = get_verification_run_dir(run)  # Save before delete

    with transaction.atomic():
        # Delete the Verification Run
        run.delete()

        logger.info(f"Deleting directory {verification_dir}")
        shutil.rmtree(verification_dir, ignore_errors=True)

        shutil.rmtree(get_verification_run_dir(run), ignore_errors=True)

    message = f"Verification Job {run_id} has been deleted"

    response = {'message': message, 'verification_run_id': run_id}

    response_validator, error_response = validate_response(DeleteVerificationJobResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)
