import logging
import os
from typing import Literal

from django.conf import settings

from calibration.enums import ValidationType
from calibration.enums_vanilla import SecondaryDataEnum
from calibration.models import CalibrationRun, ValidationRun, ForecastRun, ColdStartRun, VerificationRun
from calibration.models.hindcast_run import HindcastRun
from calibration.util.file_util import get_single_file
from cerfServer.settings import NGEN_ENVIRONMENT

logger = logging.getLogger(__name__)

static_dirs = [
    NWM_RETROSPECTIVE_DIR := os.path.join(settings.NGEN_STATIC_DIR, 'nwm_retrospective'),
    NGEN_MODULE_PARAMETERS := os.path.join(settings.NGEN_STATIC_DIR, 'module_parameter_files'),
    BMI_FORCING_TEMPLATES := os.path.join(settings.NGEN_STATIC_DIR, 'bmi_forcing_templates'),
    VERF_DATA := os.path.join(settings.NGEN_STATIC_DIR, 'verification_data')
]

files = [
    NGEN_EXE := os.path.join(settings.NGEN_REPO_ROOT, 'cmake_build', 'ngen'),
    PARALLEL_NGEN_EXE := os.path.join(settings.NGEN_REPO_ROOT, 'cmake_build', 'ngen'),
    PARTITION_GENERATOR_EXE := os.path.join(settings.BASE_DIR, 'partitionGenerator'),
    CFE_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'cfe', 'cmake_build', 'libcfebmi.so'),
    SLOTH_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'sloth', 'cmake_build', 'libslothmodel.so'),
    TOPMD_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'topmodel', 'cmake_build', 'libtopmodelbmi.so'),
    NOAH_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'noah-owp-modular', 'cmake_build', 'libsurfacebmi.so'),
    SFT_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'SoilFreezeThaw', 'cmake_build', 'libsftbmi.so'),
    SMP_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'SoilMoistureProfiles', 'cmake_build', 'libsmpbmi.so'),
    LASAM_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'LASAM', 'cmake_build', 'liblasambmi.so'),
    PET_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'evapotranspiration', 'evapotranspiration', 'cmake_build', 'libpetbmi.so'),
    SNOW17_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'snow17', 'cmake_build', 'libsnow17bmi.so'),
    SAC_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'sac-sma', 'cmake_build', 'libsacbmi.so'),
    UEB_LIB := os.path.join(settings.NGEN_REPO_ROOT, 'extern', 'ueb-bmi', 'cmake_build', 'src', 'libbmiuebcxx.so'),
    VERF_CROSSWALK_NGEN_FILE := os.path.join(VERF_DATA, 'usgs_ngen_crosswalk_all_domains.parquet'),
]


def check_files():
    # If we are running locally,then ngen and ngen-cal files must be on our machine
    if NGEN_ENVIRONMENT == NGEN_ENVIRONMENT.LOCAL:
        for file in files:
            if not os.path.isfile(file):
                logger.warning(f'{file} does not exist')

    for directory in static_dirs:
        if not os.path.isdir(directory):
            logger.warning(f'{directory} does not exist')
        elif not os.listdir(directory):
            logger.warning(f'{directory} is empty')


# Construct the directory where the Input/Output is
def get_gage_dir(run: CalibrationRun) -> str:
    objective_function_name = run.objective_function.name if run.objective_function else 'None'
    optimization_name = run.optimization.name if run.optimization else 'None'
    return os.path.join(
        run.job_data_dir,
        f"{objective_function_name.lower()}_{optimization_name.lower()}",
        run.job_name,
        run.gage.gage_id
    )


def get_realization_file_path(run: CalibrationRun) -> str:
    return os.path.join(get_gage_dir(run), f"{run.gage.gage_id}_realization_config_bmi_calib.json")


def get_forcing_filename_pattern() -> str:
    return r"^cat-\d+\.csv$"


# def get_bmi_config_dir_for_job(run: CalibrationRun) -> str:
#     return os.path.join(run.job_data_dir, 'bmi_config')
#
#
# def get_bmi_config_dir_for_module(run: CalibrationRun, module_name: str) -> str:
#     return os.path.join(get_bmi_config_dir_for_job(run), module_name.lower())


# def get_bmi_config_key(module_name: str) -> str:
#     return f"{module_name.lower().replace('-', '_')}_bmi_dir"


# Job-specific forcing directory
def get_forcing_dir_for_job(run: CalibrationRun) -> str:
    return os.path.join(run.job_data_dir, 'forcing')


# Job-specific observation directory
def get_observational_dir_for_job(run: CalibrationRun) -> str:
    return os.path.join(run.job_data_dir, 'observation')


def get_observational_filename(run: CalibrationRun) -> str:
    return f"{run.gage.gage_id}_hourly_discharge.csv"


# Job-specific observation file
def get_observational_file_for_job(run: CalibrationRun) -> str:
    return os.path.join(get_observational_dir_for_job(run), get_observational_filename(run))


def get_geopackage_dir_for_job(run: CalibrationRun) -> str:
    return os.path.join(run.job_data_dir, 'geopackage')


def get_geopackage_file_path(run: CalibrationRun) -> str | None:
    return get_single_file(get_geopackage_dir_for_job(run))


def get_ngen_stdout_log_filename() -> str:
    return 'ngen_stdout_stderr.log'


def get_ngen_log_dir(run: CalibrationRun) -> str:
    return os.path.join(get_gage_dir(run), 'logs')


def get_calibration_ngen_logs(run: CalibrationRun) -> str:
    return os.path.join(get_output_dir(run), 'logs')


def get_input_dir(run: CalibrationRun) -> str:
    return os.path.join(get_gage_dir(run), 'Input')


def get_output_dir(run: CalibrationRun) -> str:
    return os.path.join(get_gage_dir(run), 'Output')


def get_output_calibration_run_dir(run: CalibrationRun) -> str:
    return os.path.join(get_output_dir(run), 'Calibration_Run')


def get_output_validation_run_dir(run: CalibrationRun) -> str:
    return os.path.join(get_output_dir(run), 'Validation_Run')


def get_output_validation_plot_dir(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), 'Plot_Valid')


def get_output_validation_iteration_plot_dir(run: CalibrationRun, iteration_num: int, worker_name: str) -> str:
    return os.path.join(get_output_validation_run_dir(run), f'Plot_Valid_{worker_name}_iter{iteration_num}')


def get_output_cold_start_run_dir(run: CalibrationRun) -> str:
    return os.path.join(get_output_dir(run), 'Model_State_Run', 'Cold_Start_Run')


def get_output_forecast_run_dir(run: CalibrationRun) -> str:
    return os.path.join(get_output_dir(run), 'Forecast_Run')


def get_output_hindcast_run_dir(run: CalibrationRun) -> str:
    return os.path.join(get_output_dir(run), 'Hindcast_Run')


def get_full_worker_filename(short_worker_name: str) -> str:
    return f"ngen_{short_worker_name}_worker"


def get_calibration_worker_path(run: CalibrationRun, short_worker_name: str) -> str:
    return os.path.join(get_output_calibration_run_dir(run), get_full_worker_filename(short_worker_name))


def get_validation_output_valid(run: CalibrationRun, full_worker_name: str) -> str:
    return os.path.join(get_output_validation_run_dir(run), full_worker_name, 'Output_Valid')


def get_metrics_iteration_csv(run: CalibrationRun) -> str:
    return f"{run.gage.gage_id}_metrics_iteration.csv"


def get_output_last_iteration_csv(run: CalibrationRun) -> str:
    return f"{run.gage.gage_id}_output_last_iteration.csv"


def get_output_last_iteration_file(run: CalibrationRun, worker_dir: str) -> str:
    return os.path.join(worker_dir, get_output_last_iteration_csv(run))


def get_output_best_iteration_csv(run: CalibrationRun) -> str:
    return f"{run.gage.gage_id}_output_best_iteration.csv"


def get_output_best_iteration_file(run: CalibrationRun, worker_dir: str) -> str:
    return os.path.join(worker_dir, get_output_best_iteration_csv(run))


def get_output_valid_control_csv(run: CalibrationRun) -> str:
    return f"{run.gage.gage_id}_output_valid_control.csv"


def get_output_valid_control_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), get_output_valid_control_csv(run))


def get_output_valid_best_csv(run: CalibrationRun) -> str:
    return f"{run.gage.gage_id}_output_valid_best.csv"


def get_output_valid_best_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), get_output_valid_best_csv(run))


def get_output_valid_iteration_file(run: CalibrationRun, worker_name: str, iteration_num: int) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"{run.gage.gage_id}_output_valid_{worker_name}_iter{iteration_num}.csv")


def get_output_iteration_csv(run: CalibrationRun, iteration_num: int) -> str:
    return f"{run.gage.gage_id}_output_iteration_{iteration_num:04d}.csv"


def get_output_iteration_file(run: CalibrationRun, iteration_num: int, worker_dir: str) -> str:
    return os.path.join(worker_dir, 'Output_Iteration', get_output_iteration_csv(run, iteration_num))


def get_metrics_iteration_file(run: CalibrationRun, short_worker_name: str) -> str:
    return os.path.join(get_calibration_worker_path(run, short_worker_name), get_metrics_iteration_csv(run))


def get_cost_hist_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_calibration_run_dir(run), f"{run.gage.gage_id}_cost_hist.csv")


def get_metrics_iteration_file_from_worker_dir(run: CalibrationRun, worker_dir: str) -> str:
    return os.path.join(worker_dir, get_metrics_iteration_csv(run))


def get_params_iteration_file(run: CalibrationRun, short_worker_name: str) -> str:
    return os.path.join(get_calibration_worker_path(run, short_worker_name), f"{run.gage.gage_id}_params_iteration.csv")


def get_objective_log_best_file(run: CalibrationRun, short_worker_name: str) -> str:
    return os.path.join(get_calibration_worker_path(run, short_worker_name), f"{run.gage.gage_id}_objective_log.txt")


def get_calibration_stdout_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_calibration_run_dir(run), 'cal-mgr_calibration_stdout.log')


def get_calibration_performance_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_calibration_run_dir(run), 'cal-mgr_calibration_performance.log')


def get_global_best_params_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_calibration_run_dir(run), f"{run.gage.gage_id}_global_best_params.csv")


def get_calibration_input_file(run: CalibrationRun) -> str:
    return os.path.join(get_input_dir(run), f"{run.gage.gage_id}_config_calib.yaml")


def get_validation_best_input_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"{run.gage.gage_id}_config_valid_best.yaml")


def get_validation_best_stdout_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), 'cal-mgr_validation_best_stdout.log')


def get_validation_control_stdout_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), 'cal-mgr_validation_control_stdout.log')


def get_validation_iteration_stdout_file(run: CalibrationRun, worker_name: str, iteration_num: int) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"cal-mgr_validation_{worker_name}_iter{iteration_num}_stdout.log")


def get_cold_start_dir(cold_start_run: ColdStartRun) -> str:
    return os.path.join(get_output_cold_start_run_dir(cold_start_run.calibration_run), f'cold_start_{cold_start_run.id}')


def get_cold_start_state(cold_start_run: ColdStartRun) -> str:
    return os.path.join(get_cold_start_dir(cold_start_run), f'state_save')


def get_forecast_dir(forecast_run: ForecastRun) -> str:
    return os.path.join(get_output_forecast_run_dir(forecast_run.calibration_run), f'forecast_{forecast_run.id}')


def get_hindcast_dir(hindcast_run: HindcastRun) -> str:
    return os.path.join(get_output_hindcast_run_dir(hindcast_run.calibration_run), f'hindcast_{hindcast_run.id}')


def get_cold_start_output_dir(cold_start_run: ColdStartRun) -> str:
    return os.path.join(get_cold_start_dir(cold_start_run), 'Output')


def get_forecast_output_dir(forecast_run: ForecastRun) -> str:
    return os.path.join(get_forecast_dir(forecast_run), 'Output')


def get_forecast_forcing_config_file(forecast_run: ForecastRun) -> str:
    return os.path.join(get_forecast_dir(forecast_run), f'forecast_forcing_config.yaml')


def get_cold_start_output_file(run: ForecastRun | HindcastRun) -> str | None:
    if isinstance(run, ForecastRun) and not run.cold_start_run:
        return None
    return os.path.join(get_cold_start_output_dir(run.cold_start_run), f'{run.calibration_run.gage.gage_id}_output.csv')


def get_forecast_output_file(forecast_run: ForecastRun) -> str:
    return os.path.join(get_forecast_output_dir(forecast_run), f'{forecast_run.calibration_run.gage.gage_id}_output.csv')


def get_hindcast_output_file(hindcast_run: HindcastRun, iteration: int) -> str:
    return os.path.join(get_hindcast_dir(hindcast_run), f'hindcast_{iteration}', 'Output',
                        f'{hindcast_run.calibration_run.gage.gage_id}_output.csv')


def get_cold_start_stdout_file(cold_start_run: ColdStartRun) -> str:
    return os.path.join(get_cold_start_dir(cold_start_run), f'cold_start_{cold_start_run.id}_stdout.log')


def get_forecast_stdout_file(forecast_run: ForecastRun) -> str:
    return os.path.join(get_forecast_dir(forecast_run), f'forecast_{forecast_run.id}_stdout.log')


def get_hindcast_stdout_file(hindcast_run: HindcastRun) -> str:
    return os.path.join(get_hindcast_dir(hindcast_run), f'hindcast_{hindcast_run.id}_stdout.log')


def get_hindcast_ngen_stdout_file(hindcast_run: HindcastRun) -> str:
    return os.path.join(get_hindcast_dir(hindcast_run), 'ngen_stdout_stderr.log')


def get_hindcast_ngen_stdout_file(hindcast_run: HindcastRun) -> str:
    return os.path.join(get_hindcast_dir(hindcast_run), 'ngen_stdout_stderr.log')


def get_cold_start_ngen_log_dir(cold_start_run: ColdStartRun) -> str:
    return os.path.join(get_cold_start_dir(cold_start_run), 'logs')


def get_forecast_ngen_log_dir(forecast_run: ForecastRun) -> str:
    return os.path.join(get_forecast_dir(forecast_run), 'logs')


def get_hindcast_ngen_log_dir(hindcast_run: HindcastRun) -> str:
    return os.path.join(get_hindcast_dir(hindcast_run), 'logs')


def get_cold_start_performance_file(cold_start_run: ColdStartRun) -> str:
    return os.path.join(get_cold_start_dir(cold_start_run), 'cold_start_performance.log')


def get_forecast_performance_file(forecast_run: ForecastRun) -> str:
    return os.path.join(get_forecast_dir(forecast_run), 'forecast_performance.log')


def get_hindcast_performance_file(hindcast_run: HindcastRun) -> str:
    return os.path.join(get_hindcast_dir(hindcast_run), 'forecast_performance.log')


def get_forecast_realization_file(forecast_run: ForecastRun) -> str:
    return os.path.join(get_forecast_dir(forecast_run), f'{forecast_run.calibration_run.gage.gage_id}_realization_config_bmi_fcst.json')


def get_cold_start_realization_file(cold_start_run: ColdStartRun) -> str:
    return os.path.join(get_cold_start_dir(cold_start_run), f'{cold_start_run.calibration_run.gage.gage_id}_realization_config_bmi_cold_start.json')


def get_verification_run_dir(run: VerificationRun) -> str:
    return os.path.join(get_forecast_dir(run.forecast_run), 'Verification_Run', f'verification_{run.id}')


def get_verification_yaml_config_file(run: VerificationRun) -> str:
    return os.path.join(get_verification_run_dir(run), f'verification_{run.id}_config.yaml')


def get_verification_stdout_file(run: VerificationRun) -> str:
    return os.path.join(get_verification_run_dir(run), f'verification_{run.id}_stdout.log')


def get_verification_performance_file(run: VerificationRun) -> str:
    return os.path.join(get_verification_run_dir(run), 'verification_performance.log')


def get_validation_performance_file(run: CalibrationRun, worker_name: str, iteration_num: int) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"cal-mgr_validation_{worker_name}_iter{iteration_num}_performance.log")


def get_swe_netcdf_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"{run.gage.gage_id}_swe.nc")


def get_soil_moisture_netcdf_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"{run.gage.gage_id}_soil_moisture.nc")


def get_validation_special_performance_file(run: CalibrationRun,
                                            validation_type: Literal[ValidationType.VALID_BEST, ValidationType.VALID_CONTROL]) -> str:
    validation_type_str = validation_type.value.split('_')[1].lower()
    return os.path.join(get_output_validation_run_dir(run), f"cal-mgr_validation_{validation_type_str}_performance.log")


def get_calibration_git_info_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_calibration_run_dir(run), f"git_info_calibration.json")


def get_validation_special_git_info_file(run: ValidationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run.calibration_run), f"git_info_{run.validation_type}.json")


def get_validation_iteration_git_info_file(run: ValidationRun, worker_name: str, iteration_num: int):
    return os.path.join(get_output_validation_run_dir(run.calibration_run), f"git_info_{worker_name}_iter{iteration_num}.json")


def get_forecast_git_info_file(forecast_run: ForecastRun) -> str:
    return os.path.join(get_forecast_dir(forecast_run), "git_info_forecast.json")


def get_hindcast_git_info_file(hindcast_run: HindcastRun) -> str:
    return os.path.join(get_hindcast_dir(hindcast_run), "git_info_hindcast.json")


def get_cold_start_git_info_file(cold_start_run: ColdStartRun) -> str:
    return os.path.join(get_cold_start_dir(cold_start_run), "git_info_forecast.json")


def get_verification_git_info_file(run: VerificationRun) -> str:
    return os.path.join(get_verification_run_dir(run), "git_info_verification.json")


def get_validation_metrics_valid_best_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"{run.gage.gage_id}_metrics_valid_best.csv")


def get_validation_control_input_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"{run.gage.gage_id}_config_valid_control.yaml")


def get_validation_metrics_valid_control_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"{run.gage.gage_id}_metrics_valid_control.csv")


def get_validation_metrics_nwm_retrospective_file(run: CalibrationRun) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"{run.gage.gage_id}_metrics_nwm_retro.csv")


def get_validation_metrics_valid_iteration_file(run: CalibrationRun, worker_name: str, iteration_num: int) -> str:
    return os.path.join(get_output_validation_run_dir(run), f"{run.gage.gage_id}_metrics_valid_{worker_name}_iter{iteration_num}.csv")


def get_ngen_logging_basename() -> str:
    return "ngen_logging"


def get_ngen_logging_file(run: CalibrationRun | ValidationRun | ForecastRun | HindcastRun | ColdStartRun, import_flag: bool = False) -> str:
    calibration_run = run if isinstance(run, CalibrationRun) else run.calibration_run
    job_type = run.__class__.__name__.removesuffix('Run').lower()
    file_name = f"{get_ngen_logging_basename()}_{job_type}_{run.id}{'_import' if import_flag else ''}.json"
    return os.path.join(calibration_run.job_data_dir, file_name)


def get_swe_timeseries_png_filepath(validation_run: ValidationRun) -> str:
    """
    Returns the full file path for the SWE timeseries PNG image.

    :param validation_run: The ValidationRun object.
    :return: A string representing the path to the PNG file.
    """
    return os.path.join(get_secondary_plot_dir(validation_run, SecondaryDataEnum.SWE), 'swe_timeseries.png')


def get_swe_timeseries_data_filepath(validation_run: ValidationRun) -> str:
    """
    Returns the full file path for the SWE timeseries CSV data file.

    :param validation_run: The ValidationRun object.
    :return: A string representing the path to the CSV file.
    """
    filename = (
        'swe_timeseries_best.csv'
        if validation_run.validation_type == ValidationType.VALID_BEST.value
        else f'swe_timeseries_{validation_run.worker_name}_iter{validation_run.iteration_num}'
    )
    return os.path.join(get_output_validation_run_dir(validation_run.calibration_run), filename)


def get_soil_moisture_timeseries_png_filepath(validation_run: ValidationRun) -> str:
    return os.path.join(get_secondary_plot_dir(validation_run, SecondaryDataEnum.SOIL_MOISTURE), 'soil_moisture_timeseries.png')


def get_soil_moisture_timeseries_data_filepath(validation_run: ValidationRun) -> str:
    filename = (
        'soil_moisture_timeseries_best.csv'
        if validation_run.validation_type == ValidationType.VALID_BEST.value
        else f'soil_moisture_timeseries_{validation_run.worker_name}_iter{validation_run.iteration_num}'
    )
    return os.path.join(get_output_validation_run_dir(validation_run.calibration_run), filename)


def get_precipitation_timeseries_data_filepath(calibration_run: CalibrationRun) -> str:
    return os.path.join(get_output_calibration_run_dir(calibration_run), 'precipitation_timeseries.csv')


def get_secondary_plot_dir(run: ValidationRun, data_type: SecondaryDataEnum) -> str:
    """
    Determines and returns the appropriate plot directory for a given validation run.
    """
    if run.validation_type == ValidationType.VALID_ITERATION.value:
        base_dir = get_output_validation_iteration_plot_dir(run.calibration_run, run.iteration_num, run.worker_name)
    else:
        base_dir = get_output_validation_plot_dir(run.calibration_run)

    plot_dir = os.path.join(base_dir, str(data_type.value))
    os.makedirs(plot_dir, exist_ok=True)
    return plot_dir
