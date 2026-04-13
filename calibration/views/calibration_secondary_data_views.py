import csv
import json
import logging
import os
import time

from data_assimilation_engine.precip.timeseries.timeseries import precip_ts
from data_assimilation_engine.soil_moisture.mapping.mapper import map_soil_moisture_data
from data_assimilation_engine.soil_moisture.timeseries.timeseries import soil_moisture_ts
from data_assimilation_engine.swe.mapping.mapper import map_swe_data
from data_assimilation_engine.swe.timeseries.timeseries import swe_ts
from django.contrib.auth import get_user_model
from django.core.cache import cache
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import StatusEnum, ValidationType
from calibration.enums_vanilla import SecondaryDataEnum
from calibration.models import ValidationRun, Module
from calibration.util.calibration_validators import GetImagesByDateResponseSerializer, \
    ErrorResponseSerializer, ValidationRunIdSerializer, GetTimeseriesDataResponseSerializer, GetSoilMoistureImagesByDateRequestSerializer, \
    GetSWEImagesByDateRequestSerializer
from calibration.util.file_util import get_single_file
from calibration.util.ngen_locations import get_geopackage_dir_for_job, get_swe_netcdf_file, get_validation_output_valid, \
    get_swe_timeseries_png_filepath, get_swe_timeseries_data_filepath, get_soil_moisture_timeseries_png_filepath, \
    get_soil_moisture_timeseries_data_filepath, get_soil_moisture_netcdf_file, get_secondary_plot_dir, get_precipitation_timeseries_data_filepath
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, validate_request, get_validation_run, png_to_base64_url, get_job_description, \
    validate_response, ResponseError, truncate_large_fields, find_validation_worker_with_matching_id, get_user_email, get_elapsed_str, CerfException

logger = logging.getLogger(__name__)

User = get_user_model()


def derive_secondary_data_file_inputs(run: ValidationRun) -> dict[str, str | None] | None:
    """
    Derives the common file inputs from the validation run for SWE and Soil Moisture

    :param run: The ValidationRun object.
    :return: A dict with keys 'ts_csv_location' for the path to the TS CSV file and 'gpkg' for the geopackage file.
    """
    validation_type = ValidationType(run.validation_type)

    # Find the matching worker name for the validation run if applicable.
    worker_name = find_validation_worker_with_matching_id(
        run,
        worker_name=run.iteration.worker_name if validation_type == ValidationType.VALID_ITERATION else None,
        iteration_num=run.iteration.iteration_num if validation_type == ValidationType.VALID_ITERATION else None
    )

    if worker_name:
        # Retrieve paths to required files.
        ts_csv = get_validation_output_valid(run.calibration_run, worker_name)
        gpkg = get_single_file(get_geopackage_dir_for_job(run.calibration_run))
        return {'ts_csv_location': ts_csv, 'gpkg': gpkg}
    else:
        logger.warning(f'Unable to get secondary data locations for {get_job_description(run)}')
        return {}


def get_or_create_secondary_plots(run: ValidationRun, date: str, data_type: SecondaryDataEnum) -> dict[str, str]:
    """
    Generic function to create or retrieve secondary data plots (SWE or Soil Moisture).

    Automatically determines the appropriate output directory via get_secondary_plot_Dir().

    :param run: The ValidationRun object.
    :param date: Date or timestamp string identifying the plot time.
        - For SWE: format "YYYY-MM-DD"
        - For Soil Moisture: format "YYYY-MM-DDThh:mm:ss" (data produced hourly;
          minutes and seconds are ignored when caching).
    :param data_type: The SecondaryDataEnum indicating which dataset to process.
    :return: A dict with keys 'plot_dir', 'sim_map', 'raw_map', and 'lumped_map' file paths.
    """
    # Get inputs for secondary data
    inputs = derive_secondary_data_file_inputs(run)
    if not inputs:
        return {}

    ts_csv_location = inputs['ts_csv_location']
    gpkg = inputs['gpkg']

    # Dispatch table for behavior by data type
    data_config = {
        SecondaryDataEnum.SWE: {
            "netcdf_func": get_swe_netcdf_file,
            "map_func": map_swe_data,
            "prefix": "swe",
        },
        SecondaryDataEnum.SOIL_MOISTURE: {
            "netcdf_func": get_soil_moisture_netcdf_file,
            "map_func": map_soil_moisture_data,
            "prefix": "soil_moisture",
        },
    }

    if data_type not in data_config:
        logger.error(f"Unsupported data_type: {data_type}")
        return {}

    cfg = data_config[data_type]

    plot_dir = get_secondary_plot_dir(run, data_type)
    os.makedirs(plot_dir, exist_ok=True)

    # Required input file
    netcdf_file = cfg["netcdf_func"](run.calibration_run)
    prefix = cfg["prefix"]
    # Build a Memcached-safe cache key that uniquely identifies this run/date combination.
    # The original date may include minutes and seconds (e.g., "2015-12-02 12:12:12"), but:
    #   - Memcached keys cannot contain spaces or colons, so we replace the space with 'T'
    #   - Soil Moisture (and similar datasets) are generated hourly, not per minute,
    #     so we only need hour-level granularity to avoid redundant cache entries.
    # Example result: "2015-12-02T12"
    date_key = date.replace(" ", "T")[:13]  # "YYYY-MM-DDTHH"
    cache_key = f"{prefix}_results_{run.id}_{date_key}"

    cached_result = cache.get(cache_key)
    if cached_result:
        logger.info(f"Returning cached {prefix.replace('_', ' ').title()} results for {get_job_description(run)}")
        return cached_result

    # Build paths for expected outputs
    sim_map_path = os.path.join(plot_dir, f"sim_{prefix}_map_{date}.png")
    raw_map_path = os.path.join(plot_dir, f"raw_{prefix}_map_{date}.png")
    lumped_map_path = os.path.join(plot_dir, f"{prefix}_lumped_map_{date}.png")

    # Run generator if files are missing
    if not (os.path.exists(sim_map_path) and os.path.exists(raw_map_path) and os.path.exists(lumped_map_path)):
        args = [
            date,
            ts_csv_location,
            netcdf_file,
            gpkg,
            sim_map_path,
            raw_map_path,
            lumped_map_path,
            '--direct_s3'
        ]
        logger.info(f"Calling {cfg['map_func'].__name__} with arguments: {args}")
        start_time = time.perf_counter()
        cfg["map_func"](args)
        elapsed_time = time.perf_counter() - start_time
        logger.info(f"Finished running {cfg['map_func'].__name__} in {elapsed_time:.2f} seconds")
    else:
        logger.info(f"{prefix.upper()} files already exist in {plot_dir} for {get_job_description(run)}")

    # Build the result once and cache it.
    result = {
        "plot_dir": plot_dir,
        "sim_map": sim_map_path,
        "raw_map": raw_map_path,
        "lumped_map": lumped_map_path,
    }

    cache.set(cache_key, result)
    return result


def generate_secondary_ts_data(validation_run: ValidationRun, data_type: SecondaryDataEnum) -> None:
    """
    Generates secondary timeseries images (SWE, Soil Moisture) and CSV data (SWE/Soil Moisture/Precipitation)
    if the validation run is not of type VALID_CONTROL.

    :param validation_run: The ValidationRun object.
    :param data_type: The SecondaryDataEnum indicating which dataset to process.
    :return: None
    """
    # Generate timeseries images.
    inputs = derive_secondary_data_file_inputs(validation_run)
    if not inputs:
        logger.warning("No inputs returned from derive_secondary_data_file_inputs; skipping.")
        return

    ts_csv_location = inputs["ts_csv_location"]
    gpkg = inputs["gpkg"]

    # Select appropriate timeseries function and file generators
    if data_type == SecondaryDataEnum.SWE:
        ts_func = swe_ts
        png_file = get_swe_timeseries_png_filepath(validation_run)
        csv_file = get_swe_timeseries_data_filepath(validation_run)

        args = [
            ts_csv_location,
            gpkg,
            "--plot_output", png_file,
            "--csv_output", csv_file,
            "--direct_s3",
        ]

    elif data_type == SecondaryDataEnum.SOIL_MOISTURE:
        ts_func = soil_moisture_ts
        png_file = get_soil_moisture_timeseries_png_filepath(validation_run)
        csv_file = get_soil_moisture_timeseries_data_filepath(validation_run)

        args = [
            ts_csv_location,
            gpkg,
            "--plot_output", png_file,
            "--csv_output", csv_file,
            "--direct_s3",
        ]

    elif data_type == SecondaryDataEnum.PRECIPITATION:
        ts_func = precip_ts
        # Although we get the precipitation data at the end of a validation run, it is really calibration level data that doesn't change
        csv_file = get_precipitation_timeseries_data_filepath(validation_run.calibration_run)

        if os.path.exists(csv_file):
            logger.info(f"Precipitation file {csv_file} already exists; skipping generation")
            return

        # precip_ts uses positional args only
        args = [ts_csv_location, csv_file]

    else:
        raise CerfException(f"Unsupported data_type: {data_type}")

    logger.info(f"Calling {ts_func.__name__} with arguments: {args}")
    start_time = time.perf_counter()
    ts_func(args)
    elapsed_time = time.perf_counter() - start_time
    logger.info(f"Finished running {ts_func.__name__} in {elapsed_time:.2f} seconds")


def _get_secondary_images_by_date_or_datetime(
        request: Request,
        data_type: SecondaryDataEnum,
        is_datetime: bool
) -> Response:
    """
    Shared helper for retrieving SWE or Soil Moisture images by date/datetime.

    :param request: The HTTP request.
    :param data_type: The SecondaryDataEnum (SWE or SOIL_MOISTURE).
    :param is_datetime: True if the serializer uses a datetime field, False if using a date field.
    :return: DRF Response.
    """
    # Choose serializer based on type
    serializer_cls = (
        GetSoilMoistureImagesByDateRequestSerializer if is_datetime
        else GetSWEImagesByDateRequestSerializer
    )

    data = request.data if request.method == "POST" else request.query_params.dict()
    logger.debug(f"{get_caller_name()}() request from {get_user_email(request)} - {data}")

    validator, error_return = validate_request(serializer_cls, data)
    if error_return:
        return error_return

    validation_run_id = validator.get("validation_run_id")
    if is_datetime:
        date_str = validator.get("datetime").strftime("%Y-%m-%d %H:%M:%S")
    else:
        date_str = validator.get("date").strftime("%Y-%m-%d")

    run, error_return = get_validation_run(
        validation_run_id,
        request.user,
        run_status=[StatusEnum.DONE, StatusEnum.FAILED, StatusEnum.SERVER_ERROR]
    )
    if error_return:
        return error_return
    assert run is not None

    validation_start_date = run.calibration_run.validation_start_period.strftime("%Y-%m-%d")
    validation_end_date = run.calibration_run.validation_end_period.strftime("%Y-%m-%d")
    if date_str < validation_start_date or date_str > validation_end_date:
        return ResponseError(
            f"Date specified {date_str} must be within the Validation Simulation range "
            f"{validation_start_date} to {validation_end_date}"
        )

    # Do not allow plots for Validation Control runs.
    if run.validation_type == ValidationType.VALID_CONTROL.value:
        return ResponseError(f"{data_type.value} plots are not available for a Validation Control run")

    # Retrieve or generate the plots using the shared helper.
    results = get_or_create_secondary_plots(run, date_str, data_type)
    if not results:
        return ResponseError(f"Unable to retrieve {data_type.value} data for {get_job_description(run)}")

    response = {
        "message": f"{data_type} plots created in {results['plot_dir']} for {date_str}",
        "lumped_map": png_to_base64_url(results["lumped_map"]),
        "raw_map": png_to_base64_url(results["raw_map"]),
        "sim_map": png_to_base64_url(results["sim_map"]),
    }

    response_validator, error_response = validate_response(
        GetImagesByDateResponseSerializer,
        response,
        fields_to_truncate=["lumped_map", "raw_map", "sim_map"],
    )
    if error_response:
        return error_response

    logger.debug(
        f"Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - "
        f"{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=['lumped_map', 'raw_map', 'sim_map']))}"
    )

    return Response(response)


@extend_schema(
    request=GetSWEImagesByDateRequestSerializer,
    responses={
        200: GetImagesByDateResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Retrieve SWE images for a given date"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_swe_images_by_date(request: Request) -> Response:
    """
    Retrieve SWE images for a given date.

    :param request: The HTTP request object.
    :return: A Response object with SWE image data or an error message.
    """
    return _get_secondary_images_by_date_or_datetime(
        request, data_type=SecondaryDataEnum.SWE, is_datetime=False
    )


@extend_schema(
    request=GetSoilMoistureImagesByDateRequestSerializer,
    responses={
        200: GetImagesByDateResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Retrieve Soil Moisture images for a given datetime"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_soil_moisture_images_by_date(request: Request) -> Response:
    """
    Retrieve Soil Moisture images for a given datetime.

    :param request: The HTTP request object.
    :return: A Response object with Soil Moisture image data or an error message.
    """
    return _get_secondary_images_by_date_or_datetime(
        request, data_type=SecondaryDataEnum.SOIL_MOISTURE, is_datetime=True
    )


def _get_secondary_timeseries_data(
        request: Request,
        data_type: SecondaryDataEnum,
) -> Response:
    """
    Shared helper for retrieving SWE or Soil Moisture timeseries data.

    :param request: The HTTP request object.
    :param data_type: The SecondaryDataEnum (SWE or SOIL_MOISTURE).
    :return: A DRF Response containing timeseries image and data.
    """
    data = request.data if request.method == "POST" else request.query_params.dict()
    logger.debug(f"{get_caller_name()}() request from {get_user_email(request)} - {data}")

    validator, error_return = validate_request(ValidationRunIdSerializer, data)
    if error_return:
        return error_return

    validation_run_id = validator.get("validation_run_id")

    run, error_return = get_validation_run(
        validation_run_id,
        request.user,
        run_status=[StatusEnum.DONE, StatusEnum.FAILED, StatusEnum.SERVER_ERROR],
    )
    if error_return:
        return error_return
    assert run is not None

    # Map function dispatch by data_type
    data_config = {
        SecondaryDataEnum.SWE: {
            "csv_func": get_swe_timeseries_data_filepath,
            "png_func": get_swe_timeseries_png_filepath,
            "label": "SWE",
        },
        SecondaryDataEnum.SOIL_MOISTURE: {
            "csv_func": get_soil_moisture_timeseries_data_filepath,
            "png_func": get_soil_moisture_timeseries_png_filepath,
            "label": "Soil Moisture",
        }
    }

    if data_type not in data_config:
        logger.error(f"Unsupported data_type: {data_type}")
        return ResponseError(f"Unsupported data type: {data_type}")

    cfg = data_config[data_type]
    label = cfg["label"]

    # Read the CSV file and convert to JSON
    csv_filepath = cfg["csv_func"](run)
    try:
        ts_data = read_csv_as_json(csv_filepath)
    except Exception as e:
        logger.error(f"Error reading {label} timeseries CSV: {e}")
        return ResponseError(f"Failed to read {label} timeseries data file - {e}")

    response = {
        "message": f"Retrieved {label} timeseries data for {get_job_description(run)}",
        "timeseries_image": png_to_base64_url(cfg["png_func"](run)),
        "timeseries_data": ts_data,
    }

    response_validator, error_response = validate_response(
        GetTimeseriesDataResponseSerializer,
        response,
        fields_to_truncate=["timeseries_image", "timeseries_data"],
        max_length=10,
    )
    if error_response:
        return error_response

    logger.debug(
        f"Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - "
        f"{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=['timeseries_image', 'timeseries_data'], max_length=10))}"
    )

    return Response(response)


@extend_schema(
    request=ValidationRunIdSerializer,
    responses={
        200: GetTimeseriesDataResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Retrieve SWE timeseries data for a given validation run"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_swe_timeseries_data(request: Request) -> Response:
    """
    Retrieve SWE timeseries data for a given validation run.

    :param request: The HTTP request object.
    :return: A Response object with SWE timeseries image and data or an error message.
    """
    return _get_secondary_timeseries_data(request, SecondaryDataEnum.SWE)


@extend_schema(
    request=ValidationRunIdSerializer,
    responses={
        200: GetTimeseriesDataResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Retrieve Soil Moisture timeseries data for a given validation run"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_soil_moisture_timeseries_data(request: Request) -> Response:
    """
    Retrieve Soil Moisture timeseries data for a given validation run.

    :param request: The HTTP request object.
    :return: A Response object with Soil Moisture timeseries image and data or an error message.
    """
    return _get_secondary_timeseries_data(request, SecondaryDataEnum.SOIL_MOISTURE)


def read_csv_as_json(csv_filepath: str, keys: list[str] | None = None) -> list[dict[str, str]]:
    """
    Reads a CSV file and returns a list of dictionaries.

    Always reads the header row from the file first.
    If `keys` is provided, it overrides the column names (but still skips the file header).

    :param csv_filepath: Path to the CSV file.
    :param keys: Optional list of keys to enforce instead of using the header row.
    :return: A list of dictionaries representing each row in the CSV.
    """
    with open(csv_filepath, newline='') as csvfile:
        reader = csv.reader(csvfile)

        # Always read REAL header first from file
        original_header = next(reader)

        # Use override keys if provided, otherwise stick with the file's header
        header = keys if keys else original_header

        return [dict(zip(header, row)) for row in reader]


def should_generate_swe(modules_by_name_for_job: dict[str, Module]) -> bool:
    """
    Determine whether SWE (Snow Water Equivalent) output should be generated
    for the current calibration/validation/forecast job.

    - Return True if ANY module in this job belongs to the "SnowMelt" module group.

    :param modules_by_name_for_job: Dict mapping module name → Module instance
                                    for only the modules active in this job.
    :return: True if SWE output is required; False otherwise.
    """
    for module in modules_by_name_for_job.values():
        # Assumes group membership is already prefetched via get_cached_modules_with_groups
        if any(group.name.lower() == "snowmelt" for group in module.groups.all()):
            return True
    return False


def should_generate_soil_moisture(modules_by_name_for_job: dict[str, Module]) -> bool:
    """
    Determine whether Soil Moisture timeseries data should be generated.

    - Return True ONLY if the SMP module is present in the current job's module list.

    :param modules_by_name_for_job: Dict mapping module name → Module instance
                                    for only the modules active in this job.

    :return: True if SMP is one of the modules in the job; False otherwise.
    """
    return "SMP" in modules_by_name_for_job
