import json
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse

import fsspec
import pandas as pd
from datetimerange import DateTimeRange
from django.conf import settings
from django.db import transaction
from mswm.manager import build_fcst, build_calib
from rest_framework.response import Response

from calibration.enums import StatusEnum, ValidationType, SlurmCallbackStatusEnum
from calibration.enums_vanilla import JobType
from calibration.models import CalibrationRun, ValidationRun, Iteration, ForecastRun, ColdStartRun, VerificationRun
from calibration.models.base_run import BaseRun
from calibration.models.hindcast_run import HindcastRun
from calibration.util.git_util import get_git_info_internal
from calibration.util.ngen_locations import get_calibration_input_file, get_validation_best_stdout_file, get_validation_control_stdout_file, \
    get_calibration_stdout_file, get_validation_best_input_file, get_validation_control_input_file, get_validation_iteration_stdout_file, \
    get_forecast_stdout_file, get_forecast_dir, get_validation_iteration_git_info_file, get_validation_special_git_info_file, \
    get_calibration_git_info_file, get_forecast_git_info_file, get_forcing_dir_for_job, get_verification_yaml_config_file, \
    get_verification_git_info_file, get_forecast_realization_file, get_cold_start_realization_file, \
    get_cold_start_stdout_file, get_cold_start_dir, \
    get_cold_start_git_info_file, get_hindcast_stdout_file, get_hindcast_git_info_file, get_hindcast_dir, get_cold_start_state, \
    get_verification_stdout_file
from calibration.views import ngen_cal_input
from calibration.views.common import ResponseError, CerfException, create_validation_run_internal, get_job_description, write_ngen_logging_file
from calibration.views.data_services import should_use_bmi_forcing
from calibration.views.end_of_job_processing import read_validation_output, read_calibration_output, read_forecast_output, \
    read_cold_start_output, read_verification_output, read_hindcast_output
from calibration.views.forecast_input import create_forecast_input
from calibration.views.ngen_cal_input import ready_to_run
from calibration.views.verification_input import create_verification_input
from cerfServer.settings import NgenEnvironmentEnum

logger = logging.getLogger(__name__)

# Job registry to store subprocess objects keyed by a unique string (e.g., "calibration_123")
job_registry: dict[str, subprocess.Popen] = {}


def get_job_registry_key(run: BaseRun) -> str:
    """
    Generate a unique string key for the job registry based on run type.

    Format: "<run_class>_<id>" (all lowercase).
    Examples:
      - CalibrationRun(id=123) → "calibrationrun_123"
      - ValidationRun(id=45)   → "validationrun_45"
      - ForecastRun(id=67)     → "forecastrun_67"
      - ForecastForcingDownloadRun(id=89) → "forecastforcingdownloadrun_89"

    :param run: The CalibrationRun, ValidationRun, ForecastRun, or ForecastForcingDownloadRun object.
    :return: A unique string key for the job registry.
    """
    return f"{run.__class__.__name__.lower()}_{run.id}"


def set_job_status(run: BaseRun, status: StatusEnum | None, failure_messages: dict = None) -> None:
    """
    Update the status and related metadata for a run, and clean up registry state if appropriate.

    Behavior:
      - If `status` is provided, update the run's status field.
      - In LOCAL or DOCKER environments:
          * Clear `slurm_job_id`.
          * Remove the run from the job registry.
      - In PW environment: keep the real Slurm job ID.
      - If `failure_messages` are provided, store them as JSON.

    :param run: The CalibrationRun, ValidationRun, ForecastRun or HindcastRun object.
    :param status: The new status to set. If None, status is left unchanged.
    :param failure_messages: Optional failure details to record.
    """
    update_fields: list[str] = []

    if status:
        run.status = status.db_instance
        update_fields.append("status")

    # Only clear slurm_job_id for LOCAL/DOCKER
    if settings.NGEN_ENVIRONMENT in [NgenEnvironmentEnum.LOCAL, NgenEnvironmentEnum.DOCKER]:
        run.slurm_job_id = None
        update_fields.append("slurm_job_id")
        job_registry.pop(get_job_registry_key(run), None)

    if failure_messages:
        run.failure_messages = json.dumps(failure_messages)
        update_fields.append("failure_messages")

    if update_fields:
        run.save(update_fields=update_fields)


def get_run_owner(run: BaseRun):
    """
    Retrieve the owner of a BaseRun object.

    Determines the owner of the job from its relationship to CalibrationRun

    :param run: The BaseRun object (CalibrationRun, ValidationRun, etc.).
    :return: The owner of the associated CalibrationRun or the run itself.
    :raises AttributeError: If the owner cannot be determined.
    """
    if hasattr(run, 'owner'):  # CalibrationRun case
        return run.owner
    elif hasattr(run, 'calibration_run'):
        return run.calibration_run.owner
    elif hasattr(run, 'forecast_run'):  # VerificationRun
        return run.forecast_run.calibration_run.owner
    raise AttributeError(f"Cannot determine owner for run of type {type(run).__name__}")


def validate_cmd_args(cmd_line_args: dict[str, str], stdout_file: str) -> None:
    """
    Validates the command-line arguments and output file paths for LOCAL and DOCKER environments.

    This function ensures that all arguments passed to subprocess-based commands are valid types
    (str, bytes, or os.PathLike) and not None. It raises a TypeError if any invalid argument type
    is encountered, or a ValueError if any argument value is None.

    :param cmd_line_args: A dictionary of command-line arguments where the keys are argument names
                          and the values are their corresponding values.
    :param stdout_file: The path to the file where the job's stdout will be written.
                        It must be a valid path-like object.
    :raises TypeError: If any argument or the stdout file is not a valid type.
    :raises ValueError: If any argument value is None.
    """

    # Define allowed types for clarity
    allowed_types = (str, bytes, os.PathLike)

    # Validate each argument in the command-line arguments dictionary
    for key, value in cmd_line_args.items():
        if value is None:
            logger.error(f"Argument '{key}' is None, which is not allowed.")
            raise ValueError(f"Command-line argument '{key}' cannot be None.")

        # Check if the value is one of the allowed types
        if not isinstance(value, allowed_types):
            # Log the invalid argument with valid type information
            logger.error(
                f"Invalid argument for '{key}': {value} (type: {type(value)}). "
                f"Expected one of {allowed_types}."
            )
            # Raise a TypeError with details about the invalid argument
            raise TypeError(
                f"Invalid argument for '{key}': {value} (type: {type(value)}). "
                f"Expected one of {allowed_types}."
            )

    # Validate the stdout file path to ensure it's a valid type
    if not isinstance(stdout_file, allowed_types):
        # Log the invalid stdout file path with valid type information
        logger.error(
            f"Invalid stdout_file: {stdout_file} (type: {type(stdout_file)}). "
            f"Expected one of {allowed_types}."
        )
        # Raise a TypeError with details about the invalid stdout file path
        raise TypeError(
            f"Invalid stdout_file: {stdout_file} (type: {type(stdout_file)}). "
            f"Expected one of {allowed_types}."
        )


def execute_job(run: BaseRun, cmd_line_args: dict[str, str], stdout_file: str, simulate: bool = False) -> None:
    """
    Execute a job based on the configured NGEN environment.

    This function dynamically calls the appropriate job execution function
    based on the environment (LOCAL, DOCKER, or PARALLEL_WORKS).

    :param run: The BaseRun object (CalibrationRun, ValidationRun, etc.).
    :param cmd_line_args: A dictionary of command-line arguments for the job.
    :param stdout_file: The path to the file where the job's stdout will be written.
    :param simulate: For LOCAL or DOCKER jobs, if True, simulates successful execution without running a real job.
    :raises CerfException: If the environment is unsupported.
    """
    if settings.NGEN_ENVIRONMENT in [NgenEnvironmentEnum.LOCAL, NgenEnvironmentEnum.DOCKER]:
        # Validate for LOCAL and DOCKER environments
        validate_cmd_args(cmd_line_args, stdout_file)

        from calibration.run_util.run_ngen_cal_local import run_job_local
        run_job_local(run, cmd_line_args, stdout_file, simulate=simulate)
    elif settings.NGEN_ENVIRONMENT == NgenEnvironmentEnum.PARALLEL_WORKS:
        from calibration.run_util.run_ngen_cal_pw import submit_job_to_slurm
        # Resolve owner dynamically for the Slurm submission
        try:
            owner = get_run_owner(run)  # Use the utility function
        except AttributeError as e:
            raise CerfException(f"Error retrieving owner for run {run.id}: {str(e)}")
        submit_job_to_slurm(run, owner, cmd_line_args, stdout_file)
    else:
        raise CerfException(f"Unsupported environment: {settings.NGEN_ENVIRONMENT}")

    run.sent_date = datetime.now(timezone.utc)
    run.save(update_fields=['sent_date'])


def cancel_job_common(run: BaseRun) -> bool:
    """
    Cancel a job using the appropriate environment-specific logic.

    This function handles job cancellation for LOCAL, DOCKER, and PARALLEL_WORKS environments.

    :param run: The CalibrationRun, ValidationRun, or ForecastRun object.
    :return: True if the job was successfully canceled; False otherwise.
    """
    if settings.NGEN_ENVIRONMENT in [NgenEnvironmentEnum.LOCAL, NgenEnvironmentEnum.DOCKER]:
        from calibration.run_util.run_ngen_cal_local import cancel_local_job
        return cancel_local_job(run)
    elif settings.NGEN_ENVIRONMENT == NgenEnvironmentEnum.PARALLEL_WORKS:
        from calibration.run_util.run_ngen_cal_pw import cancel_slurm_job
        return cancel_slurm_job(run)
    else:
        logger.error(f"Unsupported environment: {settings.NGEN_ENVIRONMENT}")
        return False


def run_calibration_job(calibration_run: CalibrationRun) -> None:
    """
    Start a calibration job by determining input and output file paths.

    This function is intended to be passed as an argument to `submit_job`
    and not called directly.

    :param calibration_run: The CalibrationRun object representing the job.
    :raises CerfException: If the input file does not exist.
    """
    input_file = get_calibration_input_file(calibration_run)
    if not os.path.exists(input_file):
        raise CerfException(
            f"Input file '{input_file}' does not exist for Calibration Job {calibration_run.id}, user: {calibration_run.owner.username}"
        )

    stdout_file = get_calibration_stdout_file(calibration_run)

    execute_job(
        calibration_run,
        {
            'input_file': input_file,
            'nprocs': str(calibration_run.mpi_nprocs)
        },
        stdout_file,
        simulate=settings.SIMULATE_FLAGS.get(JobType.CALIBRATION, False)
    )


def run_validation_job(validation_run: ValidationRun) -> None:
    """
    Start a validation job by determining input and output file paths.

    This function is intended to be passed as an argument to `submit_job`
    and not called directly.

    :param validation_run: The ValidationRun object representing the job.
    :raises CerfException: If the input file does not exist.
    """
    if validation_run.validation_type == ValidationType.VALID_BEST.value:
        input_file = get_validation_best_input_file(validation_run.calibration_run)
        stdout_file = get_validation_best_stdout_file(validation_run.calibration_run)
    elif validation_run.validation_type == ValidationType.VALID_CONTROL.value:
        input_file = get_validation_control_input_file(validation_run.calibration_run)
        stdout_file = get_validation_control_stdout_file(validation_run.calibration_run)
    else:
        # Regular validation
        input_file = get_calibration_input_file(validation_run.calibration_run)
        stdout_file = get_validation_iteration_stdout_file(validation_run.calibration_run, validation_run.worker_name, validation_run.iteration_num)

    if not os.path.exists(input_file):
        raise CerfException(
            f"Input file '{input_file}' does not exist for Validation Job {validation_run.id}, "
            f"user: {validation_run.calibration_run.owner.username}, type: {validation_run.validation_type}"
        )

    cmd_line_args = {'input_file': input_file}
    if validation_run.validation_type == ValidationType.VALID_ITERATION.value:
        # For running local, we need to leave these out
        cmd_line_args['worker_name'] = validation_run.worker_name
        cmd_line_args['iteration_num'] = str(validation_run.iteration_num)
    cmd_line_args['nprocs'] = str(validation_run.calibration_run.mpi_nprocs)
    execute_job(
        validation_run,
        cmd_line_args,
        stdout_file,
        simulate=settings.SIMULATE_FLAGS.get(JobType.VALIDATION, False)
    )


def run_cold_start_job(cold_start_run: ColdStartRun) -> None:
    """
    Start a cold start job by determining input and output file paths.

    This function is intended to be passed as an argument to `submit_job`
    and not called directly.

    :param cold_start_run: The ColdStartRun object representing the job.
    """
    validation_yaml = get_validation_best_input_file(cold_start_run.calibration_run)
    if not os.path.exists(validation_yaml):
        raise CerfException(
            f"Input file '{validation_yaml}' does not exist for {get_job_description(cold_start_run)}"
        )
    realization_file = get_cold_start_realization_file(cold_start_run)
    stdout_file = get_cold_start_stdout_file(cold_start_run)

    execute_job(
        cold_start_run,
        {
            'validation_yaml': validation_yaml,
            'realization_file': realization_file
        },
        stdout_file,
        simulate=settings.SIMULATE_FLAGS.get(JobType.COLD_START, False)
    )


def run_forecast_job(forecast_run: ForecastRun) -> None:
    """
    Start a forecast job by determining input and output file paths.

    This function is intended to be passed as an argument to `submit_job`
    and not called directly.

    :param forecast_run: The ForecastRun object representing the job.
    """
    validation_yaml = get_validation_best_input_file(forecast_run.calibration_run)
    if not os.path.exists(validation_yaml):
        raise CerfException(
            f"Input file '{validation_yaml}' does not exist for {get_job_description(forecast_run)}"
        )
    realization_file = get_forecast_realization_file(forecast_run)
    stdout_file = get_forecast_stdout_file(forecast_run)

    execute_job(
        forecast_run,
        {
            'validation_yaml': validation_yaml,
            'realization_file': realization_file
        },
        stdout_file,
        simulate=settings.SIMULATE_FLAGS.get(JobType.FORECAST, False)
    )


def run_hindcast_job(hindcast_run: HindcastRun) -> None:
    """
    Start a hindcast job by determining input and output file paths.

    This function is intended to be passed as an argument to `submit_job`
    and not called directly.

    :param hindcast_run: The HindcastRun object representing the job.
    """
    validation_yaml = get_validation_best_input_file(hindcast_run.calibration_run)
    if not os.path.exists(validation_yaml):
        raise CerfException(
            f"Input file '{validation_yaml}' does not exist for {get_job_description(hindcast_run)}"
        )

    stdout_file = get_hindcast_stdout_file(hindcast_run)

    cold_start_state = get_cold_start_state(hindcast_run.cold_start_run)
    if not os.path.exists(cold_start_state):
        # Issue an explicit error for legacy Cold Start runs that might not have a saved state
        raise RuntimeError(f'Saved state not found for cold start run {get_job_description(hindcast_run)}')

    execute_job(
        hindcast_run,
        {
            'validation_yaml': validation_yaml,
            'config_file': create_forecast_input(hindcast_run),
            'run_name': os.path.basename(get_hindcast_dir(hindcast_run)),
            'interval_cycle': str(hindcast_run.interval_cycle),
            'num_iterations': str(hindcast_run.num_iterations),
            'use_state': cold_start_state
        },
        stdout_file,
        simulate=settings.SIMULATE_FLAGS.get(JobType.HINDCAST, False)
    )


def run_verification_job(verification_run: VerificationRun) -> None:
    """
    Start a verification job by determining input and output file paths.

    This function is intended to be passed as an argument to `submit_job`
    and not called directly.

    :param verification_run: The VerificationRun object representing the job.
    """
    stdout_file = get_verification_stdout_file(verification_run)

    execute_job(
        verification_run,
        {
            'verification_config': create_verification_input(verification_run),
        },
        stdout_file,
        simulate=settings.SIMULATE_FLAGS.get(JobType.VERIFICATION, False)
    )


def submit_job(run: BaseRun, logging_config=None) -> Response | None:
    """
    Submits a job by setting initial metadata and dispatching it to the appropriate execution function.

    - Sets the submission timestamp and updates the job status to 'SUBMITTED'.
    - For CalibrationRun, performs additional preprocessing, validation, and input generation.
    - Selects the appropriate job runner based on the job type (calibration, validation, forecast, etc.).
    - For each run type, a git info file is created prior to execution.
    - If an error occurs during submission, the job status is set to 'FAILED' and the error is logged.

    :param run: A CalibrationRun, ValidationRun, ForecastRun, or VerificationRun object.
    :param logging_config: Optional logging configuration to use when creating calibration job logs.
    :return: None if successful; a DRF Response object if the job is not ready or fails preprocessing.
    :raises CerfException: If the run type is unsupported or job execution fails.
    """
    if isinstance(run, CalibrationRun):
        # Before we attempt to submit, make sure it's ready
        error_object, _ = ready_to_run(run)
        if error_object.has_errors() or error_object.has_warnings():
            return ResponseError(error_object)

    with transaction.atomic():
        # Set submission date and status
        run.submit_date = datetime.now(timezone.utc)
        run.status = StatusEnum.SUBMITTED.db_instance
        run.save(update_fields=['submit_date', 'status'])

    try:
        # Do pre-processing for certain jobs
        if isinstance(run, CalibrationRun):
            write_ngen_logging_file(run, logging_config)
            fatal, response = prepare_calibration_job(run)
            if response:
                if fatal:
                    failure_message = {
                        'validation_errors': response.data.get("validation_errors"),
                        'errors': response.data.get("errors"),
                    }

                    run.status = StatusEnum.FAILED.db_instance
                    run.failure_messages = json.dumps(failure_message)
                    run.save(update_fields=['status', 'failure_messages'])
                return response
        elif isinstance(run, (ColdStartRun, ForecastRun)):
            write_ngen_logging_file(run, logging_config)
            _, _ = prepare_fcst_or_cold_start_job(run)

        # Determine the appropriate job execution function
        if isinstance(run, CalibrationRun):
            create_git_info(get_calibration_git_info_file(run))
            run_calibration_job(run)
        elif isinstance(run, ValidationRun):
            if run.validation_type != ValidationType.VALID_ITERATION.value:
                create_git_info(get_validation_special_git_info_file(run))
            else:
                create_git_info(get_validation_iteration_git_info_file(run, run.worker_name, run.iteration_num))

            run_validation_job(run)
        elif isinstance(run, ColdStartRun):
            create_git_info(get_cold_start_git_info_file(run))

            run_cold_start_job(run)
        elif isinstance(run, ForecastRun):
            create_git_info(get_forecast_git_info_file(run))

            run_forecast_job(run)
        elif isinstance(run, HindcastRun):
            create_git_info(get_hindcast_git_info_file(run))

            run_hindcast_job(run)
        elif isinstance(run, VerificationRun):
            create_git_info(get_verification_git_info_file(run))

            run_verification_job(run)
        else:
            raise CerfException(f"Unsupported run type: {type(run).__name__}")
    except Exception as e:
        # Handle failures by marking the job as FAILED
        run.status = StatusEnum.FAILED.db_instance

        msg = f'Exception submitting {get_job_description(run)} - {str(e)}'
        logger.exception(msg)
        failure_messages = {'message': msg}
        run.failure_messages = json.dumps(failure_messages)

        run.save(update_fields=['status', 'failure_messages'])

        raise  # Re-raise the exception

    logger.info(f"{get_job_description(run)} successfully submitted.")
    return None


def create_git_info(git_info_file: str) -> None:
    logger.info(f"Writing git info to {git_info_file}")
    git_info_data = get_git_info_internal()
    os.makedirs(os.path.dirname(git_info_file), exist_ok=True)
    with open(git_info_file, 'w') as f:
        f.write(json.dumps(git_info_data, indent=4))


def prepare_calibration_job(calibration_run: CalibrationRun) -> tuple[bool, Response | None]:
    """
    Prepare a CalibrationRun job by validating inputs, preprocessing data, and generating configuration files.

    This function performs:
    - Readiness validation using `ready_to_run`
    - Forcing/observational data subsetting
    - Input file generation using `create_input`

    This is only used internally by `submit_job` for CalibrationRun.

    :param calibration_run: The CalibrationRun object to prepare.
    :return: A tuple (fatal_error: bool, Response). If preparation is successful, returns (False, None).
             If errors occur, returns (True, error response) or (False, warning response).
    """
    error_object, config_file = ngen_cal_input.ready_to_run(calibration_run, build=True)

    if error_object.has_warnings() or error_object.has_errors():
        return error_object.has_errors(), ResponseError(
            f'Calibration Job {calibration_run.id} is not ready',
            validation_errors=error_object.warnings,
            errors=error_object.errors
        )

    job_description = get_job_description(calibration_run)
    try:
        logger.info(f'Final preparation to run Calibration Job {calibration_run.id}')
        validation_errors = final_preprocessing_for_calibration(calibration_run)

        if validation_errors:
            return True, ResponseError(
                f'Calibration Job {calibration_run.id} failed validation after preprocessing',
                errors=validation_errors
            )

        logger.info(f'Running build_calib for {job_description} with config {config_file}')
        build_calib(config_file)
    except Exception as e:
        CalibrationRun.objects.filter(id=calibration_run.id).update(status=StatusEnum.FAILED.db_instance)
        msg = f'Exception during build_calib for {job_description} - {str(e)}'
        logger.exception(msg)
        raise CerfException(msg) from e

    logger.info(f'Return from build_calib for {job_description}')
    return False, None


def prepare_fcst_or_cold_start_job(run: ColdStartRun | ForecastRun | HindcastRun) -> tuple[bool, Response | None]:
    """
    Prepare a ColdStartRun, ForecastRun or Hindcast job by generating configuration files.

    This function:
    - Calls create_forecast_input(run) to generate the config
    - Uses get_validation_best_input_file() for the calibration baseline
    - Runs build_fcst() with use_cold_start=True if run is a ColdStartRun

    :param run: A ForecastRun or ColdStartRun instance.
    :return: (fatal_error: bool, Response) — If preparation is successful, returns (False, None).
             If errors occur, returns (True, error response) or (False, warning response).
    """
    job_description = get_job_description(run)

    try:
        config_file = create_forecast_input(run)
        valid_best = get_validation_best_input_file(run.calibration_run)

        if isinstance(run, ColdStartRun):
            run_name = os.path.basename(get_cold_start_dir(run))
            use_cold_start = True
            save_state = True
            saved_state = None
        else:  # ForecastRun
            run_name = os.path.basename(get_forecast_dir(run))
            use_cold_start = False
            save_state = False
            saved_state = get_cold_start_state(run.cold_start_run) if run.cold_start_run else None

        logger.info(f'Running build_fcst for {job_description} '
                    f'with config: {config_file}, valid_best: {valid_best}, run_name: {run_name}, save_state: {save_state}, load_state_from: {saved_state}')

        build_fcst(config_file, valid_best, run_name, use_cold_start=use_cold_start, save_state=save_state, load_state_from=saved_state)
    except Exception as e:
        # Mark the run as failed
        run.__class__.objects.filter(id=run.id).update(status=StatusEnum.FAILED.db_instance)
        msg = f'Exception during build_fcst for {job_description} - {str(e)}'
        logger.exception(msg)
        raise CerfException(msg) from e

    logger.info(f'Return from build_fcst for {job_description}')
    return False, None


def create_and_submit_validation_control(calibration_run: CalibrationRun) -> None:
    """
    Create a validation run of type VALID_CONTROL and submit it.

    :param calibration_run: The CalibrationRun object for which the validation control run is created.
    """
    validation_run = create_validation_run_internal(calibration_run, None, validation_type=ValidationType.VALID_CONTROL)
    submit_job(validation_run)


def process_validation_output_and_maybe_create_best(validation_run: ValidationRun, failed_so_far: bool) -> None:
    """
    Process validation output and, if this was a VALID_CONTROL run,
    create and submit a follow-up VALID_BEST run after the current
    DB transaction commits.

    :param validation_run: The ValidationRun object representing the job run.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    - True if the job encountered a failure.
    - False if the job has completed successfully so far.
    """
    job_description = get_job_description(validation_run)

    try:
        # Process the validation output
        read_validation_output(validation_run, failed_so_far)
        if not failed_so_far:
            set_job_status(validation_run, StatusEnum.DONE)
    except Exception as e:
        # Catch the exception and mark the job as FAILED
        msg = f"Error processing validation output for {job_description}: {str(e)}"
        logger.exception(msg)
        failure_messages = {'message': msg}
        set_job_status(validation_run, StatusEnum.FAILED, failure_messages)
        return  # Stop further processing if the job failed

    if failed_so_far:
        return

    # Only VALID_CONTROL can trigger a follow-up VALID_BEST run
    if validation_run.validation_type != ValidationType.VALID_CONTROL.value:
        return

    if not validation_run.calibration_run.automatic_validation:
        return

    # We just ran Validation Control, so need to run Validation Best
    # Create the VALID_BEST run now, but don't attach the iteration yet.
    best_validation_run = create_validation_run_internal(
        validation_run.calibration_run, None, validation_type=ValidationType.VALID_BEST
    )

    # Run this AFTER the surrounding transaction commits,
    # so we see the final 'best_params' state (not the intermediate writes).
    def _finish():
        with transaction.atomic():
            iteration = (
                Iteration.objects
                .filter(calibration_run=validation_run.calibration_run, best_params=True)
                .get()
            )
            # Set the iteration containing the best values before we run it
            best_validation_run.iteration = iteration
            best_validation_run.save(update_fields=['iteration'])
            submit_job(best_validation_run)

    transaction.on_commit(_finish)


def run_generic_job_end_callback(
        run: BaseRun,
        status: Future | SlurmCallbackStatusEnum,
        check_if_failed: Callable[[BaseRun, Future | SlurmCallbackStatusEnum], bool],
        finalize_func: Callable[[BaseRun, bool], None]
) -> None:
    """
    Generic callback function for handling job completion.

    :param run: The job object (CalibrationRun, ValidationRun, or ForecastRun) representing the job.
    :param status: The job's completion status. This can be:
        - A `Future` object (for Local environments)
        - A `SlurmStatusEnum` value (for Parallel Works environments)
    :param check_if_failed: Function to check job status based on the environment.
    :param finalize_func: Function to execute finalization logic specific to the job type.
    """
    # TODO Clean up some of the handlers so that we handle the exceptions here instead of the individual handlers
    job_description = get_job_description(run)
    try:
        logger.info(f"Job end callback received for {job_description} with status{status}")

        run.run_end = datetime.now(timezone.utc)
        run.save(update_fields=["run_end"])

        failed_so_far = check_if_failed(run, status)

        # Execute finalization logic
        finalize_func(run, failed_so_far)

    except Exception as e:
        msg = f"Exception occurred during job end callback for {job_description}: {str(e)}"
        logger.exception(msg)
        failure_messages = {'message': msg}
        try:
            set_job_status(run, StatusEnum.FAILED, failure_messages)
        except Exception:
            logger.exception(f"Failed to set FAILED status for {job_description}")


def finalize_calibration_after_callback(run: CalibrationRun, failed_so_far: bool) -> None:
    """
    Finalizes a calibration job after it has completed.

    :param run: The CalibrationRun object representing the job.
    - Reads the output data generated by the calibration job and processes it.
    - Marks the calibration job as DONE in the database, indicating successful completion.
    - Creates and submits a validation control job to verify the calibration's results.

    :param failed_so_far: Indicates whether the job has failed up to this point.
    - True if the job encountered a failure or was cancelled.
    - False if the job has completed successfully so far.
    """
    job_description = get_job_description(run)

    try:
        if failed_so_far:
            # Must have been a cal-mgr/ngen failure
            if run.status == StatusEnum.CANCELLED.db_instance:
                failure_messages = {
                    "message": "Calibration job was cancelled by the user before completion."
                }
                set_job_status(run, None, failure_messages)
            else:
                failure_messages = {
                    "message": "The ngen or cal-mgr job failed. See logs for further details."
                }
                set_job_status(run, None, failure_messages)

        # Process the calibration output regardless of failure/cancel
        read_calibration_output(run, failed_so_far)  # Process and store the output of the calibration job.
        if not failed_so_far:
            set_job_status(run, StatusEnum.DONE)  # Update the job's status to DONE in the database.

    except Exception as e:
        # Catch the exception and mark the job as FAILED
        msg = f"Error processing calibration output for {job_description}: {str(e)}"
        logger.exception(msg)
        failure_messages = {'message': msg}
        set_job_status(run, StatusEnum.FAILED, failure_messages)
        return  # Stop further processing if the job failed

    if failed_so_far:
        # Don’t continue to validation jobs if calibration was failed/cancelled
        return
    # If processing succeeded, continue with the next step
    try:
        create_and_submit_validation_control(run)  # Trigger the creation of validation jobs.
    except Exception as e:
        msg = f"Error creating and submitting validation control run for {job_description}: {str(e)}"
        logger.exception(msg)
        failure_messages = {'message': msg}
        set_job_status(run, StatusEnum.FAILED, failure_messages)


def finalize_validation_after_callback(run: ValidationRun, failed_so_far: bool) -> None:
    """
    Finalizes a validation job after it has completed.

    :param run: The ValidationRun object representing the validation job.
    - Processes the output of the validation job and evaluates its results.
    - If applicable, creates a 'VALID_BEST' validation run.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    - True if the job encountered a failure.
    - False if the job has completed successfully so far.
    """
    process_validation_output_and_maybe_create_best(run, failed_so_far)  # Process the validation results and handle best-run logic.


def finalize_cold_start_after_callback(run: ColdStartRun, failed_so_far: bool) -> None:
    """
    Finalizes a cold start job after it has completed.

    :param run: The ColdStartRun object representing the cold start job.
    - Processes the output of the Cold Start job.
    - Marks the cold start job as DONE in the database, indicating successful completion.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    - True if the job encountered a failure.
    - False if the job has completed successfully so far.
    """
    read_cold_start_output(run, failed_so_far)
    if failed_so_far:
        return

    set_job_status(run, StatusEnum.DONE)  # Update the job's status to DONE in the database.

    # A new cold start may have at most one dependent run associated with it:
    # either one ForecastRun, one HindcastRun, or neither.
    forecast_run = ForecastRun.objects.filter(cold_start_run=run).first()
    hindcast_run = HindcastRun.objects.filter(cold_start_run=run).first()

    if forecast_run and hindcast_run:
        raise ValueError(
            f"ColdStartRun {run.pk} has both a ForecastRun and HindcastRun dependent on it."
        )

    dependent_run = forecast_run or hindcast_run
    if dependent_run:
        submit_job(dependent_run)


def finalize_forecast_after_callback(run: ForecastRun, failed_so_far: bool) -> None:
    """
    Finalizes a forecast job after it has completed.

    :param run: The ForecastRun object representing the forecast job.
    - Processes the output of the forecast job.
    - Marks the forecast job as DONE in the database, indicating successful completion.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    - True if the job encountered a failure.
    - False if the job has completed successfully so far.
    """
    read_forecast_output(run, failed_so_far)
    if failed_so_far:
        return
    set_job_status(run, StatusEnum.DONE)  # Update the job's status to DONE in the database.


def finalize_hindcast_after_callback(run: HindcastRun, failed_so_far: bool) -> None:
    """
    Finalizes a hindcast job after it has completed.

    :param run: The HindcastRun object representing the hindcast job.
    - Processes the output of the hindcast job.
    - Marks the hindcast job as DONE in the database, indicating successful completion.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    - True if the job encountered a failure.
    - False if the job has completed successfully so far.
    """
    read_hindcast_output(run, failed_so_far)
    if failed_so_far:
        return
    set_job_status(run, StatusEnum.DONE)  # Update the job's status to DONE in the database.


def finalize_verification_after_callback(run: VerificationRun, failed_so_far: bool) -> None:
    """
    Finalizes a verification job after it has completed.

    :param run: The VerificationRun object representing the verification job.
    - Processes the output of the verification job.
    - Marks the verification job as DONE in the database, indicating successful completion.
    :param failed_so_far: Indicates whether the job has failed up to this point.
    - True if the job encountered a failure.
    - False if the job has completed successfully so far.
    """
    read_verification_output(run, failed_so_far)
    if failed_so_far:
        return
    set_job_status(run, StatusEnum.DONE)  # Update the job's status to DONE in the database.


def final_preprocessing_for_calibration(run: CalibrationRun) -> list[str]:
    """
    Executes the long-running preparation steps for the given CalibrationRun.
    Assumes that all prerequisites (paths, date ranges) have been validated.

    :param run: The CalibrationRun to process.
    :return: List of validation error messages.
    """
    errors: list[str] = []

    date_range = DateTimeRange(
        min(run.calibration_start_period, run.validation_start_period),
        max(run.calibration_end_period, run.validation_end_period),
    )

    use_bmi = should_use_bmi_forcing(run)

    # ─────────────────────────────────────────────────────────────
    # Forcing data
    # ─────────────────────────────────────────────────────────────
    # Subset only CSV data, not BMI
    if not use_bmi:
        subset_directory_by_time_range(
            run,
            run.forcing_eds_dir_path,
            get_forcing_dir_for_job(run),
            date_range
        )

    return errors


def _get_fs_and_scheme(path_or_url: str):
    """Return (fs, scheme) for local or remote directory."""
    parsed = urlparse(path_or_url)
    scheme = parsed.scheme or "file"
    fs = fsspec.filesystem(scheme)
    return fs, scheme


def _list_dir_files(path_or_url: str) -> list[str]:
    """
    Return a list of full paths (local or remote URLs) for regular files in a directory/prefix.
    - Local: uses os.listdir / os.path.isfile
    - Remote: uses fsspec.ls(detail=True) and filters for files.
      Ensures each returned item is a fully-qualified URL (e.g., s3://bucket/key),
      because some backends (notably s3fs) return names like 'bucket/key' without a scheme.
    """
    parsed = urlparse(path_or_url)
    scheme = parsed.scheme or "file"

    if scheme == "file":
        base = parsed.path or path_or_url
        return [
            os.path.join(base, name)
            for name in os.listdir(base)
            if os.path.isfile(os.path.join(base, name))
        ]

    fs = fsspec.filesystem(scheme)
    entries = fs.ls(path_or_url, detail=True)
    out: list[str] = []
    for e in entries:
        if e.get("type") != "file":
            continue
        name = e.get("name") or ""
        # If the backend returned a scheme-less "bucket/key", add the scheme.
        if not urlparse(name).scheme:
            name = f"{scheme}://{name}"
        out.append(name)
    return out


def _detect_first_column_name(fs: fsspec.AbstractFileSystem, url_or_path: str) -> str:
    """Open the CSV and read only the header to discover the first column name."""
    # text mode is fine; pandas reads just the header with nrows=0
    with fs.open(url_or_path, "rt") as fh:
        header_df = pd.read_csv(fh, delimiter=",", nrows=0)
    if header_df.columns.empty:
        raise ValueError(f"No columns found in {url_or_path}")
    return str(header_df.columns[0])


def subset_directory_by_time_range(
        run: CalibrationRun,
        input_directory: str,
        output_directory: str,
        date_time_range: DateTimeRange,
        max_workers: int = 4
) -> None:
    """
    Subsets the files in a directory/prefix based on a provided time range and saves
    the filtered files into an output directory, processing files in parallel.

    - Works with local dirs and S3 prefixes (s3://bucket/prefix).
    - Streams each input file directly from S3; does not download all upfront.

    :param run: The CalibrationRun instance (used for consistent logging context).
    :param input_directory: Path to the input directory.
    :param output_directory: Path to the output directory.
    :param date_time_range: DateTimeRange object specifying the time range for filtering.
    :param max_workers: Maximum number of parallel workers (default is 4 to balance S3FS I/O and system resources).
    - S3FS benefits from parallel reads, but excessive threads can cause API throttling or network congestion.
    - 4 workers provide a good balance between concurrency and avoiding excessive I/O wait.
    - If running on a high-performance instance (e.g., AWS EC2 with high network bandwidth), this value can be increased.
    - If running on a slow or metered connection, keeping this at 4 prevents potential slowdowns.
    """
    start_time = time.perf_counter()
    os.makedirs(output_directory, exist_ok=True)

    files_in = _list_dir_files(input_directory)
    file_pairs = [
        (src, os.path.join(output_directory, os.path.basename(urlparse(src).path)))
        for src in files_in
    ]

    logger.info(f"Starting subsetting for {len(file_pairs)} files in {input_directory} "
                f"with max_workers={max_workers} for Calibration Job {run.id}")

    def _process(one: tuple[str, str]) -> None:
        src, dst = one
        subset_by_time_range(run, src, dst, date_time_range)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(_process, file_pairs))

    elapsed = time.perf_counter() - start_time
    logger.info(f"Finished subsetting directory {input_directory} in {elapsed:.2f}s "
                f"for Calibration Job {run.id}")


def subset_by_time_range(
        run: CalibrationRun,
        input_file: str, output_file: str,
        date_time_range: DateTimeRange
) -> None:
    """
    Reads a CSV file, filters rows based on a time range, and writes the filtered data
    to an output file with the original column names and timezone-naive datetime values.

    Optimized to take advantage of sorted data for faster processing.
    Chunksize is optimized for **performance**, reducing disk I/O overhead.

    - Supports local files and S3 URLs.
    - Opens remote files directly via fsspec (streams line-by-line, no staging to disk).
    - Assumes the first column is the datetime column.
    - Converts all datetimes to UTC for filtering, then writes them back as naive timestamps
      to match the original format.
    - Stops reading early once the file is past the requested time range (since input is sorted).

    :param run: The CalibrationRun instance (used for logging context only).
    :param input_file: Path or URL to the input CSV file.
    :param output_file: Path to the output CSV file.
    :param date_time_range: DateTimeRange object specifying the time range for filtering.
    """
    file_basename = os.path.basename(urlparse(input_file).path)

    logger.info(f"Subsetting file {input_file} -> {output_file} with range "
                f"{date_time_range} for Calibration Job {run.id}")

    # Dynamically determine the best chunksize for performance
    chunk_size = get_performance_chunksize(input_file)
    logger.info(f"Using optimized chunksize={chunk_size} for {file_basename} for Calibration Job {run.id}")

    # Ensure the output directory exists
    os.makedirs(os.path.dirname(output_file), exist_ok=True)

    # Convert DateTimeRange boundaries to UTC Timestamps
    start_dt = pd.to_datetime(date_time_range.start_datetime, utc=True)
    end_dt = pd.to_datetime(date_time_range.end_datetime, utc=True)

    fs_in, _scheme = _get_fs_and_scheme(input_file)

    # Discover the first column name by reading just the header
    first_col = _detect_first_column_name(fs_in, input_file)

    # Now stream the file in chunks and filter
    with fs_in.open(input_file, "rt") as in_fh, open(output_file, "w") as out_fh:
        write_header = True
        # Iterator over chunks; parse the first column as dates
        reader = pd.read_csv(
            in_fh,
            delimiter=",",
            parse_dates=[0],
            chunksize=chunk_size
        )

        current_line_start = 1
        for chunk in reader:
            current_line_end = current_line_start + len(chunk) - 1

            # Normalize datetime column name
            if first_col not in chunk.columns:
                logger.error(f"Expected datetime column '{first_col}' not found in "
                             f"{file_basename} for Calibration Job {run.id}")
                raise KeyError(f"Expected datetime column '{first_col}' not found")

            chunk.rename(columns={first_col: "dateTime"}, inplace=True)

            # Ensure proper datetime dtype
            chunk["dateTime"] = pd.to_datetime(chunk["dateTime"], errors="coerce")
            if chunk["dateTime"].isna().any():
                logger.error(f"Invalid datetime values in lines {current_line_start}-{current_line_end} "
                             f"for {input_file} (Calibration Job {run.id})")
                raise ValueError("Invalid datetime values encountered")

            # Standardize to UTC
            if chunk["dateTime"].dt.tz is None:
                chunk["dateTime"] = chunk["dateTime"].dt.tz_localize("UTC")
            else:
                chunk["dateTime"] = chunk["dateTime"].dt.tz_convert("UTC")

            # Chunk-level range for fast skip/early stop
            cmin, cmax = chunk["dateTime"].min(), chunk["dateTime"].max()
            logger.debug(f"Chunk range {cmin}..{cmax} "
                         f"(lines {current_line_start}-{current_line_end}) for {input_file}")

            if cmax < start_dt:
                # Entire chunk is before the window → skip
                current_line_start += len(chunk)
                continue
            if cmin > end_dt:
                # Entire chunk is after the window → stop early
                break

            # Filter rows inside the requested time window
            keep = chunk[(chunk["dateTime"] >= start_dt) & (chunk["dateTime"] <= end_dt)].copy()
            if keep.empty:
                current_line_start += len(chunk)
                continue

            # Convert back to naive timestamps to match original format
            keep["dateTime"] = keep["dateTime"].dt.tz_convert(None)
            keep.rename(columns={"dateTime": first_col}, inplace=True)

            # Append to output file
            keep.to_csv(out_fh, index=False, header=write_header, mode="a")
            write_header = False

            current_line_start = current_line_end + 1

    logger.info(f"Finished subsetting file {input_file} -> {output_file} for Calibration Job {run.id}")


def get_performance_chunksize(file_path: str) -> int:
    """
    Dynamically determines an optimal chunksize for high-performance processing
    using a **single data row** to estimate row size (rows are uniform).

    Works for local files and remote URLs (e.g., s3://bucket/key) via fsspec.

    :param file_path: Path to the input CSV file.
    :return: Optimal chunksize for pandas.read_csv()
    """
    parsed = urlparse(file_path)
    scheme = parsed.scheme or "file"
    fs = fsspec.filesystem(scheme)

    # File size in bytes
    if scheme == "file":
        total_size = os.path.getsize(parsed.path or file_path)
    else:
        info = fs.info(file_path)
        total_size = int(info.get("size", 0))

    # Read a tiny sample (exactly 1 data row) to approximate row size
    if scheme == "file":
        with open(parsed.path or file_path, "rt") as fh:
            sample_df = pd.read_csv(fh, nrows=1)
    else:
        with fs.open(file_path, "rt") as fh:
            sample_df = pd.read_csv(fh, nrows=1)

    # Size of one data row (ignore header)
    if sample_df.empty:
        # Fallback if file is empty or malformed; keep it conservative
        return 10_000

    # Size of one data row (header excluded already)
    row_size_bytes = sample_df.memory_usage(deep=True).sum()
    if row_size_bytes <= 0:
        return 10_000

    # Estimate total rows and pick a fraction based on file size
    est_rows = max(1, int(total_size / row_size_bytes))

    if total_size < 50_000_000:  # < 50MB
        target_fraction = 0.05  # ~5%
    elif total_size < 200_000_000:  # 50–200MB
        target_fraction = 0.03  # ~3%
    else:
        target_fraction = 0.01  # ~1%

    optimal = int(est_rows * target_fraction)
    return max(10_000, min(optimal, 100_000))
