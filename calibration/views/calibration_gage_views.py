import json
import logging
import os

from django.db import transaction
from drf_spectacular.utils import OpenApiParameter, extend_schema, OpenApiResponse
from pyogrio.errors import DataLayerError
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import ObservationalSourceEnum, ForcingSourceEnum, DomainEnum, GeopackageSourceEnum
from calibration.models import Gage, CalibrationRun, CalibrationFormulation
from calibration.util.caching import get_cached_gages, get_gage_by_id, update_and_get_cached_gage_status
from calibration.util.calibration_validators import SaveGageRequestSerializer, GageIdSerializer, SaveGageResponseSerializer, GageSerializer, \
    LoadGageResponseSerializer, ErrorResponseSerializer, UpdateGageStatusRequestSerializer, UpdateGageStatusResponseSerializer, EmptySerializer
from calibration.util.geopkg import gpkg_to_png_selected_layers, get_geometry_from_gpkg
from calibration.util.ngen_locations import get_forcing_dir_for_job, get_geopackage_file_path
from calibration.views import ngen_cal_input
from calibration.views.called_from import get_caller_name
from calibration.views.common import get_calibration_run, ResponseError, handle_exceptions, validate_response, validate_request, \
    png_str_to_base64_url, truncate_large_fields, get_valid_path, get_user_email, get_elapsed_str, CerfException
from calibration.views.data_services import get_geopackage_from_data_services, \
    get_forcing_data_from_s3, DataServicesException, get_module_metadata_from_data_services, clear_times, should_use_bmi_forcing, \
    update_parameters

logger = logging.getLogger(__name__)


@extend_schema(
    request=EmptySerializer,
    responses={
        200: LoadGageResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        404: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Gage not found"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    parameters=[
        OpenApiParameter(name='calibration_run_id', description='ID of the calibration run', required=True, type=int)
    ],
    description="Load gage tab data"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def load_gage_tab(request: Request) -> Response:
    """
    Load gage tab data based on the calibration run.

    :param request: The HTTP request containing either POST data or query parameters.
    :return: A JSON response with gage data, available source options, and calibration run status.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()

    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(EmptySerializer, data)
    if error_return:
        return error_return

    # Retrieve active source and domain options
    forcing_source_values = ForcingSourceEnum.get_choices_with_fields(fields=['name', 'description'])
    observational_source_values = ObservationalSourceEnum.get_choices_with_fields(fields=['name', 'description'])
    geopackage_source_values = GeopackageSourceEnum.get_choices_with_fields(fields=['name', 'description'])
    domain_values = DomainEnum.get_choices_with_fields(fields=['name', 'display_name', 'description'])

    # Retrieve cached active gages with necessary fields
    gages = [
        {
            'gage_id': gage.get('gage_id'),
            'headwater_calibration': gage.get('headwater_calibration'),
            'nws_id': gage.get('nws_id'),
            'domain': gage.get('domain')
        }
        for gage in get_cached_gages().values()
        if gage.get('is_active')
    ]

    response = {
        'domain_values': domain_values,
        'forcing_source_values': forcing_source_values,
        'observational_source_values': observational_source_values,
        'geopackage_source_values': geopackage_source_values,
        'gages': gages
    }

    # Strip empty values from the payload
    response = {key: value for key, value in response.items() if value not in [None, '', [], {}]}

    response_validator, error_response = validate_response(
        LoadGageResponseSerializer,
        response,
        fields_to_truncate=["gages"],
        max_length=50
    )
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["gages"], max_length=50))}'
    )

    return Response(response_validator.data)


@extend_schema(
    request=GageIdSerializer,
    responses={
        200: GageSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get details for a specific gage"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_gage(request: Request) -> Response:
    """
    Retrieve details for a specific gage.

    :param request: The HTTP request containing either POST data or query parameters.
    :return: A JSON response with the details of the requested gage.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()

    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(GageIdSerializer, data)
    if error_return:
        return error_return

    gage_id = validator.get('gage_id')
    gage_dict = get_gage_by_id(gage_id)

    if not gage_dict:
        return ResponseError(f"Gage '{gage_id}' does not exist or is not active", http_status=status.HTTP_404_NOT_FOUND)

    if not gage_dict['station_name']:
        gage_dict['station_name'] = "<unknown>"

    response_validator, error_response = validate_response(GageSerializer, gage_dict)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)


@extend_schema(
    request=SaveGageRequestSerializer,
    responses={
        200: SaveGageResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Save gage tab data"
)
@api_view(['POST'])
@handle_exceptions
def save_gage_tab(request: Request):
    """
    Save gage tab data and update the calibration run with new gage information.

    This function handles updating forcing, observational, and geopackage data sources and updating the calibration run status.

    :param request: The HTTP request containing POST data with gage and data source details.
    :return: A JSON response confirming the update and including any errors from data services.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(SaveGageRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    gage_id = validator.get('gage_id')
    forcing_source_requested_name = validator.get('forcing_source_requested')
    observational_source_name = validator.get('observational_source')
    geopackage_source_name = validator.get('geopackage_source')
    job_name = validator.get('job_name')

    run, error_return = get_calibration_run(calibration_run_id, request.user)
    if error_return:
        return error_return
    assert run is not None

    run.job_name = job_name

    eds_errors: list[dict] = []
    geopackage_image_url = None

    # Check cache first to confirm the gage exists and is active
    gage_dict = get_gage_by_id(gage_id)
    if not gage_dict:
        raise Gage.DoesNotExist(f"Gage '{gage_id}' does not exist or is not active")

    # Fetch the actual DB object to assign to the FK (include domain for Data Services calls)
    gage = (
        Gage.objects
        .select_related("domain")
        .only("id", "gage_id", "domain")
        .get(gage_id=gage_id)
    )

    gage_is_new_or_changed = (run.gage is None) or (run.gage != gage)

    if gage_is_new_or_changed:
        # reset + set gage (non-CLI path)
        reset_gage_dependent_state_on_change(run, gage, cli=False)

        # Refresh module parameters for existing formulations (if any).
        # Data Services call must be outside a write transaction.
        my_formulations = (
            CalibrationFormulation.objects
            .filter(calibration_run_id=run.id)
            .select_related("module")
        )
        module_names = set(my_formulations.values_list("module__name", flat=True))

        if module_names:
            module_metadata, module_eds_errors = get_module_metadata_from_data_services(run, module_names)

            if module_eds_errors:
                eds_errors.extend(module_eds_errors)
            else:
                modules_with_params = [
                    m for m in (module_metadata or {}).get("modules", [])
                    if not m.get("error")
                ]

                if modules_with_params:
                    with transaction.atomic():
                        update_parameters(run, {"modules": modules_with_params}, gage_changed=True)

        # Get Geopackage - for now HYDROFABRIC is the only possibility
        if geopackage_source_name and geopackage_source_name == GeopackageSourceEnum.HYDROFABRIC.value:
            try:
                get_geopackage_from_data_services(run)
            except DataServicesException as e:
                logger.exception("Error retrieving geopackage data from Data Services")
                eds_errors.append({
                    'name': 'geopackage',
                    'message': str(e),
                    'status_code': e.status_code if e.status_code else None
                })
        else:
            raise CerfException("Invalid geopackage source")

        run.geopackage_source = GeopackageSourceEnum.get_instance(geopackage_source_name) if geopackage_source_name else None

        geopackage_path = get_geopackage_file_path(run)
        if geopackage_path:
            catchments = list(get_geometry_from_gpkg(geopackage_path)['catchments'].keys())
            run.num_catchments = len(catchments)
            logger.info(f"Found {run.num_catchments} catchments in {geopackage_path}: {catchments}")

        geopackage_image_url = get_geopackage_image_url(geopackage_path)

        run.observational_source = ObservationalSourceEnum.get_instance(observational_source_name) if observational_source_name else None

        # Get Forcing data
        # Determine requested forcing source
        forcing_source_requested = (
            ForcingSourceEnum.get_instance(forcing_source_requested_name)
            if forcing_source_requested_name
            else None
        )

        # Must be set before get_forcing_data_from_s3() because should_use_bmi_forcing() reads it
        run.forcing_source_requested = forcing_source_requested

        if forcing_source_requested_name:
            try:
                get_forcing_data_from_s3(run, forcing_source_requested_name)
            except DataServicesException as e:
                logger.exception("Error retrieving forcing data from Data Services")
                eds_errors.append({
                    'name': 'forcing',
                    'message': str(e),
                    'status_code': e.status_code if e.status_code else None
                })
        else:
            # No forcing_source_requested → clear any existing forcing state
            run.forcing_eds_dir_path = None
            run.forcing_source_actual = None

    else:
        # Get Forcing data
        # Gage unchanged → refetch only if requested source changed

        forcing_source_requested = (
            ForcingSourceEnum.get_instance(forcing_source_requested_name)
            if forcing_source_requested_name
            else None
        )

        needs_forcing_fetch = (
                forcing_source_requested_name
                and (
                        not run.forcing_source_requested
                        or run.forcing_source_requested.name != forcing_source_requested_name
                )
        )

        # Persist the requested source selection even if we don't refetch
        run.forcing_source_requested = forcing_source_requested

        if needs_forcing_fetch:
            try:
                get_forcing_data_from_s3(run, forcing_source_requested_name)
            except DataServicesException as e:
                logger.exception("Error retrieving forcing data from Data Services")
                eds_errors.append({
                    'name': 'forcing',
                    'message': str(e),
                    'status_code': e.status_code if e.status_code else None
                })
        elif not forcing_source_requested_name:
            # No forcing_source_requested → clear any existing forcing state
            run.forcing_eds_dir_path = None
            run.forcing_source_actual = None
            clear_times(run)

    # -------------------------
    # Write phase
    # -------------------------
    run.save()

    ngen_cal_input.ready_to_run(run)

    response = {'message': f'Calibration Job {run.id} updated',
                'calibration_run_id': run.id,
                'status': run.status.name,
                'geopackage_image_url': geopackage_image_url,
                'num_catchments': run.num_catchments,
                'forcing_source_requested': run.forcing_source_requested.name if run.forcing_source_requested else None,
                'forcing_source_actual': run.forcing_source_actual.name if run.forcing_source_actual else None}
    should_use_bmi = should_use_bmi_forcing(run)
    if not should_use_bmi:
        if run.forcing_source_requested and run.forcing_source_requested != run.forcing_source_actual:
            response['warnings'] = [
                f'{run.forcing_source_requested.name} forcing data not found.  Using {run.forcing_source_actual.name if run.forcing_source_actual else None}'
            ]
    if eds_errors:
        response['eds_errors'] = eds_errors

    response_validator, error_response = validate_response(SaveGageResponseSerializer, response, fields_to_truncate=['geopackage_image_url'])
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["geopackage_image_url"]))}'
    )

    return Response(response_validator.data)


@extend_schema(
    request=UpdateGageStatusRequestSerializer,
    responses={
        200: UpdateGageStatusResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get and optionally set a gage statusa"
)
@api_view(['POST'])
@handle_exceptions
def update_and_get_gage_status(request: Request) -> Response:
    """
    Update (or query) a gage's cached 'is_active' flag, and return its current state.

    Body: { "gage_id": "<str>", "is_active": <bool> }  # 'is_active' optional; omit to query only
    Response: { "message": "<str>", "gage_id": "<str>", "is_active": <bool> }
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(UpdateGageStatusRequestSerializer, data)
    if error_return:
        return error_return

    gage_id = validator.get('gage_id')
    desired_active = validator.get('is_active')  # may be None

    result = update_and_get_cached_gage_status(gage_id, desired_active)
    if result is None:
        return ResponseError(f"Gage '{gage_id}' does not exist", http_status=status.HTTP_404_NOT_FOUND)

    gage_id, is_active = result

    # Optional: differentiate query-only vs update in the message
    action = "now " if desired_active is not None else "currently "
    response = {
        'message': f"Gage {gage_id} is {action}{'active' if is_active else 'inactive'}",
        'gage_id': gage_id,
        'is_active': is_active
    }

    response_validator, error_response = validate_response(UpdateGageStatusResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


def get_geopackage_image_url(geopackage_path: str | None) -> str | None:
    """
    Convert a GeoPackage file to a PNG image URL if available.

    :param geopackage_path: The file path of the GeoPackage.
    :return: A base64-encoded URL string of the PNG image if conversion is successful; otherwise, None.
    """
    if geopackage_path and os.path.exists(geopackage_path):
        try:
            # Attempt to convert the GeoPackage to PNG for selected layers
            geopackage_png = gpkg_to_png_selected_layers(geopackage_path)
            return png_str_to_base64_url(geopackage_png.getvalue())
        except DataLayerError as e:
            # Log the error and return None if the layer could not be opened
            logger.exception(f"DataLayerError - {e} - while processing geopackage: {geopackage_path}")
            return None
        except Exception as e:
            # Handle any other exceptions
            logger.exception(f"An unexpected error occurred: {e} - while processing geopackage: {geopackage_path}")
            return None
    else:
        return None


def reset_gage_dependent_state_on_change(run: CalibrationRun, new_gage: Gage, *, cli: bool = False) -> bool:
    """
    If the gage changes, clear any fields derived from the prior gage and clear derived times.

    This function performs no DB writes; it only mutates `run` in memory.
    Returns True if the gage changed (or was newly set), else False.
    """
    gage_changed = (run.gage is None) or (run.gage_id != new_gage.id)
    if not gage_changed:
        return False

    # Clear file paths associated with the prior gage.
    geopackage = get_geopackage_file_path(run)
    if geopackage and os.path.exists(geopackage):
        os.remove(geopackage)

    run.forcing_eds_dir_path = None

    clear_times(run, cli=cli)
    run.gage = new_gage
    return True


def get_data_files_status(run: CalibrationRun) -> dict:
    """
    Check the status of data files for a calibration run.

    This function verifies whether observational, forcing, and geopackage files are available for the given calibration run.

    :param run: The calibration run instance to check.
    :return: A dictionary with boolean values indicating the presence of observational, forcing, and geopackage files.
    """
    forcing_path = True if should_use_bmi_forcing(run) else get_valid_path(run.forcing_eds_dir_path, lambda: get_forcing_dir_for_job(run))

    geopackage_path = get_geopackage_file_path(run)

    # TODO Talk to Richard about this.  Do we really need Obs status?
    return {'observational': True,
            'forcing': bool(forcing_path),
            'geopackage': bool(geopackage_path)}
