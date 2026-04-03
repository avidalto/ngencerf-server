import json
import logging
import math
from collections import defaultdict
from numbers import Real

from django.db.models import F, QuerySet
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import StatusEnum, ValidationMetricPeriod
from calibration.models import Iteration, NWMRetrospectiveMetrics, CalibrationRun, ValidationRun, IterationParameter, IterationMetric
from calibration.util.calibration_validators import CalibrationRunIdSerializer, \
    ErrorResponseSerializer, GetCalibrationDataByIterationResponseSerializer
from calibration.views.called_from import get_caller_name
from calibration.views.common import get_calibration_run, handle_exceptions, validate_response, validate_request, truncate_large_fields, \
    get_user_email, get_elapsed_str

logger = logging.getLogger(__name__)


def normalize_float(value):
    """
    Normalize numeric values for JSON serialization.

    Behavior:
    - None -> None
    - Any real numeric type (float, int, numpy floats/ints, Decimal):
        - Converted to float
        - If non-finite (NaN, +inf, -inf) -> None
        - Otherwise -> finite float
    - All non-numeric values (str, dict, list, etc.) pass through unchanged

    Rationale:
    - PostgreSQL can store NaN/±Infinity in float columns.
    - JSON cannot represent NaN/±Infinity.
    - This function is applied ONLY at API response construction time,
      not at DB write time, to preserve raw numeric fidelity in storage
      while guaranteeing JSON-safe output.
    """

    # Preserve None as-is
    if value is None:
        return None

    # bool is a subclass of int; don't treat it as numeric here
    if isinstance(value, Real) and not isinstance(value, bool):
        f = float(value)
        return f if math.isfinite(f) else None

    # All other values pass through unchanged
    return value


@extend_schema(
    request=CalibrationRunIdSerializer,
    responses={
        200: GetCalibrationDataByIterationResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Retrieve metrics and parameters by iteration for a specific calibration run"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_calibration_data_by_iteration(request: Request) -> Response:
    """
    Retrieves calibration data by iteration for a specific calibration run.

    - Handles user authentication and validation.
    - Fetches retrospective metrics and iteration data.
    - Constructs a response containing iterations, parameters, metrics, and validation information.

    :param request: The HTTP request object containing calibration run data.
    :return: JSON response with calibration data or error information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunIdSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')

    run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.DONE])
    if error_return:
        return error_return

    # Fetch retrospective metrics data associated with the calibration run
    nwm_retrospective_data = list(
        NWMRetrospectiveMetrics.objects
        .filter(period=ValidationMetricPeriod.valid.value, calibration_run=run)
        .select_related('metric')
        .annotate(
            metric_name=F('metric__name'),
            metric_display_name=F('metric__display_name'),
        )
        .values('metric_name', 'metric_display_name', 'metric_value')
    )

    for row in nwm_retrospective_data:
        row['metric_value'] = normalize_float(row.get('metric_value'))
    retrospective_data = [{'name': 'NWM 3.0', 'data': nwm_retrospective_data}]

    iterations = list(get_iterations_for_calibration_job(run))
    iteration_ids = [it.id for it in iterations]

    # Prefetch validation runs for all iterations
    validation_runs = (
        ValidationRun.objects
        .filter(
            iteration_id__in=iteration_ids,
            status__in=[
                StatusEnum.DONE.db_instance,
                StatusEnum.RUNNING.db_instance,
                StatusEnum.SUBMITTED.db_instance,
            ],
        )
        .only('id', 'iteration_id')
    )

    validation_runs_by_iteration = {vr.iteration_id: vr for vr in validation_runs}

    params_by_iter = defaultdict(list)
    for p in (
            IterationParameter.objects
                    .filter(iteration_id__in=iteration_ids)
                    .select_related('calibration_parameter')
                    .values(
                'iteration_id',
                'calibration_parameter__name',
                'tuned_value',
            )
    ):
        params_by_iter[p['iteration_id']].append({
            'parameter_name': p['calibration_parameter__name'],
            'parameter_value': p['tuned_value']
        })

    metrics_by_iter = defaultdict(list)
    for m in (
            IterationMetric.objects
                    .select_related('metric')
                    .filter(iteration_id__in=iteration_ids)
                    .values(
                'iteration_id',
                'metric__name',
                'metric__display_name',
                'metric_value',
            )
    ):
        metrics_by_iter[m['iteration_id']].append({
            'metric_name': m['metric__name'],
            'metric_display_name': m['metric__display_name'],
            'metric_value': m['metric_value']
        })

    # Construct iteration data with parameters, metrics, and validation reference
    iteration_data = []
    for iteration in iterations:
        validation_run = validation_runs_by_iteration.get(iteration.id)

        raw_params = params_by_iter.get(iteration.id, [])
        raw_metrics = metrics_by_iter.get(iteration.id, [])

        iteration_element = {
            'iteration_num': iteration.iteration_num,
            'iteration_id': iteration.id,
            'worker_name': iteration.worker_name,
            'best_params': iteration.best_params,
            'objective_function_value': normalize_float(iteration.objective_function_value),
            'parameters': [
                {
                    'parameter_name': p['parameter_name'],
                    'parameter_value': normalize_float(p['parameter_value']),
                }
                for p in raw_params
            ],
            'metrics': [
                {
                    'metric_name': m['metric_name'],
                    'metric_display_name': m['metric_display_name'],
                    'metric_value': normalize_float(m['metric_value']),
                }
                for m in raw_metrics
            ],
        }

        if validation_run:
            iteration_element['validation_run_id'] = validation_run.id

        iteration_data.append(iteration_element)

    response = {
        'message': f'Calibration Job {run.id}, data retrieved',
        # Will be none for LSTM
        'objective_function_metric': run.objective_function.name if run.objective_function else None,
        'iteration_data': iteration_data,
        'retrospective_data': retrospective_data
    }

    response_validator, error_response = validate_response(
        GetCalibrationDataByIterationResponseSerializer,
        response,
        fields_to_truncate=['iteration_data'],
        max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["iteration_data"], max_length=10))}'
    )

    return Response(response_validator.data)


def get_iterations_for_calibration_job(calibration_run: CalibrationRun, worker_name: str | None = None) -> QuerySet[Iteration]:
    """
        Fetches iterations for the given calibration run.

        - Optionally filters by worker name.
        - Prefetches related parameters and metrics for optimized retrieval.

        :param calibration_run: The CalibrationRun instance to fetch iterations for.
        :param worker_name: Optional worker name to filter iterations.
        :return: QuerySet of Iteration objects associated with the calibration run.
        """
    queryset = Iteration.objects.filter(calibration_run=calibration_run)

    if worker_name:
        queryset = queryset.filter(worker_name=worker_name)

    return (
        queryset
        .select_related('calibration_run')
        .prefetch_related('iterationparameter_set__calibration_parameter',
                          'iterationmetric_set')
        .order_by('worker_name', 'iteration_num')
    )
