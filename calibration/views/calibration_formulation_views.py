import json
import logging
from typing import Any

from django.db import transaction
from drf_spectacular.utils import extend_schema, OpenApiResponse
from mswm.build_inputs import validate_topoflow_glacier
from rest_framework.decorators import api_view
from rest_framework.response import Response

from calibration.enums import StatusEnum, DataTypeEnum
from calibration.models import CalibrationFormulation, CalibrationSlothParam, CalibrationParameter, CalibrationRun, \
    CalibrationStopCriteria, CalibrationModulePropertyValue, ModulePropertyChoice
from calibration.util.caching import get_cached_module_by_name, get_cached_modules_with_groups, get_cached_module_groups, get_cached_modules_by_id, \
    get_cached_module_properties, get_cached_module_property_choices
from calibration.util.calibration_validators import ValidateFormulationRequestSerializer, \
    SaveFormulationRequestSerializer, ErrorResponseSerializer, ValidateFormulationResponseSerializer, \
    SaveFormulationResponseSerializer, EmptySerializer, GetModulesResponseSerializer
from calibration.util.ngen_locations import get_geopackage_file_path
from calibration.views import ngen_cal_input
from calibration.views.calibration_optimization_views import write_optimization_inputs
from calibration.views.called_from import get_caller_name
from calibration.views.common import get_calibration_run, ResponseError, handle_exceptions, validate_response, validate_request, SLOTH, \
    get_user_email, join_with_or, get_elapsed_str, readonly_transaction
from calibration.views.data_services import get_module_metadata_from_data_services, update_parameters

logger = logging.getLogger(__name__)


@extend_schema(
    request=EmptySerializer,
    responses={
        200: GetModulesResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get static list of modules and groups"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_modules(request) -> Response:
    """
    Retrieve module and group information

    :param request: The HTTP request containing either POST data or query parameters.
    :return: A JSON response with the calibration run ID, status, modules, and module groups.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(EmptySerializer, data)
    if error_return:
        return error_return

    # Retrieve all modules with their groups from the cache
    cached_modules = get_cached_modules_with_groups()

    # Prepare modules list as dictionaries for response serialization
    module_groups_list = [
        {
            "name": module.name,
            "display_name": module.display_name,
            "description": module.description,
            "is_active": module.is_active,
            "groups": sorted([g.name for g in module.groups.all()], key=lambda n: n)

        }
        for module in cached_modules.values()
    ]

    # Retrieve ordered list of module groups from cache
    module_groups = get_cached_module_groups()

    response = {'modules': module_groups_list, 'module_groups': module_groups}

    response_validator, error_response = validate_response(GetModulesResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')

    return Response(response_validator.data)


def get_sloth_parameters(run: CalibrationRun) -> list[dict[str, str]]:
    """
    Retrieve Sloth parameters for a given calibration run.
    Uses cached modules keyed by ID to resolve maps_to_module names.

    :param run: The calibration run instance.
    :return: A list of Sloth parameters formatted as dictionaries.
    """
    sloth_parameters = list(
        CalibrationSlothParam.objects.filter(calibration_run=run).values(
            'param_name', 'param_count', 'param_type', 'param_units',
            'param_location', 'param_value', 'maps_to_module_id', 'maps_to_variable_name'
        )
    )

    modules_by_id = get_cached_modules_by_id()
    for sp in sloth_parameters:
        module = modules_by_id.get(sp['maps_to_module_id'])
        sp['maps_to_module'] = module.name if module else None
        del sp['maps_to_module_id']

    return sloth_parameters


def _property_value_to_str(v: CalibrationModulePropertyValue | None, data_type: str) -> str | None:
    """
    Convert CalibrationModulePropertyValue's typed storage columns into the UI wire format (string).

    The UI submits property_value as a string, and ModuleProperty.data_type drives interpretation.
    To keep the API round-trip stable, we send values back as strings too.
    """
    if not v:
        return None

    if data_type == DataTypeEnum.BOOLEAN.value:
        if v.value_bool is None:
            return None
        return "true" if v.value_bool else "false"

    if data_type == DataTypeEnum.INTEGER.value:
        return None if v.value_int is None else str(v.value_int)

    if data_type == DataTypeEnum.DOUBLE.value:
        return None if v.value_double is None else str(v.value_double)

    # STRING (or unknown fallback)
    return None if v.value_str is None else str(v.value_str)


def _choice_value_to_str(c: ModulePropertyChoice) -> str:
    """
    Convert ModulePropertyChoice's storage columns into the UI wire format (string).

    Exactly one of (value_int, value_str) is set (enforced by ck_choice_exactly_one_value).
    """
    if c.value_int is not None:
        return str(c.value_int)
    return c.value_str or ""  # should not happen if constraint is enforced


@extend_schema(
    request=ValidateFormulationRequestSerializer,
    responses={
        200: ValidateFormulationResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Validate the module list from the formulation tab."
)
@api_view(['POST'])
@handle_exceptions
def load_formulation_tab(request) -> Response:
    """
    Validate the module list from the formulation tab.

    In addition to validation messages, returns module property definitions needed by the UI
    to render inputs for the selected modules, including current saved values (if any).

    :param request: The HTTP request containing POST data with a list of modules.
    :return: A JSON response with any warnings or errors, plus module property schemas.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(ValidateFormulationRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    new_module_names = set(validator.get('modules'))

    run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=list(StatusEnum))
    if error_return:
        return error_return
    assert run is not None

    formulation_errors, formulation_warnings, formulation_messages = validate_formulation(
        new_module_names,
        get_geopackage_file_path(run)
    )

    # ---------------------------------------------------------------------
    # Module properties schema for selected modules (and current saved values, if any)
    # ---------------------------------------------------------------------

    # 1) Resolve selected module_ids from cache (not from existing formulations),
    # so the UI can render property inputs even before the selection is saved.
    modules_by_name = get_cached_modules_with_groups()  # dict[name -> Module], already hydrated
    module_ids = [
        modules_by_name[name].id
        for name in new_module_names
        if name in modules_by_name
    ]

    # 2) Find formulations for selected modules in this run (used only to locate any current saved values).
    formulations_for_run = list(
        CalibrationFormulation.objects
        .filter(calibration_run=run, module__name__in=new_module_names)
        .select_related("module")
        .only("id", "module_id", "module__name")
    )
    formulation_ids = [f.id for f in formulations_for_run]

    # 3) Pull ModuleProperty definitions for the selected modules (cache, then filter in-memory).
    all_props = get_cached_module_properties()
    prop_defs = [p for p in all_props if p.module_id in set(module_ids)]
    prop_ids = {p.id for p in prop_defs}

    # 4) Pull ModulePropertyChoice rows for those properties (cache, then filter in-memory; already ordered).
    all_choices = get_cached_module_property_choices()
    choices = [c for c in all_choices if c.module_property_id in prop_ids]

    choices_by_property_id: dict[int, list[ModulePropertyChoice]] = {}
    for c in choices:
        choices_by_property_id.setdefault(c.module_property_id, []).append(c)

    # 5) Pull current saved values for this run (single query) and map by (formulation_id, property_id).
    current_value_map: dict[tuple[int, int], CalibrationModulePropertyValue] = {}
    if formulation_ids:
        current_values = list(
            CalibrationModulePropertyValue.objects
            .filter(calibration_formulation_id__in=formulation_ids)
            .select_related("module_property", "calibration_formulation")
            .only(
                "calibration_formulation_id",
                "module_property_id",
                "value_bool",
                "value_int",
                "value_double",
                "value_str",
            )
        )
        current_value_map = {
            (v.calibration_formulation_id, v.module_property_id): v
            for v in current_values
        }

    # 6) Build a formulation_id lookup by module_id for quick joins (may be empty if nothing is saved yet).
    formulation_id_by_module_id = {f.module_id: f.id for f in formulations_for_run}

    # 7) Group properties by module name for output, attaching current saved value (if any).
    modules_by_id = get_cached_modules_by_id()
    props_by_module_name: dict[str, list[dict[str, Any]]] = {}
    for p in prop_defs:
        module_name = modules_by_id[p.module_id].name
        props_by_module_name.setdefault(module_name, [])

        formulation_id = formulation_id_by_module_id.get(p.module_id)
        current_v = current_value_map.get((formulation_id, p.id)) if formulation_id else None

        effective_value = (
            _property_value_to_str(current_v, p.data_type)
            if current_v is not None
            else p.default_value
        )
        prop_payload: dict[str, Any] = {
            "name": p.name,
            "display_name": p.display_name,
            "description": p.description,
            "data_type": p.data_type,
            "value": effective_value
        }

        # If choices exist, return them with choice.value as a string for stable round-trip.
        prop_choices = choices_by_property_id.get(p.id, [])
        if prop_choices:
            prop_payload["choices"] = [
                {
                    "value": _choice_value_to_str(c),  # always string
                    "label": c.label,
                    "description": c.description,
                }
                for c in prop_choices
            ]

        props_by_module_name[module_name].append(prop_payload)

    # 8) Only include modules that have at least one property.
    module_properties_payload = {
        "modules": [
            {
                "name": module_name,
                "properties": props,
            }
            for module_name in sorted(new_module_names)
            if (props := props_by_module_name.get(module_name))
        ]
    }

    response: dict[str, Any] = {
        "module_properties": module_properties_payload
    }
    if formulation_warnings:
        response["formulation_warnings"] = formulation_warnings
    if formulation_errors:
        response["formulation_errors"] = formulation_errors
    if formulation_messages:
        response["formulation_messages"] = formulation_messages

    response_validator, error_response = validate_response(ValidateFormulationResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=SaveFormulationRequestSerializer,
    responses={
        200: SaveFormulationResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Save formulation tab data"
)
@api_view(['POST'])
@handle_exceptions
def save_formulation_tab(request) -> Response:
    """
    Save or update calibration formulations for a calibration run.

   High-level flow:
    - Determine which modules are new, removed, or missing parameters.
    - Fetch module metadata from Data Services outside of any write transaction.
    - In a single atomic block:
        * Delete unused CalibrationFormulation rows and their parameters.
        * Bulk-create missing CalibrationFormulation rows.
        * Persist CalibrationParameter rows via update_parameters().
        * Persist CalibrationModulePropertyValue rows for module properties (replace-all for the run).
        * Refresh Sloth and optimization-related state as required.

    Error handling:
    - Data Services errors are accumulated in eds_errors as a list of error objects.
    - Database writes are only performed after metadata has been successfully fetched.

    :param request: The HTTP request containing POST data with formulation details.
    :return: A JSON response confirming the update along with any warnings or errors.
    """
    data = request.data
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(SaveFormulationRequestSerializer, data)
    if error_return:
        return error_return

    new_module_names = set(validator.get('modules'))
    calibration_run_id = validator.get('calibration_run_id')
    use_sloth = validator.get('use_sloth')
    sloth_parameters = validator.get('sloth_parameters')
    module_properties: list[dict[str, Any]] | None = validator.get('module_properties')

    have_lstm = 'LSTM' in new_module_names
    if have_lstm and (sloth_parameters or use_sloth):
        return ResponseError("You cannot specify sloth_parameters or use_sloth when using LSTM")

    run, error_return = get_calibration_run(calibration_run_id, request.user)
    if error_return:
        return error_return
    assert run is not None

    if not run.gage:
        return ResponseError('Gage must be specified before selecting formulation')

    formulation_errors, formulation_warnings, _ = validate_formulation(new_module_names, get_geopackage_file_path(run))

    if not use_sloth and sloth_parameters:
        return ResponseError(f'You must check the box to allow {SLOTH} parameters to be specified')

    # Set run.use_sloth and handle Sloth parameters
    run.use_sloth = use_sloth
    # Initialize the eds_errors list
    eds_errors: list[dict] = []

    # Fetch all formulations and determine changes
    existing_module_ids = set(
        CalibrationFormulation.objects
        .filter(calibration_run=run)
        .values_list("module_id", flat=True)
    )

    modules_by_id = get_cached_modules_by_id()
    modules_by_name = {m.name: m for m in modules_by_id.values()}

    existing_module_names = {
        modules_by_id[mid].name
        for mid in existing_module_ids
        if mid in modules_by_id
    }

    # Determine which modules to delete and add
    to_be_added: set[str] = new_module_names - existing_module_names
    to_be_unused = existing_module_names - new_module_names

    # We also want to fetch for those Formulations that have no parameters, in case there was an error previously
    with readonly_transaction():
        formulations_without_params = (
            CalibrationFormulation.objects
            .filter(calibration_run=run)
            .filter(calibrationparameter__isnull=True)
            .values_list("module__name", flat=True)
            .distinct()
        )
        # Add modules that already exist but have no parameters, limited to the current selection to avoid refetching for soon-to-be-deleted modules
        to_be_added.update(set(formulations_without_params) & new_module_names)

    # Fetch module metadata from Data Services (outside write transaction)
    module_metadata, ds_errors = get_module_metadata_from_data_services(run, to_be_added)
    if ds_errors:
        eds_errors.extend(ds_errors)

    # ---------------------------------------------------------------------
    # Validate module_properties BEFORE we delete/insert anything for them
    # ---------------------------------------------------------------------
    plan, plan_error = build_module_property_write_plan(
        module_properties=module_properties,
        user=request.user,
        modules_by_name=modules_by_name,
        allowed_module_names=new_module_names,
    )
    if plan_error:
        return ResponseError(
            "Validation error",
            validation_errors=plan_error,
            http_status=400,
        )

    with transaction.atomic():
        # ------------------------------------------------------------
        # Delete unused formulations
        # ------------------------------------------------------------
        if to_be_unused:
            logger.info(f"Deleting unused modules: {to_be_unused}")
            delete_unused_formulations(to_be_unused, run)

        # ------------------------------------------------------------
        # Create missing formulations
        # ------------------------------------------------------------
        existing_module_ids = set(
            CalibrationFormulation.objects
            .filter(calibration_run=run)
            .values_list("module_id", flat=True)
        )

        to_create: list[CalibrationFormulation] = []
        for module_name in to_be_added:
            m = get_cached_module_by_name(module_name)
            if m and m.id not in existing_module_ids:
                to_create.append(CalibrationFormulation(calibration_run=run, module=m))

        if to_create:
            CalibrationFormulation.objects.bulk_create(to_create, ignore_conflicts=True)

        # ------------------------------------------------------------
        # Persist parameters only for modules that actually returned them
        # ------------------------------------------------------------
        modules_with_params = [
            m for m in (module_metadata or {}).get("modules", [])
            if not m.get("error")
        ]

        if modules_with_params:
            update_parameters(run, {"modules": modules_with_params})

        # ------------------------------------------------------------
        # Persist module properties (replace-all), using the pre-validated plan
        # ------------------------------------------------------------
        apply_module_property_write_plan(run=run, plan=plan)  # type: ignore[arg-type]

        # ------------------------------------------------------------
        # Delete existing Sloth params for this run and re-add them if enabled
        # ------------------------------------------------------------
        CalibrationSlothParam.objects.filter(calibration_run=run).delete()
        if use_sloth:
            error_message = add_sloth_parameters(run, sloth_parameters, new_module_names)
            if error_message:
                logger.error(f"Error adding Sloth parameters: {error_message}")
                return ResponseError(error_message)

        # ------------------------------------------------------------
        # If formulation uses LSTM, we need to clear all irrelevant fields
        # ------------------------------------------------------------
        if have_lstm:
            # clear core CalibrationRun fields
            run.optimization = None
            run.objective_function = None
            run.streamflow_threshold = None
            run.peak_flow_threshold = None
            run.save_plot_iteration_frequency = None
            run.save_output_iteration = False

            # remove stop criteria
            CalibrationStopCriteria.objects.filter(calibration_run=run).delete()

            # No optimization inputs
            write_optimization_inputs(run, [])

        run.save()

    ngen_cal_input.ready_to_run(run)

    response = {
        'message': f'Calibration Job {run.id} updated',
        'calibration_run_id': run.id,
        'status': run.status.name,
    }
    if formulation_warnings:
        response['formulation_warnings'] = formulation_warnings
    if formulation_errors:
        response['formulation_errors'] = formulation_errors
    if eds_errors:
        response['eds_errors'] = eds_errors

    response_validator, error_response = validate_response(SaveFormulationResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)


def _parse_property_value(raw: str, data_type: str) -> tuple[str | int | float | bool, dict[str, Any] | None]:
    """
    Convert the UI's raw string `property_value` into a typed Python value based on ModuleProperty.data_type.

    UI contract:
    - The UI submits property_value as a string.
    - ModuleProperty.data_type (DataTypeEnum.*.value) determines how that string is interpreted.

    :param raw: Raw string value from the UI (e.g., "false", "123", "0.25", "ABC").
    :param data_type: DataTypeEnum value from ModuleProperty.data_type.
    :return: (typed_value, error_dict_or_none) where error_dict has shape {"message": "..."}.
    """
    # Keep parsing strict and deterministic. We only accept a limited boolean vocabulary.
    # Numeric parsing uses int()/float() and will error on invalid values.
    try:
        if data_type == DataTypeEnum.BOOLEAN.value:
            v = raw.strip().lower()  # normalize user input like " True " -> "true"
            if v in ("true", "1", "yes", "y", "on"):
                return True, None
            if v in ("false", "0", "no", "n", "off"):
                return False, None
            return False, {"message": f"Invalid boolean '{raw}' (expected true/false)."}

        if data_type == DataTypeEnum.INTEGER.value:
            # Reject floats/strings that are not valid base-10 integers.
            return int(raw), None

        if data_type == DataTypeEnum.DOUBLE.value:
            # Accept values that float() can parse (e.g., "1", "1.0", "1e-3").
            return float(raw), None

        if data_type == DataTypeEnum.STRING.value:
            # Preserve as-is; the DB column will store the literal string.
            return raw, None

        return raw, {"message": f"Unsupported data_type '{data_type}'."}

    except (ValueError, TypeError) as e:
        return raw, {"message": f"Invalid value '{raw}' for data_type '{data_type}': {e}"}


def _value_columns_for_type(value: str | int | float | bool, data_type: str) -> dict[str, Any]:
    """
    Map a typed Python value into the correct single value_* column for CalibrationModulePropertyValue.

    IMPORTANT:
    - The returned dict keys MUST match the column names in CalibrationModulePropertyValue:
        value_bool, value_int, value_double, value_str
    - Exactly one of these keys will be non-null.
    - This relies on CalibrationModulePropertyValue's DB check constraint enforcing exactly one non-null.

    :param value: Typed value produced by _parse_property_value().
    :param data_type: DataTypeEnum value from ModuleProperty.data_type.
    :return: Dict suitable for **cols when creating CalibrationModulePropertyValue(**cols).
    """
    # Start with all-null columns, then set exactly one.
    cols: dict[str, Any] = {
        "value_bool": None,
        "value_int": None,
        "value_double": None,
        "value_str": None
    }

    # Choose the destination column based on ModuleProperty.data_type (not Python type),
    if data_type == DataTypeEnum.BOOLEAN.value:
        cols["value_bool"] = bool(value)
    elif data_type == DataTypeEnum.INTEGER.value:
        cols["value_int"] = int(value)
    elif data_type == DataTypeEnum.DOUBLE.value:
        cols["value_double"] = float(value)
    elif data_type == DataTypeEnum.STRING.value:
        cols["value_str"] = str(value)
    else:
        # Should not happen if ModuleProperty.data_type is constrained, but keep a safe fallback.
        cols["value_str"] = str(value)

    return cols


def delete_unused_formulations(to_delete_modules: set[str], run: CalibrationRun) -> None:
    """
    Delete unused CalibrationFormulation rows (and their dependent parameters) for modules
    that are no longer part of the current formulation selection.

    This function performs database writes and must be called inside a write transaction.

    :param to_delete_modules: Set of module names to delete for the given run.
    :param run: CalibrationRun instance whose formulations will be pruned.
    :return: None.
    """
    formulations_to_delete_qs = CalibrationFormulation.objects.filter(
        calibration_run=run,
        module__name__in=to_delete_modules
    ).only("id")

    # Delete CalibrationParameters related to the formulations_to_delete
    CalibrationParameter.objects.filter(
        calibration_formulation__in=formulations_to_delete_qs
    ).delete()

    # Finally, delete the formulations
    formulations_to_delete_qs.delete()


formulation_validations: dict[str, Any] = {
    "formulation_rules": {
        "group_requirements": {
            "Glacier": {
                "expected_counts": [0, 1],
                "fatal": True
            },
            "Snowmelt": {
                "expected_counts": [0, 1],
                "fatal": False
            },
            "Evapotranspiration": {
                "expected_counts": [1],
                "fatal": False
            },
            "Rainfall Runoff": {
                "expected_counts": [1],
                "fatal": True
            },
            "Soil Moisture": {
                "expected_counts": [0, 2],
                "fatal": True
            },
            "Routing": {
                "expected_counts": [1],
                "fatal": True
            }
        },
        "module_dependencies": {
            "SMP": [
                {
                    "requires_any_of": ["Noah-OWP-Modular"],
                    "fatal": True
                }
            ],
            "SFT": [
                {
                    "requires_any_of": ["Noah-OWP-Modular"],
                    "fatal": True
                }
            ]
        }
    }
}


def split_routing_modules(
        module_names: set[str],
        cached_modules: dict[str, Any],
) -> tuple[set[str], set[str]]:
    """
    Split module names into (routing_modules, non_routing_modules) using cached module group membership.

    Assumes module_names are already validated and present in cached_modules.
    """
    routing: set[str] = set()
    non_routing: set[str] = set()

    for name in module_names:
        group_names = {g.name for g in cached_modules[name].groups.all()}
        if "Routing" in group_names:
            routing.add(name)
        else:
            non_routing.add(name)

    return routing, non_routing


def validate_formulation(module_names: set[str], geopackage_path: str | None, return_group_info: bool = False) \
        -> tuple[list[str], list[str], list[str]]:
    """
    Validate formulation rules based on group requirements and module dependencies.

    Uses cached modules/groups to avoid repeated DB hits.

    :param module_names: A set of module names to validate.
    :param geopackage_path: Path to geopackage file used if Topoflow is specified
    :param return_group_info: If true, then include a message about the groups in Info messages
    :return: A tuple of lists (fatal_errors, nonfatal_errors, info_messages).
             Each list contains validation messages of the corresponding severity.
             If there are no messages of a given severity, that list will be empty.
    """

    # Prepare containers for fatal vs. non-fatal vs. info messages
    fatal_errors: list[str] = []
    nonfatal_errors: list[str] = []
    info_messages: list[str] = []

    cached_modules = get_cached_modules_with_groups()

    # --- Special case: if LSTM is present, enforce LSTM-specific rules and skip the rest ---
    if "LSTM" in module_names:
        if len(module_names) > 2:
            # More than two modules with LSTM is not allowed
            fatal_errors.append("LSTM cannot be combined with more than one other module.")
            return fatal_errors, nonfatal_errors, info_messages

        if len(module_names) < 2:
            # LSTM alone (no other module) is not allowed
            fatal_errors.append("When LSTM is specified, exactly one other Routing module must be included.")
            return fatal_errors, nonfatal_errors, info_messages

        routing, non_routing = split_routing_modules(module_names - {"LSTM"}, cached_modules)

        if len(routing) != 1 or non_routing:
            other_names = sorted(module_names - {"LSTM"})
            fatal_errors.append(
                "When LSTM is specified, exactly one other Routing module must be included; "
                f"found: {', '.join(other_names)}"
            )
            return fatal_errors, nonfatal_errors, info_messages

        # Check for completeness
        check_completeness(module_names, fatal_errors, nonfatal_errors, info_messages)

        if not fatal_errors:
            info_messages.append('Formulation is Calibratable.')

            if return_group_info:
                # Add group summary
                msg = get_represented_groups_message(module_names)
                if msg:
                    info_messages.append(msg)
        else:
            fatal_errors.append('Formulation is not Calibratable.')

        return fatal_errors, nonfatal_errors, info_messages

    # --- End of LSTM special case. All further checks assume LSTM is NOT present. ---

    # Topoflow-Glacier composition rule:
    # Topoflow-Glacier cannot be specified by itself (routing modules don't count).
    # If Topoflow-Glacier is present, there must be at least 1 other non-routing module.
    if "Topoflow-Glacier" in module_names:
        _, non_routing = split_routing_modules(module_names - {"Topoflow-Glacier"}, cached_modules)
        if not non_routing:
            fatal_errors.append(
                "Topoflow-Glacier cannot be used by itself. When Topoflow-Glacier is specified, "
                "at least one additional non-Routing module must be included."
            )
            return fatal_errors, nonfatal_errors, info_messages

    # Perform checks for non-LSTM case
    modules_by_id = get_cached_modules_by_id()  # canonical cache
    modules_by_name = {m.name: m for m in modules_by_id.values()}  # lightweight derived view

    my_modules = [modules_by_name[name] for name in module_names if name in modules_by_name]

    # Count how many selected modules belong to each group
    group_defs = formulation_validations["formulation_rules"]["group_requirements"]
    group_counts = {grp_name: 0 for grp_name in group_defs}

    # Parse the groups for each module once and update the group counts
    for module in my_modules:
        for group in module.groups.all():
            if group.name in group_counts:  # Only count groups that are in the group_requirements
                group_counts[group.name] += 1

    # 1) Check module_dependencies
    dependency_defs = formulation_validations["formulation_rules"].get("module_dependencies", {})
    for module_name, rules_list in dependency_defs.items():

        if module_name not in module_names:
            continue

        for rule in rules_list:
            requires_any_of = rule.get("requires_any_of", [])

            if requires_any_of and not any(required_module in module_names for required_module in requires_any_of):
                if len(requires_any_of) == 1:
                    msg = f"{module_name} module requires {requires_any_of[0]}"
                else:
                    msg = f"{module_name} module requires one of: {', '.join(requires_any_of)}"
                logger.warning(msg)
                if rule.get("fatal", True):
                    fatal_errors.append(msg)
                else:
                    nonfatal_errors.append(msg)

    # 2) Check group_requirements
    for group_name, group_rules in group_defs.items():
        expected_counts = group_rules.get("expected_counts", [])
        count = group_counts.get(group_name, 0)

        # Validate the count against expected_counts
        if count not in expected_counts:
            # Build the “1” vs “0 or 2” string
            expected_str = join_with_or([str(c) for c in expected_counts])
            # Choose singular if exactly [1], otherwise plural
            word = "module" if len(expected_counts) == 1 and expected_counts[0] == 1 else "modules"

            # Find which modules from this group are currently specified
            modules_in_group = sorted(
                m.name for m in my_modules
                if any(g.name == group_name for g in m.groups.all())
            )

            msg = f"{group_name} group is expected to have {expected_str} {word}, but it currently has {count}"

            if count > 0:
                msg += f" ({', '.join(modules_in_group)})"

            msg += "."

            if count > 1 and 'Noah-OWP-Modular' in module_names:
                msg += f" Noah-OWP-Modular will not be used for {group_name}."

            logger.warning(msg)

            if group_rules.get("fatal", False):
                fatal_errors.append(msg)
            else:
                nonfatal_errors.append(msg)

    # 3) Check for completeness
    check_completeness(module_names, fatal_errors, nonfatal_errors, info_messages)

    # 4) Special case for Topoflow
    if 'Topoflow-Glacier' in module_names and geopackage_path:
        glacier_status = validate_topoflow_glacier(geopackage_path)
        if not glacier_status.get('result'):
            nonfatal_errors.append(glacier_status.get('message'))

    # 5) If no fatal errors, indicate that the formulation is Calibratable
    if not fatal_errors:
        info_messages.append('Formulation is Calibratable.')

        if return_group_info:
            # Add group summary
            msg = get_represented_groups_message(module_names)
            if msg:
                info_messages.append(msg)
    else:
        fatal_errors.append('Formulation is not Calibratable.')

    return fatal_errors, nonfatal_errors, info_messages


def get_represented_groups_message(module_names: set[str]) -> str | None:
    """
    Return a formatted message listing all groups represented by the given modules.
    """
    cached_modules = get_cached_modules_with_groups()

    represented_groups = set()
    for name in module_names:
        module = cached_modules.get(name)
        if not module:
            continue
        for g in module.groups.all():
            represented_groups.add(g.name)

    if not represented_groups:
        return None

    return "Groups represented by this formulation: " + ", ".join(sorted(represented_groups))


def check_completeness(module_names: set[str], fatal_errors: list[str], nonfatal_errors: list[str], info_messages: list[str]) -> None:
    """
    Check if the formulation is complete by ensuring all necessary modules are included.

    Uses canonical cached modules (ID→Module), then derives a name-based view in-memory.
    Avoids duplicate or inconsistent cache hydration.

    :param module_names: A set of module names to check for completeness.
    :param fatal_errors: List to append fatal errors.
    :param nonfatal_errors: List to append nonfatal errors.
    :param info_messages: List to append informational messages.
    :return: None.
    """
    modules_by_id = get_cached_modules_by_id()
    # lightweight derived view for name lookup
    modules_by_name = {m.name: m for m in modules_by_id.values()}

    # Get only the modules referenced in this formulation
    modules_included = [modules_by_name[name] for name in module_names if name in modules_by_name]

    # Get all output variable names from cacheable modules
    all_output_vars = {ov.name for m in modules_by_id.values() for ov in m.output_variables.all()}

    # Collect only the output variables produced by selected modules
    included_output_vars = {ov.name for m in modules_included for ov in m.output_variables.all()}

    missing_output_vars = sorted(all_output_vars - included_output_vars)
    produced_output_vars = sorted(included_output_vars)

    if missing_output_vars:
        nonfatal_errors.append('Formulation Incomplete. Not all NWM v3 Output Variables can be produced by the selected formulation.')
        nonfatal_errors.append('The following NWM v3 Output Variables cannot be produced:\n' + ", ".join(missing_output_vars))
    else:
        info_messages.append('Formulation Complete. All NWM v3 Output Variables can be produced by the selected formulation.')

    if produced_output_vars:
        info_messages.append('The following NWM v3 Output Variables can be produced:\n' + ", ".join(produced_output_vars))


def add_sloth_parameters(run: CalibrationRun, sloth_parameters: list[dict], module_names: set[str]) -> str | None:
    """
    Add Sloth parameters to a calibration run, validating module associations.

    This function performs database writes and must be called inside a write transaction.

    :param run: The calibration run instance.
    :param sloth_parameters: A list of dictionaries containing Sloth parameter data.
    :param module_names: A set of module names included in the run.
    :return: An error message if a Sloth parameter is invalid; otherwise, None.
    """
    sloth_param_objects = []
    if sloth_parameters is not None:
        for s in sloth_parameters:
            module = get_cached_module_by_name(s['maps_to_module'])
            if not module or module.name not in module_names:
                return f"Sloth parameter '{s['param_name']}' has an invalid module - '{s['maps_to_module']}'.  This module has not been added to this run"

            sloth_param_objects.append(
                CalibrationSlothParam(
                    calibration_run=run, param_name=s['param_name'], param_count=s['param_count'],
                    param_type=s['param_type'], param_units=s['param_units'], param_location=s['param_location'],
                    param_value=s['param_value'], maps_to_module=module, maps_to_variable_name=s['maps_to_variable_name']
                )
            )

        CalibrationSlothParam.objects.bulk_create(sloth_param_objects)

    return None


def build_module_property_write_plan(
        *,
        module_properties: list[dict[str, Any]] | None,
        user,
        modules_by_name: dict[str, Any],
        allowed_module_names: set[str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """
    Validate + normalize the incoming module_properties payload into a "write plan".

    This does NOT write anything to the database. It:
      - Validates module names exist (via modules_by_name).
      - Optionally validates each payload module is within allowed_module_names (selected modules).
      - Validates each property exists for the given module using cached ModuleProperty definitions
        (get_cached_module_properties()).
      - Parses the incoming string value according to ModuleProperty.data_type.
      - Prepares the exact (value_bool/value_int/value_double/value_str) columns to write.
      - Returns user-friendly error messages that include module *names* (not ids).

    Replace-all semantics:
      - If module_properties is omitted or empty, that means "clear all saved values for this run".
        The plan will be returned with {"clear_all": True, "items": []}.

    Why this exists:
      - save_formulation_tab wants to validate module_properties BEFORE doing any deletes/inserts
        (replace-all semantics), so a bad payload cannot wipe out existing saved values.
      - import_calibration_run_data can reuse this function to validate without depending on the UI.

    :param module_properties: List of dicts from the request serializer, or None if omitted.
                              Example item:
                                  {"module": "CFE-S", "property_name": "AET Rootzone", "property_value": "true"}
    :param user: User object used for created_by/updated_by in the plan items (written later).
    :param modules_by_name: Cached Module ORM instances keyed by module.name.
    :param allowed_module_names: If provided, module_properties entries must reference only these module names
                                 (typically the current selected modules list).
    :return: A tuple (plan, error_obj):
             - On success: (plan, None) where plan is:
                   {
                     "clear_all": bool,
                     "items": [
                       {
                         "module_name": str,
                         "module_id": int,
                         "module_property_id": int,
                         "cols": {"value_bool": ..., "value_int": ..., "value_double": ..., "value_str": ...},
                         "created_by": user,
                         "updated_by": user,
                       },
                       ...
                     ]
                   }
             - On failure: (None, error_obj) where error_obj is suitable for ResponseError(...):
                   {"module_properties": ["[0] ...", "[1] ...", ...]}
    """
    # Replace-all semantics: empty payload means "clear all saved values for this run".
    if not module_properties:
        return {"clear_all": True, "items": []}, None

    errors: list[str] = []

    # Collect module ids referenced by the payload so we can query ModuleProperty in a single DB hit.
    requested_module_ids: set[int] = set()

    # Keep module_name in the resolved items so error messages are readable.
    # (module_name, module_id, prop_name, raw_value)
    resolved_items: list[tuple[str, int, str, str]] = []

    for i, p in enumerate(module_properties):
        module_name = p["module"]
        if allowed_module_names is not None and module_name not in allowed_module_names:
            errors.append(f"[{i}] Module '{module_name}' is not in selected modules")
            continue

        prop_name = p["property_name"]
        raw_value = p["property_value"]

        module_obj = modules_by_name.get(module_name)
        if not module_obj:
            errors.append(f"[{i}] Unknown module '{module_name}'")
            continue

        requested_module_ids.add(module_obj.id)
        resolved_items.append((module_name, module_obj.id, prop_name, raw_value))

    if errors:
        return None, {"module_properties": errors}

    # Pull ModuleProperty defs from cache (filter in-memory)
    all_props = get_cached_module_properties()
    prop_def_map = {
        (p.module_id, p.name): p
        for p in all_props
        if p.module_id in requested_module_ids
    }

    plan_items: list[dict[str, Any]] = []

    for i, (module_name, module_id, prop_name, raw_value) in enumerate(resolved_items):
        prop_def = prop_def_map.get((module_id, prop_name))
        if not prop_def:
            errors.append(f"[{i}] Unknown property '{prop_name}' for module '{module_name}'")
            continue

        # Parse the wire-format string into a typed Python value using the property data_type.
        typed_value, parse_err = _parse_property_value(raw_value, prop_def.data_type)
        if parse_err:
            errors.append(f"[{i}] module.{module_name}.{prop_name}: {parse_err['message']}")
            continue

        # Convert typed value into exactly one DB column (value_bool/value_int/value_double/value_str).
        cols = _value_columns_for_type(typed_value, prop_def.data_type)

        plan_items.append({
            "module_name": module_name,
            "module_id": module_id,
            "module_property_id": prop_def.id,
            "cols": cols,
            "created_by": user,
            "updated_by": user,
        })

    if errors:
        return None, {"module_properties": errors}

    return {"clear_all": False, "items": plan_items}, None


def apply_module_property_write_plan(*, run: CalibrationRun, plan: dict[str, Any]) -> None:
    """
    Apply a validated module-property "write plan" to the database using replace-all semantics.

    Replace-all semantics are intentional:
      - The incoming payload is treated as the full desired set of saved module properties for the run.
      - We delete all existing CalibrationModulePropertyValue rows for the run, then insert the new set.

    Safety properties:
      - This function assumes the plan has already been validated (typically by build_module_property_write_plan).
      - If a plan item refers to a module that does NOT have a formulation for this run, we raise an
        exception (fail hard) to avoid silently dropping values.

    Transaction expectations:
      - Call this inside transaction.atomic() in the caller.
      - Because we delete then insert, you want the whole operation to be atomic.

    Args:
        run: CalibrationRun whose formulation-scoped property values are being replaced.
        plan: The plan returned by build_module_property_write_plan().

    Returns:
        None. Raises ValueError if the plan cannot be applied safely.
    """
    # Replace-all: clear current values first. Caller should ensure this is in an atomic transaction.
    CalibrationModulePropertyValue.objects.filter(
        calibration_formulation__calibration_run=run
    ).delete()

    # "clear_all" means the caller intentionally sent an empty payload.
    if plan.get("clear_all"):
        return

    items: list[dict[str, Any]] = plan.get("items") or []
    if not items:
        return

    # Build module_id -> formulation_id for this run so we can write by FK ids without extra queries per item.
    formulations = list(
        CalibrationFormulation.objects
        .filter(calibration_run=run)
        .only("id", "module_id")
    )
    formulation_id_by_module_id = {f.module_id: f.id for f in formulations}

    rows: list[CalibrationModulePropertyValue] = []
    errors: list[str] = []

    for i, item in enumerate(items):
        module_id = item["module_id"]
        module_name = item.get("module_name") or f"module_id={module_id}"

        # A property value must attach to a concrete formulation row (run + module).
        formulation_id = formulation_id_by_module_id.get(module_id)
        if not formulation_id:
            errors.append(f"[{i}] No formulation found for module '{module_name}' in run {run.id}")
            continue

        cols = item["cols"]

        # Construct the row using *_id fields to avoid ORM hydration overhead.
        rows.append(
            CalibrationModulePropertyValue(
                calibration_formulation_id=formulation_id,
                module_property_id=item["module_property_id"],
                created_by=item.get("created_by"),
                updated_by=item.get("updated_by"),
                **cols,
            )
        )

    if errors:
        raise ValueError("apply_module_property_write_plan failed: " + "; ".join(errors))

    # Single bulk insert for performance.
    CalibrationModulePropertyValue.objects.bulk_create(rows, ignore_conflicts=False)
