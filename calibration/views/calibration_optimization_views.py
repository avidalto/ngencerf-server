import json
import logging
from typing import Any

from django.db import transaction
from django.db.models import F
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiResponse
from rest_framework.decorators import api_view
from rest_framework.response import Response

from calibration.enums import OptimizationEnum, MetricEnum
from calibration.models import Optimization, CalibrationOptimizationInput, CalibrationStopCriteria, CalibrationRun
from calibration.util.caching import have_LSTM
from calibration.util.calibration_validators import LoadOptimizationResponseSerializer, \
    SaveOptimizationRequestSerializer, ErrorResponseSerializer, GenericResponseSerializer, EmptySerializer
from calibration.views import ngen_cal_input
from calibration.views.called_from import get_caller_name
from calibration.views.common import get_calibration_run, ResponseError, handle_exceptions, validate_response, validate_request, get_user_email, \
    get_elapsed_str

logger = logging.getLogger(__name__)


@extend_schema(
    request=EmptySerializer,
    responses={
        200: LoadOptimizationResponseSerializer,
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
    description="Load optimization tab data"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def load_optimization_tab(request) -> Response:
    """
    Handles loading optimization data for a calibration run.

    Validates the request, retrieves calibration run information, metrics, and optimizations,
    and returns a structured response.

    :param request: The incoming HTTP request.
    :return: JSON response with calibration run optimization data.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()

    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(EmptySerializer, data)
    if error_return:
        return error_return

    metrics = MetricEnum.get_choices_with_fields(
        fields=['name', 'display_name', 'categorical', 'event_based'],
        extra_filter={'objective_function': True}
    )

    optimization_list = OptimizationEnum.get_choices_with_fields(
        fields=['name', 'description', 'is_active']
    )
    for o in optimization_list:
        item = OptimizationEnum.get_instance(o['name'])
        o['inputs'] = list(
            item.inputs.values(
                'name', 'description', 'data_type',
                'default_value', 'min', 'max', 'is_active'
            )
        )

    response = {
        'metrics': metrics,
        'optimizations': optimization_list
    }

    response = {key: value for key, value in response.items() if value not in [None, '', [], {}]}

    response_validator, error_response = validate_response(LoadOptimizationResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)


def get_user_optimization(run: CalibrationRun) -> tuple[str | None, list[dict[str, Any]]]:
    """
    Retrieve the selected optimization and its inputs for a calibration run.

    :param run: The CalibrationRun instance.
    :return: A tuple with the optimization name (or None) and a list of input dicts
             containing input name and value.
    """
    if run.optimization:
        optimization = run.optimization.name
        optimization_inputs = list(
            CalibrationOptimizationInput.objects
            .filter(calibration_run=run)
            .select_related('optimization_input')
            .order_by('optimization_input__name')
            .values('value', name=F('optimization_input__name'))
        )
    else:
        optimization = None
        optimization_inputs = []

    return optimization, optimization_inputs


# noinspection PyUnusedLocal
@extend_schema(
    request=SaveOptimizationRequestSerializer,
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
    description="Save optimization tab data"
)
@api_view(['POST'])
@handle_exceptions
def save_optimization_tab(request) -> Response:
    """
    Saves optimization configuration for a calibration run.

    Validates and saves the user-selected optimization, inputs, and thresholds for the calibration run.

    :param request: The incoming HTTP request.
    :return: JSON response indicating the success of the save operation.
    """
    data = request.data

    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(SaveOptimizationRequestSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    optimization_name = validator.get('optimization')
    objective_function_name = validator.get('objective_function')
    streamflow_threshold = validator.get('streamflow_threshold')
    peak_flow_threshold = validator.get('peak_flow_threshold')
    optimization_inputs = validator.get('optimization_inputs')
    stop_criteria = validator.get('stop_criteria')
    save_plot_iteration_frequency = validator.get('save_plot_iteration_frequency')
    save_output_iteration = validator.get('save_output_iteration')

    run, error_return = get_calibration_run(calibration_run_id, request.user)
    if error_return:
        return error_return
    assert run is not None

    if have_LSTM(run) and (optimization_name or objective_function_name or
                           streamflow_threshold is not None or peak_flow_threshold is not None or
                           optimization_inputs or stop_criteria is not None or
                           save_output_iteration or save_plot_iteration_frequency is not None):
        return ResponseError(
            "You cannot specify optimization_name, objective_function_name, streamflow_threshold, peak_flow_threshold, "
            "optimization_name, stop_criteria, save_output_iteration or save_plot_iteration_frequency when using LSTM")

    if optimization_inputs and not optimization_name:
        return ResponseError('Optimization inputs cannot be specified without an optimization name')

    prepared_inputs: list[CalibrationOptimizationInput] | None = None

    if optimization_name:
        optimization, prepared_inputs, error_message = validate_optimizations(run, optimization_name, optimization_inputs)
        if error_message:
            return ResponseError(error_message)
    else:
        # No optimization specified → clear optimization and inputs
        run.optimization = None

    error_message = validate_objective_function(run, objective_function_name, streamflow_threshold, peak_flow_threshold)
    if error_message:
        return ResponseError(error_message)

    run.save_plot_iteration_frequency = save_plot_iteration_frequency

    # This field is not supported by UI, so if not specified, leave it alone
    if save_output_iteration is not None:
        run.save_output_iteration = save_output_iteration

    run.streamflow_threshold = streamflow_threshold
    run.peak_flow_threshold = peak_flow_threshold

    # keep_ids = {obj.optimization_input_id for obj in prepared_inputs} if prepared_inputs else set()
    with transaction.atomic():
        if stop_criteria is not None:
            # I'm assuming for now that there is just one CalibrationStopCriteria for this run, but that might change in the future
            CalibrationStopCriteria.objects.update_or_create(calibration_run=run, defaults={"value": stop_criteria})

        write_optimization_inputs(run, prepared_inputs)

        run.save()

    ngen_cal_input.ready_to_run(run)

    response = {'message': f'Calibration Job {run.id} updated', 'calibration_run_id': run.id, 'status': run.status.name}

    response_validator, error_response = validate_response(GenericResponseSerializer, response)
    if error_response:
        return error_response
    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}')
    return Response(response_validator.data)


def validate_optimizations(
        run: CalibrationRun,
        optimization_name: str,
        optimization_inputs: list[dict[str, Any]]
) -> tuple[Optimization | None, list[CalibrationOptimizationInput] | None, str | None]:
    """
    Validate and prepare optimization inputs for a calibration run.

    :param run: The CalibrationRun instance.
    :param optimization_name: Name of the optimization to apply.
    :param optimization_inputs: List of inputs for the optimization.
    :return: Tuple with the Optimization instance, a list of prepared
             CalibrationOptimizationInput objects, and an error message if validation fails.
    """
    optimization = OptimizationEnum.get_instance(optimization_name)
    run.optimization = optimization

    prepared_inputs: list[CalibrationOptimizationInput] = []

    if optimization_inputs:
        # Retrieve cached optimization inputs
        opt = OptimizationEnum.get_instance(optimization_name)
        valid_inputs_dict = {
            i['name']: i
            for i in opt.inputs.values(
                'name', 'description', 'data_type',
                'default_value', 'min', 'max', 'id', 'is_active'
            )
        }

        for o in optimization_inputs:
            name = o['name']
            value = o['value']
            optimization_input_data = valid_inputs_dict.get(name)
            if not optimization_input_data:
                return None, None, f"'{name}' is not a valid parameter input for '{optimization_name}'"

            # Safely retrieve min and max values
            min_value = optimization_input_data['min']
            max_value = optimization_input_data['max']

            # Convert types if necessary for validation
            if optimization_input_data['data_type'] != 'double':
                # noinspection PyTypeChecker
                min_value = int(min_value) if min_value is not None else None
                # noinspection PyTypeChecker
                max_value = int(max_value) if max_value is not None else None
                value = int(value)

            # Validate against min and max
            if min_value is not None and value < min_value:
                return None, None, f"'{name}' value ({value}) is below the minimum allowed ({min_value})"
            if max_value is not None and value > max_value:
                return None, None, f"'{name}' value ({value}) is above the maximum allowed ({max_value})"

            # Append to the list for bulk creation with the original `OptimizationInput` id
            prepared_inputs.append(
                CalibrationOptimizationInput(
                    optimization_input_id=optimization_input_data['id'],
                    calibration_run=run,
                    value=value
                )
            )

    return optimization, prepared_inputs, None


def validate_objective_function(run: CalibrationRun,
                                objective_function_name: str,
                                streamflow_threshold: float | None,
                                peak_flow_threshold: float | None) -> str | None:
    """
    Validates and assigns the objective function to a calibration run.

    :param run: The CalibrationRun instance.
    :param objective_function_name: Name of the objective function to apply.
    :param streamflow_threshold: Streamflow threshold value.
    :param peak_flow_threshold: Peak flow threshold value.
    :return: Error message if validation fails, otherwise None.
    """
    if objective_function_name:

        # Fetch the metric from cache
        try:
            objective_function = MetricEnum.get_instance(objective_function_name.lower())
        except ValueError:
            return f"Invalid metric specified for objective function - '{objective_function_name}'"
        if not objective_function.objective_function:
            return f"{objective_function_name} cannot be used as an objective function"

        run.objective_function = objective_function

        if objective_function.categorical:
            if not streamflow_threshold:
                return "Streamflow threshold must be specified for a categorical function"
            run.streamflow_threshold = streamflow_threshold

        if objective_function.event_based:
            if not peak_flow_threshold:
                return "Peak flow threshold must be specified for an event-based function"
            run.peak_flow_threshold = peak_flow_threshold

    return None


def write_optimization_inputs(run: CalibrationRun, prepared_inputs: list[CalibrationOptimizationInput] | None) -> None:
    """
    Write or update optimization input records for a calibration run.

    This function synchronizes the database state of `CalibrationOptimizationInput`
    entries for the given run with the provided validated inputs:
      - Deletes any existing inputs not present in `prepared_inputs`.
      - Inserts or updates the provided inputs.
      - If `prepared_inputs` is empty or None, removes all existing inputs for the run.

    This function does not manage transactions; callers modifying multiple related
    models should wrap the operation inside a `transaction.atomic()` block.

    :param run: The CalibrationRun instance whose optimization inputs are being updated.
    :param prepared_inputs: A list of prepared `CalibrationOptimizationInput` objects,
                            typically produced by `validate_optimizations()`.
    :return: None
    """
    # If there are no inputs, this means the run should have none — delete and exit.
    if not prepared_inputs:
        CalibrationOptimizationInput.objects.filter(calibration_run=run).delete()
        return

    keep_ids = {obj.optimization_input_id for obj in prepared_inputs}

    (CalibrationOptimizationInput.objects
     .filter(calibration_run=run)
     .exclude(optimization_input_id__in=keep_ids)
     .delete())

    # Upsert the remaining/new ones in bulk:
    # - update existing rows' value
    # - insert new rows
    #
    CalibrationOptimizationInput.objects.bulk_create(
        prepared_inputs,
        update_conflicts=True,
        update_fields=['value'],
        unique_fields=['calibration_run', 'optimization_input'],
    )
