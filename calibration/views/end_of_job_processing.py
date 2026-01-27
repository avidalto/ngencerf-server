import csv
import logging
import math
import os
import traceback
from collections import deque
from datetime import timedelta
from itertools import groupby
from operator import attrgetter

import pandas as pd
from django.db import transaction
from django.utils.timezone import now

from calibration.enums import OptimizationEnum, ValidationMetricPeriod, ValidationType, MetricEnum
from calibration.enums_vanilla import SecondaryDataEnum
from calibration.models import Iteration, CalibrationRun, IterationMetric, IterationParameter, CalibrationParameter, ValidationRun, \
    PerformanceMetrics, ValidationMetrics, NWMRetrospectiveMetrics, IterationResult, ColdStartRun, ForecastRun, \
    VerificationRun
from calibration.models.base_run import BaseRun
from calibration.util.caching import have_LSTM
from calibration.util.ngen_locations import get_realization_file_path, get_metrics_iteration_file, \
    get_objective_log_best_file, get_calibration_worker_path, get_global_best_params_file, get_validation_metrics_valid_control_file, \
    get_validation_metrics_valid_best_file, get_validation_metrics_valid_iteration_file, \
    get_validation_performance_file, get_calibration_performance_file, get_validation_metrics_nwm_retrospective_file, get_output_iteration_csv, \
    get_validation_special_performance_file, get_forecast_performance_file, get_verification_performance_file, \
    get_params_iteration_file, get_cold_start_performance_file
from calibration.views.calibration_secondary_data_views import generate_secondary_ts_data
from calibration.views.common import CerfException, get_job_description, find_validation_worker_with_matching_id

logger = logging.getLogger(__name__)

BULK_CREATE_BATCH_SIZE = 1000  # Define a reasonable batch size


def sanitize_metric_value(value) -> float | None:
    """
    Ensure metric values are JSON-safe later by preventing NaN/±Inf from ever
    being stored in the DB. Return None for non-finite values.
    """
    if value is None:
        return None

    try:
        f = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(f):
        return None

    return f


def read_validation_output(validation_run: ValidationRun, failed_so_far: bool) -> None:
    """
    Processes the output of a validation run by identifying the correct worker,
    retrieving performance metrics, and updating the validation run's attributes.

    :param validation_run: The ValidationRun instance.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    """
    job_description = get_job_description(validation_run)

    logger.info(f"Processing output for {job_description}, status: {validation_run.status}")

    # --- Moved this read-only query OUTSIDE the transaction to reduce lock contention ---
    validation_type = ValidationType(validation_run.validation_type)
    iteration = validation_run.iteration if validation_type == ValidationType.VALID_ITERATION else None

    with transaction.atomic():
        # Identify the matching worker based on validation type
        matching_worker = find_validation_worker_with_matching_id(
            validation_run,
            worker_name=iteration.worker_name if validation_type == ValidationType.VALID_ITERATION else None,
            iteration_num=iteration.iteration_num if validation_type == ValidationType.VALID_ITERATION else None,
        )
        validation_run.validation_worker_name = matching_worker
        validation_run.save(update_fields=['validation_worker_name'])

        performance_metrics_file = (
            get_validation_performance_file(validation_run.calibration_run, iteration.worker_name, iteration.iteration_num)
            if validation_type == ValidationType.VALID_ITERATION
            else get_validation_special_performance_file(validation_run.calibration_run, validation_type)
        )

        create_performance_metrics(validation_run, performance_metrics_file)

        if not failed_so_far:
            process_validation_for_validation_run(validation_run)

    logger.info(f"End of processing output for {job_description}")


def read_calibration_output(calibration_run: CalibrationRun, failed_so_far: bool) -> None:
    """
    Process the output of a CalibrationRun. This function handles reading and
    processing the worker directories and their iteration files.

    :param calibration_run: The CalibrationRun instance whose output is to be processed.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    """
    job_description = get_job_description(calibration_run)

    logger.info(f"Processing output for {job_description}, status={calibration_run.status}")

    already_processed = IterationMetric.objects.filter(
        iteration__calibration_run=calibration_run
    ).exists()

    if already_processed:
        raise CerfException(f"End of job processing has already been completed for {job_description}")

    with transaction.atomic():
        performance_metrics_file = get_calibration_performance_file(calibration_run)
        create_performance_metrics(calibration_run, performance_metrics_file)
        calibration_run.save(update_fields=['performance_metrics'])

        if not failed_so_far:
            # Set the realization file path for the run
            calibration_run.realization_file_path = get_realization_file_path(calibration_run)
            calibration_run.save(update_fields=['realization_file_path'])

            process_iterations_for_all_workers(calibration_run)

    logger.info(f"End of processing output for {job_description}")


def read_cold_start_output(run: ColdStartRun, _failed_so_far: bool) -> None:
    """
    Processes the output of a forecast run by parsing performance metrics.

    :param run: The ColdStartRun instance.
    :param _failed_so_far: Indicates whether the job has failed up to this point.
    """

    job_description = get_job_description(run)

    logger.info(f"Processing output for {job_description}, status={run.status}")
    with transaction.atomic():
        create_performance_metrics(run, get_cold_start_performance_file(run))

    # No other processing needed

    logger.info(f"End of processing output for {job_description}")


def read_forecast_output(run: ForecastRun, _failed_so_far: bool) -> None:
    """
    Processes the output of a forecast run by parsing performance metrics.

    :param run: The ForecastRun instance.
    :param _failed_so_far: Indicates whether the job has failed up to this point.
    """

    job_description = get_job_description(run)

    logger.info(f"Processing output for {job_description}, status={run.status}")
    with transaction.atomic():
        create_performance_metrics(run, get_forecast_performance_file(run))

    # No other processing needed

    logger.info(f"End of processing output for {job_description}")


def read_verification_output(run: VerificationRun, _failed_so_far: bool) -> None:
    """
    Processes the output of a verification run by parsing performance metrics.

    :param run: The VerificationRun instance.
    :param _failed_so_far: Indicates whether the job has failed up to this point.
    """

    job_description = get_job_description(run)

    logger.info(f"Processing output for {job_description}, status={run.status}")
    with transaction.atomic():
        performance_metrics_file = (
            get_verification_performance_file(run)
        )

        create_performance_metrics(run, performance_metrics_file)

    # No other processing needed

    logger.info(f"End of processing output for {job_description}")


def create_performance_metrics(run: BaseRun, performance_metrics_file: str) -> None:
    """
    Parses performance metrics from a file and updates the run with the metrics.

    :param run: The run instance (CalibrationRun, ValidationRun, or similar).
    :param performance_metrics_file: Path to the performance metrics file.
    :return: None
    """
    performance_metrics = parse_performance_metrics(performance_metrics_file)

    if not performance_metrics:
        # Fallback to calculate run_time manually
        run_time = now() - run.run_start
        performance_metrics = PerformanceMetrics.objects.create(run_time=run_time)

    run.performance_metrics = performance_metrics
    run.save(update_fields=['performance_metrics'])


def process_validation_metrics(run: ValidationRun | CalibrationRun, metrics_file: str, expected_run_type: str) -> None:
    """
    Generic function to process validation or calibration metrics from a CSV file and create corresponding Metric objects.

    :param run: The ValidationRun or CalibrationRun instance.
    :param metrics_file: The file path of the metrics CSV file.
    :param expected_run_type: The expected run type to validate.
    :return: None
    """

    job_description = get_job_description(run)
    logger.info(f"Processing '{metrics_file}' for {job_description}")

    # Check if the file exists
    if not os.path.isfile(metrics_file):
        logger.error(f'{metrics_file} does not exist')
        return

    # Read the metrics file using pandas
    metrics_df = pd.read_csv(metrics_file)

    metrics_to_create = []  # List to accumulate metrics to be created

    # Determine the type of metric to create
    MetricModel = ValidationMetrics if isinstance(run, ValidationRun) else NWMRetrospectiveMetrics

    # Loop over each row in the metrics file
    for _, row in metrics_df.iterrows():
        # Extract the run type and period fields
        run_type = str(row['run']).strip()
        if run_type != expected_run_type:
            logger.info(f'Unexpected run_type in {metrics_file} - {run_type}')

        period = str(row['period']).strip()
        if period not in ValidationMetricPeriod.get_names():
            logger.info(f'Unexpected period in {metrics_file} - {period}')

        # Extract the metrics starting from the third column onwards
        metrics_row = row[2:]

        # For each metric in the row, create or update the relevant Metric model
        for metric_name, value in metrics_row.items():
            # Perform case-insensitive lookup for the metric
            metric = MetricEnum.get_instance(str(metric_name))
            if not metric:
                raise CerfException(f"Could not find metric '{metric_name}' in MetricEnum")

            metric_value = sanitize_metric_value(value)

            # Create the Metric object (ValidationMetrics or NWMRetrospectiveMetrics)
            metric_obj = MetricModel(
                metric=metric,
                run_type=run_type,
                period=period,
                metric_value=metric_value,
                **({'validation_run': run} if isinstance(run, ValidationRun) else {'calibration_run': run})
            )
            logger.debug(
                f'{job_description}, type: {expected_run_type}: Creating {MetricModel.__name__} metric for Period: {period}, {metric_name} with value {metric_value}'
            )

            metrics_to_create.append(metric_obj)

    # Bulk create the metrics in the database
    if metrics_to_create:
        MetricModel.objects.bulk_create(metrics_to_create, batch_size=BULK_CREATE_BATCH_SIZE)


def process_validation_for_validation_run(validation_run: ValidationRun) -> None:
    """
    Read the file that is created by the Validation run for the specific iteration.
    Processes the metrics and updates the corresponding ValidationMetric entries.

    :param validation_run: The ValidationRun instance.
    :return: None
    """
    job_description = get_job_description(validation_run)

    metrics_file = None
    expected_run_type = None
    worker_name = validation_run.worker_name
    iteration_num = validation_run.iteration_num

    if validation_run.validation_type == ValidationType.VALID_ITERATION.value:
        metrics_file = get_validation_metrics_valid_iteration_file(validation_run.calibration_run, worker_name, iteration_num)
        expected_run_type = f'valid_{worker_name}_iter{iteration_num}'
    elif validation_run.validation_type == ValidationType.VALID_CONTROL.value:
        metrics_file = get_validation_metrics_valid_control_file(validation_run.calibration_run)
        expected_run_type = ValidationType.VALID_CONTROL.value
    elif validation_run.validation_type == ValidationType.VALID_BEST.value:
        metrics_file = get_validation_metrics_valid_best_file(validation_run.calibration_run)
        expected_run_type = ValidationType.VALID_BEST.value

    already_done = ValidationMetrics.objects.filter(
        validation_run=validation_run, run_type=expected_run_type
    ).exists()

    if already_done:
        raise CerfException(f"End of job processing has already been completed for {job_description}")

    process_validation_metrics(
        run=validation_run,
        metrics_file=metrics_file,
        expected_run_type=expected_run_type
    )

    if validation_run.validation_type == ValidationType.VALID_BEST.value:
        logger.info("Processing nwm retrospective data")

        # NWM Retrospective data is processed as part of Validation Best, but we save it in the Calibration Run
        metrics_file = get_validation_metrics_nwm_retrospective_file(validation_run.calibration_run)
        expected_run_type = 'nwm_retro'

        process_validation_metrics(
            run=validation_run.calibration_run,
            metrics_file=metrics_file,
            expected_run_type=expected_run_type
        )

    logger.info('Generating SWE timeseries data')
    try:
        generate_secondary_ts_data(validation_run, SecondaryDataEnum.SWE)
    except Exception as e:
        logger.error(f'Failed to generate SWE timeseries data: {e}')
        traceback.print_exc()

    logger.info('Generating Soil Moisture timeseries data')
    try:
        generate_secondary_ts_data(validation_run, SecondaryDataEnum.SOIL_MOISTURE)
    except Exception as e:
        logger.error(f'Failed to generate Soil Moisture timeseries data: {e}')
        traceback.print_exc()


def process_iterations_for_all_workers(calibration_run: CalibrationRun) -> None:
    """
    Process all Iteration objects for the workers of a given CalibrationRun.
    It uses prefetching to optimize database queries and processes the iterations
    for each worker based on their metrics and parameters.

    :param calibration_run: The CalibrationRun instance.
    """

    # ------------------------------------------------------------
    # This function operates at the *run* level.
    #
    # - Worker-level processing determines which iteration is best
    # - This function enforces run-level invariants:
    #     * Best-params dictionary loading
    #     * Calling per-worker processing
    #     * Verifying that exactly one best iteration exists
    # ------------------------------------------------------------
    # Compute best_params_dict once, outside the loop
    best_params_dict: dict[str, float | None] = {}

    have_LSTM_flag = have_LSTM(calibration_run)

    logger.debug(
        f"[BESTPARAMS CHECK] Run {calibration_run.id} using optimization={calibration_run.optimization}; "
        f"DDS={calibration_run.optimization == OptimizationEnum.DDS.db_instance}, "
        f"LSTM={have_LSTM_flag}"
    )

    # ----------------------------------------------------------------------
    # NON-DDS branch (GWO / PSO)
    # Only load global best params if NOT DDS AND NOT LSTM
    # ----------------------------------------------------------------------
    if calibration_run.optimization != OptimizationEnum.DDS.db_instance:
        if not have_LSTM_flag:
            global_best_params_file = get_global_best_params_file(calibration_run)

            logger.debug(
                f"[BESTPARAMS CHECK] Non-DDS + Non-LSTM: expecting global_best_params_file={global_best_params_file}"
            )

            if not os.path.isfile(global_best_params_file):
                logger.error(
                    f"[BESTPARAMS ERROR] global_best_params_file does NOT exist for run {calibration_run.id}: "
                    f"{global_best_params_file}"
                )
                raise CerfException(f"{global_best_params_file} does not exist")

            # File exists — load it
            logger.debug(
                f"[BESTPARAMS CHECK] Loading global best params file for run {calibration_run.id}"
            )
            # Read the global best parameters into a dictionary

            df = pd.read_csv(global_best_params_file, names=['value', 'name', 'model'], skiprows=1)

            best_params_dict = {
                str(name): sanitize_metric_value(value)
                for name, value in zip(df['name'], df['value'])
            }

            logger.debug(
                f"[BESTPARAMS CHECK] Loaded {len(best_params_dict)} best params for run {calibration_run.id}: "
                f"{list(best_params_dict.keys())[:10]}..."
            )

    # ----------------------------------------------------------------------
    # DDS branch — SHOULD NOT USE global best params
    # But we add diagnostics if the file exists
    # ----------------------------------------------------------------------
    else:
        global_best_params_file = get_global_best_params_file(calibration_run)
        if os.path.isfile(global_best_params_file):
            logger.error(
                f"[DDS WARNING] global_best_params_file EXISTS for DDS run {calibration_run.id}: "
                f"{global_best_params_file}. DDS should not produce this file."
            )
        else:
            logger.debug(
                f"[DDS OK] No global_best_params_file present for DDS run {calibration_run.id}"
            )

        # Explicitly ensure no params are used
        best_params_dict = {}

    # Query all Iteration objects for the calibration run and prefetch related metrics and parameters
    iterations = list(
        Iteration.objects
        .filter(calibration_run_id=calibration_run.id)
        .only("id", "worker_name", "iteration_num", "best_params")
        .order_by("worker_name", "iteration_num")
    )

    # Load all CalibrationParameter rows ONCE for this run
    job_parameters = CalibrationParameter.objects.filter(
        calibration_formulation__calibration_run_id=calibration_run.id
    )
    params_lookup = {p.name.lower(): p for p in job_parameters}

    # ----------------------------------------------------------------------
    # Group the iterations by worker and process them
    # ----------------------------------------------------------------------
    for worker_name, worker_iterations in groupby(iterations, key=attrgetter('worker_name')):
        process_iterations_for_a_worker(
            calibration_run,
            worker_name,
            list(worker_iterations),
            best_params_dict,
            have_LSTM_flag,
            params_lookup
        )

    # ----------------------------------------------------------------------
    # FINAL DIAGNOSTIC — Make sure exactly one best iteration exists
    # Applies to DDS, GWO/PSO, LSTM
    # ----------------------------------------------------------------------
    best_list = list(
        Iteration.objects
        .filter(calibration_run=calibration_run, best_params=True)
        .values_list("id", "iteration_num", "worker_name")
    )

    if len(best_list) == 0:
        logger.error(
            f"[BESTPARAMS ERROR] No best iteration found after processing for run {calibration_run.id}. "
            f"(optimization={calibration_run.optimization}, LSTM={have_LSTM_flag})"
        )
    elif len(best_list) > 1:
        logger.error(
            f"[BESTPARAMS ERROR] Multiple ({len(best_list)}) best iterations found for run {calibration_run.id}. "
            f"Expected exactly one. Details: {best_list}"
        )
    else:
        logger.debug(
            f"[BESTPARAMS OK] Exactly one best iteration found for run {calibration_run.id}: {best_list[0]}"
        )

    # ----------------------------------------------------------------------
    # Final check — only runs without LSTM require best iteration detection
    # ----------------------------------------------------------------------
    if not have_LSTM_flag:
        # Raise an error if no best iteration was found
        has_best = Iteration.objects.filter(
            calibration_run=calibration_run, best_params=True
        ).exists()

        if not has_best:
            raise CerfException(f"No best iteration was found for CalibrationRun {calibration_run.id}")


# Function to process iterations for a specific worker
def process_iterations_for_a_worker(
        calibration_run: CalibrationRun,
        worker_name: str,
        iterations: list[Iteration],
        best_params_dict: dict[str, float | None],
        have_LSTM_flag: bool,
        params_lookup: dict[str, CalibrationParameter]
) -> None:
    """
    Process all iterations for a specific worker in a CalibrationRun.
    It reads the metrics and parameters files for the worker and processes each
    iteration for metrics and parameters creation.

    :param calibration_run: The CalibrationRun instance.
    :param worker_name: The name of the worker. This is the middle part of the worker name.
                        Need to prefix with ngen_ and suffix with _worker.
    :param iterations: A list of Iteration objects for the worker.
    :param best_params_dict: Precomputed dictionary of best parameters for comparison.
    :param have_LSTM_flag: Flag to indicate whether this job has LSTM
    :param params_lookup: Mapping of lowercased parameter names to CalibrationParameter objects
    """

    logger.info(f"Processing iterations for {worker_name} for Calibration Job {calibration_run.id}")

    # ------------------------------------------------------------
    # Worker-level best-iteration determination happens here.
    #
    # Rules:
    #
    # DDS:
    #   - Best iteration is read from objective_log_best_file
    #
    # GWO / PSO:
    #   - Best iteration is determined by matching parameters
    #     against global_best_params_file
    #
    # LSTM:
    #   - Only one iteration exists and is always best
    #
    # This is the ONLY function that sets iteration.best_params.
    # ------------------------------------------------------------

    job_description = f"Calibration Job {calibration_run.id}, user: {calibration_run.owner.username}"

    # Get the worker's path
    worker_path = get_calibration_worker_path(calibration_run, worker_name)
    if not os.path.isdir(worker_path):
        raise CerfException(f"{worker_path} does not exist or is not a directory for CalibrationRun {calibration_run.id}")

    # Get the necessary files for metrics, parameters, and best objective function log
    metrics_iteration_file = get_metrics_iteration_file(calibration_run, worker_name)
    params_iteration_file = get_params_iteration_file(calibration_run, worker_name)
    # Contains the best for DDS
    objective_log_best_file = get_objective_log_best_file(calibration_run, worker_name)

    # Check if the files exist
    if not os.path.isfile(metrics_iteration_file):
        raise CerfException(f'{metrics_iteration_file} does not exist for CalibrationRun {calibration_run.id}')
    if not os.path.isfile(params_iteration_file) and not have_LSTM_flag:
        raise CerfException(f'{params_iteration_file} does not exist for CalibrationRun {calibration_run.id}')

    # Check for the best iteration based on optimization type (DDS, GWO, PSO)
    best_iteration_for_worker = -1
    if calibration_run.optimization == OptimizationEnum.DDS.db_instance:
        if not os.path.isfile(objective_log_best_file):
            raise CerfException(f'{objective_log_best_file} does not exist for CalibrationRun {calibration_run.id}')
        # Read the best iteration from the log
        last_line = read_last_line(objective_log_best_file)
        best_iteration_for_worker = int(last_line.split(',')[2])
    else:
        # for GWO and PSO, we can't get the best iteration number.
        # We need to read the actual best parameters and then try to match them up when we read the parameter file later
        pass

    # Prefetch Iteration objects for efficiency
    iteration_dict = {it.iteration_num: it for it in iterations}

    metrics_to_create = []  # List to accumulate metrics to be created
    params_to_create = []  # List to accumulate parameters to be created

    # Collect best_params updates instead of saving per-iteration
    iterations_to_update_best_flag = []

    # Track whether a best iteration was set
    best_iteration_found = False

    # Process metrics file
    metrics_df = pd.read_csv(metrics_iteration_file)
    if not have_LSTM_flag:
        update_objective_function_values(metrics_iteration_file, calibration_run, worker_name)

    for _, row in metrics_df.iterrows():
        iteration_num = int(row['iteration'])

        row_dict: dict[str, float | None] = {
            str(k): sanitize_metric_value(v)
            for k, v in row.items()
            if k != 'iteration'
        }

        iteration = iteration_dict.get(iteration_num)
        if not iteration:
            raise CerfException(f"Iteration {iteration_num} not found for worker {worker_name}")
        process_metrics_row_for_calibration(iteration, row_dict, metrics_to_create, job_description)

        if have_LSTM_flag:
            # For LSTM, there is only 1 iteration so we will mark it as having the best
            iteration.best_params = True
            iterations_to_update_best_flag.append(iteration)  # buffer update

    # Process parameters file
    # xxx
    if not have_LSTM_flag:
        params_df = pd.read_csv(params_iteration_file)

        # # Prefetch CalibrationParameter objects once per calibration_run
        # job_parameters = CalibrationParameter.objects.filter(
        #     calibration_formulation__calibration_run_id=calibration_run.id
        # )
        # params_lookup = {p.name.lower(): p for p in job_parameters}

        for _, row in params_df.iterrows():
            iteration_num = int(row['iteration'])

            iteration = iteration_dict.get(iteration_num)
            if not iteration:
                raise CerfException(f"Iteration {iteration_num} not found for worker {worker_name}")

            # Determine if best iteration
            params_row: dict[str, float | None] = {
                str(k): sanitize_metric_value(v)
                for k, v in row.items()
                if k != 'iteration'
            }

            is_best_match = params_match_best(params_row, best_params_dict)

            iteration.best_params = (
                    is_best_match or
                    iteration.iteration_num == best_iteration_for_worker
            )

            if iteration.best_params:
                best_iteration_found = True

            # Buffer update — saved once via bulk_update
            iterations_to_update_best_flag.append(iteration)

            # Create parameters
            process_params_row(
                iteration,
                params_row,
                params_to_create,
                params_lookup,
                job_description
            )

    # Bulk create IterationMetric and IterationParameter objects in chunks
    if metrics_to_create:
        for i in range(0, len(metrics_to_create), BULK_CREATE_BATCH_SIZE):
            batch = metrics_to_create[i:i + BULK_CREATE_BATCH_SIZE]
            IterationMetric.objects.bulk_create(batch)

    # Bulk create parameters
    if params_to_create:
        for i in range(0, len(params_to_create), BULK_CREATE_BATCH_SIZE):
            batch = params_to_create[i:i + BULK_CREATE_BATCH_SIZE]
            IterationParameter.objects.bulk_create(batch)

    # Single bulk update for best_params instead of thousands of saves
    if iterations_to_update_best_flag:
        Iteration.objects.bulk_update(
            iterations_to_update_best_flag,
            ['best_params'],
            batch_size=BULK_CREATE_BATCH_SIZE,
        )

    # Log a warning if no best iteration was found for the worker
    if not best_iteration_found:
        logger.warning(f"No best iteration found for worker {worker_name} in CalibrationRun {calibration_run.id}")

    # Check if this worker has a non-empty Output_Iteration directory
    output_iter = os.path.join(worker_path, 'Output_Iteration')
    if os.path.isdir(output_iter) and any(os.listdir(output_iter)):
        # Iterate over files and check if a file matches the current iteration number
        for filename in os.listdir(output_iter):
            # Check if the file matches the iteration number format
            for iteration_num in iteration_dict:
                expected_filename = get_output_iteration_csv(calibration_run, iteration_num)
                if filename == expected_filename:
                    # Save the filename to the IterationResult for this iteration
                    iteration = iteration_dict.get(iteration_num)
                    if iteration:
                        IterationResult.objects.create(iteration=iteration, filename=filename)
                        logger.info(f"Saved output_iteration filename {filename} for CalibrationRun {calibration_run.id}")
                    break


# Function to process a single metrics row
def process_metrics_row_for_calibration(
        iteration: Iteration,
        metrics_row: dict[str, float | None],
        metrics_to_create: list[IterationMetric],
        job_description: str
) -> None:
    """
    Process a single row from the metrics file and create IterationMetric objects.

    :param iteration: The Iteration object for the current iteration.
    :param metrics_row: The row of metrics data from the file.
    :param metrics_to_create: The list to accumulate created IterationMetric objects.
    :param job_description: Job identifier string used for logging.
    """
    # Get rid of 'iteration' and 'objFunVal' columns
    metrics_row = {k: v for k, v in metrics_row.items() if k not in ['iteration', 'objFunVal']}

    for metric_name, value in metrics_row.items():
        # Perform case-insensitive lookup for the metric
        metric = MetricEnum.get_instance(metric_name.lower())

        if not metric:
            raise CerfException(f"Could not find metric '{metric_name}'")

        metric_value = sanitize_metric_value(value)

        metric_obj = IterationMetric(
            iteration=iteration,
            metric=metric,
            metric_value=metric_value
        )
        logger.debug(f'{job_description}: Creating Iteration metric for {metric_obj}')
        metrics_to_create.append(metric_obj)


# Function to process a single parameters row
def process_params_row(
        iteration: Iteration,
        params_row: dict[str, float | None],
        params_to_create: list[IterationParameter],
        params_lookup: dict[str, CalibrationParameter],
        job_description: str
) -> None:
    """
    Create IterationParameter objects for a single iteration.

    This function:
    - Converts one row from the parameters CSV into IterationParameter records
    - Performs case-insensitive parameter lookup
    - Buffers objects for bulk_create

    Best-iteration selection is handled in process_iterations_for_a_worker().

    :param iteration: The Iteration object for the current iteration.
    :param params_row: The row of parameter data from the file (excluding 'iteration').
    :param params_to_create: Accumulator list for IterationParameter objects.
    :param params_lookup: Mapping of lowercased parameter names to CalibrationParameter objects.
    :param job_description: Job identifier string used for logging.
    """

    # ----------------------------------------------------------------------
    # Create IterationParameter objects
    # ----------------------------------------------------------------------
    for param_name, value in params_row.items():
        # Perform case-insensitive lookup for the parameter
        parameter = params_lookup.get(param_name.lower())
        if not parameter:
            raise CerfException(f"Could not find parameter '{param_name}' referenced in params_iteration_file")

        tuned_value = sanitize_metric_value(value)

        param_obj = IterationParameter(
            iteration=iteration,
            calibration_parameter=parameter,
            tuned_value=tuned_value
        )
        logger.debug(
            f"{job_description}: Creating IterationParameter "
            f"for iteration={iteration.iteration_num}, param={parameter.name}, value={tuned_value}"
        )

        params_to_create.append(param_obj)


def update_objective_function_values(metrics_iteration_file: str, calibration_run: CalibrationRun, worker_name: str) -> None:
    """
    Updates the objective function values for each iteration of a given worker in a calibration run.

    :param metrics_iteration_file: The file path to the CSV metrics file containing iteration numbers and objective function values.
    :param calibration_run: The CalibrationRun instance to which the iterations belong.
    :param worker_name: The name of the worker whose iterations are being updated.
    """
    iterations_dict = {
        iteration_num: Iteration(id=iteration_id, objective_function_value=None)  # Initialize without value
        for iteration_id, iteration_num in Iteration.objects.filter(
            calibration_run_id=calibration_run.id,
            worker_name=worker_name
        ).values_list("id", "iteration_num")
    }

    # Read the metrics file using pandas
    metrics_df = pd.read_csv(metrics_iteration_file, usecols=["iteration", "objFunVal"])

    iterations_to_update = []  # List to accumulate iterations to update

    # Iterate over rows in the DataFrame
    for _, row in metrics_df.iterrows():
        iteration_num = int(row['iteration'])  # type: ignore[arg-type]
        obj_fun_val = sanitize_metric_value(row['objFunVal'])

        # Retrieve the iteration object from the dictionary
        iteration = iterations_dict.get(iteration_num)
        if not iteration:
            raise CerfException(
                f"Cannot find Iteration object for calibration run {calibration_run.id}, "
                f"worker {worker_name}, iteration {iteration_num}. Ngen-cal did not report this iteration"
            )

        logger.debug(
            f'{calibration_run.id}_{calibration_run.owner.username} Updating iteration {iteration_num} '
            f'for worker {worker_name} with output variable value {obj_fun_val}'
        )
        iteration.objective_function_value = obj_fun_val

        # Add the modified object to the list
        iterations_to_update.append(iteration)

    # Perform a bulk update for all iterations in chunks if there are any updates
    if iterations_to_update:
        with transaction.atomic():  # Ensure atomicity of the bulk update
            for i in range(0, len(iterations_to_update), BULK_CREATE_BATCH_SIZE):
                Iteration.objects.bulk_update(
                    iterations_to_update[i:i + BULK_CREATE_BATCH_SIZE], ['objective_function_value']
                )


# Function to read the last line of a file
def read_last_line(filename: str) -> str:
    """
    Reads and returns the last line of a file.

    :param filename: The path to the file.
    :return: The last line of the file as a string.
    """
    with open(filename, 'r', encoding='utf-8') as file:
        return deque(file, maxlen=1).pop().strip()


# Function to count the number of rows in a CSV file
def count_rows_in_csv(file_path: str) -> int:
    """
    Counts the number of rows in a CSV file, excluding the header.

    :param file_path: The path to the CSV file.
    :return: The number of rows in the CSV file (excluding the header).
    """
    with open(file_path, 'r') as file:
        # Count the lines and subtract 1 for the header
        return sum(1 for _ in file) - 1


def parse_duration(duration_str: str | None) -> timedelta | None:
    """
    Converts a duration string (D-HH:MM:SS or HH:MM:SS) into a timedelta object.

    :param duration_str: The duration string in D-HH:MM:SS or HH:MM:SS format.
    :return: A timedelta object representing the duration.
    """
    if not duration_str:
        return None
    try:
        if '-' in duration_str:
            days_part, time_part = duration_str.split('-')
            days = int(days_part)
        else:
            time_part = duration_str
            days = 0
        hours, minutes, seconds = map(int, time_part.split(':'))
        return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    except (ValueError, TypeError):
        return None


def parse_size_to_kb(size_str: str | None) -> float | None:
    """
    Converts a size string (e.g., '123K', '1.5M', '2G') to a float in kilobytes.

    :param size_str: The size string with an optional unit suffix (K, M, G).
    :return: The size converted to kilobytes or None if invalid.
    """

    if not size_str:
        return None

    size_str = size_str.strip().upper()
    try:
        if size_str.endswith('K'):
            return float(size_str[:-1])
        elif size_str.endswith('M'):
            return float(size_str[:-1]) * 1024
        elif size_str.endswith('G'):
            return float(size_str[:-1]) * 1024 ** 2
        else:
            # Assume no unit means it's already in KB
            return float(size_str)
    except ValueError:
        return None


def parse_performance_metrics(file_path: str) -> PerformanceMetrics | None:
    """
    Opens the pipe-delimited file, parses the content, and extracts performance metrics to save to the database.

    This function assumes:
    - `MaxRSS`, `MaxDiskRead`, and `MaxDiskWrite` fields are only present in the `.batch` job line.
    - `Planned` (previously called `Reserved`) is only present in the non-batch job line.

    The function:
    - Logs a warning if any expected field is missing.
    - Extracts job performance details from batch and non-batch job records.
    - Returns a `PerformanceMetrics` instance populated with parsed values.

    :param file_path: The path to the performance metrics file.
    :return: A `PerformanceMetrics` object with extracted metrics, or `None` if the file is missing or invalid.
    """
    if not os.path.exists(file_path):
        logger.error(f'Performance metrics file {file_path} not found')
        return None
    else:
        logger.info(f'Reading performance metrics from {file_path}')

    reserved_time = None
    batch_metrics = None

    with open(file_path, 'r') as file:
        reader = csv.DictReader(file, delimiter='|')

        for row in reader:
            job_id = row['JobID']

            if job_id.endswith('.batch'):
                # Expected fields only for the .batch line
                expected_batch_fields = ['Elapsed', 'NCPUS', 'CPUTime', 'MaxRSS', 'MaxDiskRead', 'MaxDiskWrite']
                missing_fields = [field for field in expected_batch_fields if not row.get(field)]
                if missing_fields:
                    logger.warning(f'Missing fields for batch JobID {job_id}: {", ".join(missing_fields)}')

                # Collect data from the .batch line with fallback to None for missing fields
                batch_metrics = {
                    'slurm_job_id': job_id,
                    'run_time': parse_duration(row.get('Elapsed')),
                    'num_cpus': int(row.get('NCPUS')) if row.get('NCPUS') else None,
                    'cpu_time': parse_duration(row.get('CPUTime')),
                    'max_rss': parse_size_to_kb(row.get('MaxRSS')),
                    'max_disk_read': parse_size_to_kb(row.get('MaxDiskRead')),
                    'max_disk_write': parse_size_to_kb(row.get('MaxDiskWrite')),
                    'reserved_time': reserved_time  # This will be updated later if available
                }
            else:
                # Expected fields only for the non-batch line
                expected_non_batch_fields = ['Elapsed', 'NCPUS', 'CPUTime', 'Planned']
                missing_fields = [field for field in expected_non_batch_fields if not row.get(field)]
                if missing_fields:
                    logger.warning(f'Missing fields for non-batch JobID {job_id}: {", ".join(missing_fields)}')

                # Save the reserved time from the non-.batch line
                reserved_time = parse_duration(row.get('Planned'))

    if batch_metrics:
        # Update the reserved_time for the batch metrics
        batch_metrics['reserved_time'] = reserved_time

        # Create or update the PerformanceMetrics record
        metrics = PerformanceMetrics.objects.create(**batch_metrics)
        return metrics

    return None


def params_match_best(params_row: dict[str, float | None], best_params_dict: dict[str, float | None]) -> bool:
    """
    Determine whether a row of tuned parameters exactly matches the known global-best parameters.

    NOTES:
    - Used only for GWO/PSO jobs. DDS never uses parameter matching.
    - best_params_dict comes from global_best_params_file.
    - A match requires:
        1. The row contains all global-best parameters
        2. Same parameter names (case-insensitive).
        3. Values matching within floating-point tolerance,
           treating None as an exact match to None.
    - This check is used for actual best-iteration selection in
      process_iterations_for_a_worker().
    """
    # If we have no global best params (DDS or missing file), never match.
    if not best_params_dict:
        return False

    # Normalize keys to avoid CSV header casing mismatches.
    row = {k.lower(): v for k, v in params_row.items()}
    best = {k.lower(): v for k, v in best_params_dict.items()}

    # Strict: same parameter set size
    if len(row) != len(best):
        return False

    # Strict: same parameter names
    if row.keys() != best.keys():
        return False

    # For each global-best parameter:
    # - Require it to exist in the row
    # - Require its value to match (None must match None; floats within tolerance)
    for name, b in best.items():
        v = row[name]

        # None matches None only.
        if v is None or b is None:
            if v is b:
                continue
            return False

        # Float match within tolerance.
        if not math.isclose(v, b, rel_tol=1e-9, abs_tol=0.0):
            return False

    return True
