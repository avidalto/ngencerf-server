import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlencode

import requests
from datetimerange import DateTimeRange
from django.conf import settings
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_aware
from mswm.utils.ginputfunc import call_icefabric_gpkg

from calibration.enums import ForcingSourceEnum, DomainEnum
from calibration.models import CalibrationParameter, CalibrationRun, CalibrationFormulation
from calibration.util.caching import get_cached_module_by_name, get_cached_modules_with_groups
from calibration.util.calibration_validators import ModuleDataListSerializer
from calibration.util.cloud_util import join_url, is_dir
from calibration.util.ngen_locations import get_geopackage_dir_for_job
from calibration.views.common import validate_response_data

logger = logging.getLogger(__name__)

default_headers = {
    "Content-Type": "application/json"
}


def fetch_from_data_services(method: str, url: str, headers: dict = None, payload: dict = None) -> dict | str:
    """
     Issue an HTTP request to Data Services and return a validated JSON object.

    :param method: HTTP method (e.g., 'GET' or 'POST').
    :param url: Fully qualified Data Services endpoint URL.
    :param headers: Optional HTTP headers to include in the request.
    :param payload: Optional JSON payload for POST requests.
    :return: Parsed response JSON as a dictionary OR raw text for non-JSON endpoints.
    :raises DataServicesException: On network errors, HTTP error status codes, or invalid JSON when JSON is expected.
    """
    status_code = None
    response_text = None
    logger.info(f'Sending request to {url}')
    if payload:
        logger.info(f"Data Services payload: {payload}")

    try:
        start_time = time.perf_counter()  # Record the start time for performance tracking

        # Send the appropriate HTTP request based on the method
        if method == 'GET':
            response = requests.get(url, headers=headers)
        elif method == 'POST':
            response = requests.post(url, headers=headers, json=payload)
        else:
            raise DataServicesException(f"Unsupported HTTP method: {method}")

        # Log the time taken for the request
        elapsed_time = time.perf_counter() - start_time
        minutes, seconds = divmod(elapsed_time, 60)  # Convert to minutes and seconds
        logger.info(f"Request to {url} took {int(minutes)}:{int(seconds):02} (minutes:seconds).")

        # Capture status code and response content before raising an exception
        status_code = response.status_code
        response_text = response.text

        # Handle potential errors based on the status code
        if 400 <= status_code < 500:
            msg = f"Client error while accessing {url}: {status_code} - {response_text}"
            logger.error(msg)
            raise DataServicesException(msg)
        elif 500 <= status_code:
            msg = f"Server error while accessing {url}: {status_code} - {response_text[:1000] + '... (truncated)'}"
            logger.error(msg)
            raise DataServicesException(msg)

        # Check if the response is HTML instead of JSON (indicating an error page)
        content_type = response.headers.get('Content-Type', '') or ''
        if 'text/html' in content_type:
            msg = f"Call to {url} returned HTML error from Data Services"
            logger.error(msg)
            raise DataServicesException(msg, status_code)

        response.raise_for_status()  # Raise HTTPError for bad responses

        # If not JSON, return text (CSV/plain text endpoints)
        content_type_l = content_type.lower()
        is_json = ('application/json' in content_type_l) or content_type_l.endswith('+json')
        if not is_json:
            return response_text

        # Parse the response JSON and validate its format
        response_data = response.json()

        # # Ensure the response is a dictionary
        if not isinstance(response_data, dict):
            raise DataServicesException(
                f"Unexpected response format: Expected a dictionary but got {type(response_data).__name__}. Response: {response_data}"
            )

        return response_data

    except requests.exceptions.HTTPError as e:
        message = f"Call to {url} failed with {status_code}. Response text: {response_text if response_text else 'No response received'}"
        logger.error(message)
        raise DataServicesException(message, status_code) from e

    except requests.exceptions.RequestException as e:
        # Handle connection, timeout, or other request errors
        message = f"Call to {url} failed to connect or timed out"
        logger.error(message)
        raise DataServicesException(message) from e

    except ValueError as e:
        # Handle invalid JSON responses
        logger.error(f"Invalid JSON response from {url}: {response_text}")
        raise DataServicesException("Invalid JSON received from Data Services") from e


class DataServicesException(Exception):
    """
    Custom exception for errors related to Data Services.

    :param message: Description of the error.
    :param status_code: Optional HTTP status code associated with the error.
    """

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def get_geopackage_from_data_services(run: CalibrationRun):
    """
    Retrieve a GeoPackage for the run's gage from MSWM, which gets it from Data Services and set the file path on the run.

    :param run: A CalibrationRun object with associated gage information.
    """
    if run.gage:
        logger.info('Retrieving geopackage from Data Services')
        geopackage_dir = get_geopackage_dir_for_job(run)
        os.makedirs(geopackage_dir, exist_ok=True)

        args = [
            run.gage.gage_id,
            'gage',
            run.gage.domain.name,
            geopackage_dir,
            settings.ENTERPRISE_DATA_ENV,
            settings.HYDROFABRIC_SOURCE,
        ]

        logger.info(f'Calling call_icefabric_gpkg with arguments: {args}')
        call_icefabric_gpkg(*args)


def _parse_utc(dt_str: str) -> datetime:
    """
    Parse an ISO-8601 datetime string and return a timezone-aware UTC datetime.

    Behavior:
      - If the input string has no timezone/offset (naive), it is interpreted as UTC.
      - If the input string includes a timezone/offset, it is converted to UTC.

    :param dt_str: ISO-8601 datetime string from Data Services (e.g., "1990-10-01T05:00:00").
    :return: A timezone-aware datetime normalized to UTC.
    :raises ValueError: If the string cannot be parsed into a datetime.
    """
    dt = parse_datetime(dt_str)
    if dt is None:
        raise ValueError(f"Invalid datetime string: {dt_str}")
    # Data Services returns naive times; interpret as UTC
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_naive(dt):
    """
    Format a datetime for Data Services URL parameters as a *naive* timestamp string.

    Data Services endpoints for query params expect timestamps *without* any timezone/offset
    suffix (e.g., "2014-10-01 00:00:00", not "2014-10-01 00:00:00+00:00").

    Behavior:
      - If the input datetime is timezone-aware, the timezone info is stripped (tzinfo removed).
        This assumes the datetime already represents the correct instant in time for the request
        (typically UTC in this codebase).
      - If the input datetime is naive, it is used as-is.

    :param dt: Datetime to serialize for URL parameters.
    :return: Naive datetime string in "YYYY-MM-DD HH:MM:SS" format.
    """
    if is_aware(dt):
        dt = dt.replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def get_observational_date_range_from_data_services(run: CalibrationRun) -> DateTimeRange:
    """
    Retrieve observational date range for the run's gage from Data Services.

    :param run: A CalibrationRun object with associated gage information.
    """

    logger.info('Getting observational data from Data Services')
    url = urljoin(
        settings.ENTERPRISE_DATA_URL,
        settings.ENTERPRISE_DATA_OBSERVATION_DATA_INFO_ENDPOINT.format(
            gage_id=run.gage.gage_id
        )
    )
    observational_info_json = fetch_from_data_services('GET', url, headers=default_headers)

    dr = observational_info_json.get('date_range') or {}
    start = _parse_utc(dr.get('start'))
    end = _parse_utc(dr.get('end'))

    return DateTimeRange(start, end)


def get_observational_data_from_data_services(run: CalibrationRun, date_time_range: DateTimeRange):
    """
    Retrieves observational data from Data Services and updates the CalibrationRun instance.

    :param run: A CalibrationRun object with associated gage information.
    :param date_time_range: Date range to subset the data
    """
    logger.info('Getting observational data from Data Services')
    params = {
        "start_date": _format_naive(date_time_range.start_datetime),
        "end_date": _format_naive(date_time_range.end_datetime),
    }

    url = urljoin(
        settings.ENTERPRISE_DATA_URL,
        settings.ENTERPRISE_DATA_OBSERVATION_DATA_ENDPOINT.format(
            gage_id=run.gage.gage_id) + "?" + urlencode(params)
    )
    return fetch_from_data_services('GET', url, headers=default_headers)


def clear_times(run: CalibrationRun, cli: bool = False):
    """
    Clears the time-related fields of a CalibrationRun instance, forcing a recalculation later.

    This function resets all time and period fields to None, which is useful when the GUI triggers
    a recalculation of these time boundaries. If the operation is initiated via the CLI (cli=True),
    the time fields are preserved because it is assumed that the user intends to keep them as set.

    :param run: A CalibrationRun instance whose time-related fields will be cleared.
    :param cli: A boolean flag indicating if the process is running from the CLI.
                If True, the time fields are not cleared.
    """
    if not cli:
        run.time_range_start = None
        run.time_range_end = None
        run.calibration_start_period = None
        run.calibration_end_period = None
        run.validation_start_period = None
        run.validation_end_period = None
        run.calibration_eval_start_period = None
        run.calibration_eval_end_period = None
        run.validation_eval_start_period = None
        run.validation_eval_end_period = None


def should_use_bmi_forcing(run: CalibrationRun) -> bool:
    """
    Determine whether BMI forcing should be used for this run.

    BMI forcing is used only when a gage is present and the run is CONUS + AORC
    and BMI forcing is enabled in settings.

    :param run: CalibrationRun instance to evaluate.
    :return: True if BMI forcing should be used, otherwise False.
    """
    if run.gage is None:
        return False

    # Use BMI forcing only if Conus and AORC
    return settings.USE_BMI_FORCING and run.gage.domain == DomainEnum.CONUS.db_instance and run.forcing_source_requested == ForcingSourceEnum.AORC.db_instance


def get_forcing_data_from_s3(run: CalibrationRun, forcing_source_name: str):
    """
    Populate forcing paths for the run from configured S3 forcing directories.

    Skips forcing retrieval when BMI forcing applies (CONUS + AORC).
    On success, sets run.forcing_eds_dir_path and run.forcing_source_actual and clears times.
    The CalibrationRun instance is mutated but not saved.

    settings.FORCING_DATA_DIRS_xxx is a dict of S3 URLs (prefixes).

    :param run: CalibrationRun instance with associated gage information.
    :param forcing_source_name: Name of the forcing source to retrieve data for.
    :raises DataServicesException: If the forcing data cannot be found in the local S3 directories.
    """
    if should_use_bmi_forcing(run):
        logger.info("Skipping forcing retrieval for CONUS and AORC")
        return

    forcing_containers = (
        settings.FORCING_DATA_DIRS_AORC
        if forcing_source_name == ForcingSourceEnum.AORC.value
        else settings.FORCING_DATA_DIRS_RETRO
    )

    for src_key, s3_uri in forcing_containers.items():
        # <prefix>/<domain>/Gage_<gage_id>
        forcing_dir = join_url(s3_uri, run.gage.domain.name, f"Gage_{run.gage.gage_id}")

        if is_dir(forcing_dir):
            logger.info(f"Found forcing directory {forcing_dir}")
            run.forcing_eds_dir_path = forcing_dir
            run.forcing_source_actual = ForcingSourceEnum.get_instance(src_key)
            clear_times(run)
            logger.info(
                "Setting run.forcing_eds_dir_path to %s; forcing_source_actual=%s",
                run.forcing_eds_dir_path, run.forcing_source_actual
            )
            return
        else:
            logger.info(
                "Forcing directory for gage %s doesn't exist at %s (key: %s)",
                run.gage.gage_id, forcing_dir, src_key
            )

    raise DataServicesException(f"Could not find forcing data for gage {run.gage.gage_id}")


def get_module_metadata_from_data_services(
        run: CalibrationRun,
        modules: set[str],
        gage_id: str | None = None,
        domain: str | None = None
) -> tuple[dict, list[dict]]:
    """
    Fetch module parameter metadata from Data Services for modules that require EDFS.

    This function performs an external HTTP request and should be executed outside of a DB transaction.

    Behavior:
    ----------
    - The input `modules` is the full set of module names for the run.
    - Only modules where Module.use_edfs == True are sent to Data Services.
    - Module definitions are resolved from the shared Redis-backed module cache (no DB queries).
    - If no modules require EDFS, no HTTP request is made and ({}, []) is returned.
    - Unknown module names that are not present in the cache are ignored.
    - Only modules returned by Data Services are processed and normalized.

    Gage context:
    -------------
    - Data Services requires gage_id (Gage.gage_id) and domain as query params.
    - If gage_id/domain are not provided, they are derived from run.gage.
    - run.gage only needs to be populated in memory; it does not need to be saved.

    :param run: CalibrationRun instance used for context only (not mutated).
    :param modules: Set of module names associated with the run. This may include modules that do not use EDFS; those will be filtered out automatically.
    :param gage_id: Optional gage identifier (Gage.gage_id). If not provided, uses run.gage.gage_id.
    :param domain: Optional domain name for Data Services. If not provided, uses run.gage.domain.name.
    :return: Tuple (module_metadata, eds_errors)
        - module_metadata: validated/normalized dict matching ModuleDataListSerializer (falsy {} on failure).
        - eds_errors: list of error dicts for Data Services failures.
    """
    logger.info('Fetching module metadata from Data Services')

    if not modules:
        return {}, []

    # -------------------------------------------------------
    # Resolve module definitions from shared cache
    # and keep only modules that require EDFS metadata.
    # -------------------------------------------------------
    cached_modules = get_cached_modules_with_groups()  # {module_name -> Module ORM instance}

    edfs_modules = sorted(
        name
        for name in modules
        if name in cached_modules and getattr(cached_modules[name], "use_edfs", True)
    )

    # If nothing requires EDFS, skip the external call entirely.
    if not edfs_modules:
        logger.info("No modules with use_edfs=True; skipping Data Services call.")
        return {}, []

    logger.info("Fetching module metadata from Data Services")

    # -------------------------------------------------------
    # Resolve gage context (explicit args take precedence)
    # -------------------------------------------------------
    resolved_gage_id = gage_id or (run.gage.gage_id if run.gage else None)
    resolved_domain = domain or (run.gage.domain.name if run.gage and run.gage.domain_id else None)

    if not resolved_gage_id or not resolved_domain:
        raise ValueError(
            "get_module_metadata_from_data_services requires gage_id and domain. "
            "Pass them explicitly or ensure run.gage is set (run.gage = gage) before calling."
        )

    # urlencode(doseq=True) only repeats keys when the value is a sequence (e.g., list)
    params = {
        "modules": edfs_modules,
        "gage_id": resolved_gage_id,
        "domain": resolved_domain,
        "source": settings.HYDROFABRIC_SOURCE
    }

    url = urljoin(
        settings.ENTERPRISE_DATA_URL,
        settings.ENTERPRISE_DATA_MODULE_METADATA_ENDPOINT
        + "?"
        + urlencode(params, doseq=True)
    )

    try:
        module_json = fetch_from_data_services("GET", url, headers=default_headers)
    except DataServicesException as e:
        logger.exception("Error retrieving module parameter data from Data Services")
        return {}, [{
            "name": "parameters",
            "message": str(e),
            "status_code": e.status_code if e.status_code else None
        }]

    module_metadata = validate_response_data(
        ModuleDataListSerializer,
        module_json,
        "Module metadata from Data Services is not in the expected format"
    )

    # Check for any error fields from EDFS
    eds_errors: list[dict] = []
    for module_data in module_metadata.get("modules", []):
        err = module_data.get("error")
        if err:
            module_name = module_data.get("module_name")
            eds_errors.append({
                "name": "parameters",
                "message": f"{module_name} - {err}",
                "status_code": None,
            })

    # Only translate names for modules that actually have parameters
    fix_module_metadata(module_metadata)

    return module_metadata, eds_errors


def update_parameters(run: CalibrationRun, module_metadata: dict, gage_changed: bool = False):
    """
    Persist module parameters for an existing (run, module) formulation.

    The corresponding CalibrationFormulation row must already exist.
    This function performs database writes and should be called inside a write transaction.

    :param run: CalibrationRun instance the parameters belong to.
    :param module_metadata: Normalized module metadata containing calibratable_parameters.
    :param gage_changed: If True, update initial_value for existing parameters while
                          preserving min/max and other fields.
    :return: None.
    """

    for module_data in module_metadata.get('modules'):

        module_name = module_data['module_name']
        module_instance = get_cached_module_by_name(module_name)

        calibration_formulation = CalibrationFormulation.objects.get(
            calibration_run=run,
            module=module_instance,
        )

        new_params: list[CalibrationParameter] = []

        # Save or update parameters for the module
        parameters = module_data.get('calibratable_parameters', [])
        if not parameters:
            logger.warning(f"Module '{module_name}' has no calibratable parameters.")
        else:
            for param in parameters:
                # Build candidate parameters from Data Services metadata (new rows insert from this list).
                initial_value = safe_float(
                    param.get('initial_value'),
                    "Initial value",
                    param.get('name'),
                    module_name
                )

                new_param = CalibrationParameter(
                    name=param['name'],
                    calibration_formulation=calibration_formulation,
                    data_type=param['data_type'],
                    description=param['description'],
                    initial_value=initial_value,
                    minimum=param.get('min'),
                    maximum=param.get('max'),
                    units=param['units']
                )
                new_params.append(new_param)

            # Insert only missing parameters:
            # bulk_create issues INSERTs only; with ignore_conflicts=True, any (formulation, name)
            # rows that already exist (per the unique constraint) are skipped here and handled later.
            if new_params:
                CalibrationParameter.objects.bulk_create(new_params, ignore_conflicts=True)

            # Existing DB rows:
            # When the gage has changed we *only* refresh initial_value from Data Services.
            # Structural fields (min/max, units, description, data_type) are never overwritten.
            if gage_changed and new_params:
                existing_params = CalibrationParameter.objects.filter(
                    calibration_formulation=calibration_formulation,
                    name__in=[p.name for p in new_params]
                )

                # Build a lookup so we can match newly computed parameters to existing DB rows
                # and selectively update only the initial_value field.
                db_param_by_key = {
                    (p.calibration_formulation_id, p.name): p
                    for p in existing_params
                }

                params_to_update: list[CalibrationParameter] = []

                for param in new_params:
                    key = (param.calibration_formulation.id, param.name)
                    if key in db_param_by_key:
                        db_param_by_key[key].initial_value = param.initial_value
                        params_to_update.append(db_param_by_key[key])

                # Perform a narrow bulk_update to avoid touching other columns and
                # minimize writes on unchanged parameter metadata.
                if params_to_update:
                    CalibrationParameter.objects.bulk_update(params_to_update, ['initial_value'])


translation_map = {
    ("CFE-S", "soil_params.smcmax"): "maxsmc",
    ("CFE-S", "soil_params.satdk"): "satdk",
    ("CFE-S", "soil_params.slop"): "slope",
    ("CFE-S", "soil_params.b"): "b",
    ("CFE-S", "K_lf"): "Klf",
    ("CFE-S", "K_nash"): "Kn",
    ("CFE-S", "soil_params.satpsi"): "satpsi",
    ("CFE-S", "soil_params.wltsmc"): "wltsmc",

    ("CFE-X", "soil_params.smcmax"): "maxsmc",
    ("CFE-X", "soil_params.satdk"): "satdk",
    ("CFE-X", "soil_params.slop"): "slope",
    ("CFE-X", "soil_params.b"): "b",
    ("CFE-X", "K_lf"): "Klf",
    ("CFE-X", "K_nash"): "Kn",
    ("CFE-X", "soil_params.satpsi"): "satpsi",
    ("CFE-X", "soil_params.wltsmc"): "wltsmc",

    ("Noah-OWP-Modular", "MAXSMC"): "SMCMAX",
    ("Noah-OWP-Modular", "CWPVT"): "CWP",
    ("Noah-OWP-Modular", "SATDK"): "DKSAT",

    ("LASAM", "theta_e"): "smcmax",
    ("LASAM", "theta_r"): "smcmin",
    ("LASAM", "n"): "van_genuchten_n",
    ("LASAM", "alpha"): "van_genuchten_alpha",
    ("LASAM", "Ks"): "hydraulic_conductivity",
    ("LASAM", "field_capacity_psi"): "field_capacity",

    ("SFT", "soil_params.smcmax"): "smcmax",
    ("SFT", "soil_params.b"): "b",
    ("SFT", "soil_params.satpsi"): "satpsi",
    ("SFT", "soil_params.quartz"): "quartz",
    ("SFT", "soil_temperature"): "soil_temperature_profile",

    ("SMP", "soil_params.smcmax"): "smcmax",
    ("SMP", "soil_params.b"): "b",
    ("SMP", "soil_params.satpsi"): "satpsi",
}


def fix_module_metadata(module_metadata):
    """
    Normalize module metadata by translating parameter names using translation map.

    :param module_metadata: Dictionary containing module metadata.
                     Example structure:
                     {
                         "modules": [
                             {
                                 "module_name": "module_name",
                                 "calibratable_parameters": [
                                     {"name": "full_param_name", "value": 123}
                                 ]
                             }
                         ]
                     }
    """
    modules = (module_metadata or {}).get("modules") or []
    for module in modules:
        # If EDFS reported an error for this module, do not touch it.
        if module.get("error"):
            continue

        module_name = module.get("module_name")
        if not module_name:
            continue

        params = module.get("calibratable_parameters") or []
        for param in params:
            param_name = param["name"]  # Extract the parameter name
            if not param_name:
                continue

            key = (module_name, param_name)  # Create a tuple key
            mapped = translation_map.get(key)
            # Check if the key exists in the translation_map
            if mapped:
                logger.info(f"Translating {key} to {mapped}")
                param["name"] = mapped


def safe_float(value, label, param_name, module_name):
    """
    Attempt to convert a value to float, returning None on empty or invalid values.

    Logs a warning including module and parameter context when conversion fails.

    :param value: Raw value to convert.
    :param label: Human-readable label for the value being converted.
    :param param_name: Name of the parameter being processed.
    :param module_name: Name of the module the parameter belongs to.
    :return: Float value if conversion succeeds, otherwise None.
    """
    try:
        return float(value) if value else None
    except (ValueError, TypeError):
        logger.warning(f"{label} '{value}' for parameter '{param_name}' for module '{module_name}' is not a valid float")
        return None
