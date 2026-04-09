import copy
import logging
from typing import Any

import yaml

from calibration.models import VerificationRun
from calibration.util.caching import generate_forecast_config_yaml
from calibration.util.ngen_locations import get_verification_run_dir, VERF_CROSSWALK_NGEN_FILE, get_forecast_output_file_path, \
    get_verification_yaml_config_file, get_hindcast_output_file_name, get_hindcast_dir
from calibration.views.called_from import called_from
from calibration.views.common import format_datetime

logger = logging.getLogger(__name__)

# DO NOT MODIFY THIS TEMPLATE IN-PLACE.
# Use `copy.deepcopy(CONFIG_TEMPLATE)` to safely create per-thread instances.
CONFIG_TEMPLATE = {
    "general": {
        "steps": {
            "fetch_fcst_data": True,
            "fetch_obs_data": True,
            "pair_data": True,
            "compute_metrics": True,
            "plot_metrics": True,
        },
        "location_set_name": "",
        "location_list": [],
        "location_type": "usgs_gage",
        "variable_name": "streamflow",
        "nwm_configuration": "",
        "dataset_name": [],
        "nwm_version": [],
        "forecast_start_date": [],
        "forecast_end_date": []
    },

    "file_paths": {},

    "nwm_forecast": {
        "data_source": ""
    },

    "flow_observation": {
        "usgs": {
            "chunk_by": "month",
            "overwrite_output": True,
            "memory_per_worker_gb": 3
        }
    },

    "pair_data": {
        "overwrite": True,
        "group_size": 200
    },

    "metrics": {
        "overwrite": True,
        "library": "nwm.eval",
        "metric_subset": "all",
        "flow_threshold_categorical": 0.9,
        "flow_threshold_event": 0.9,
        "lead_times": ['all_aggregated'],
        "file_format": "parquet"
    },

    "plots": {
        "time_series": {
            "plot": True
        },
        "metric_table": {
            "plot": True
        },
        "barchart": {
            "plot": True
        }
    }

}


def create_verification_input(run: VerificationRun) -> str:
    """
    :param run: The VerificationRun instance to validate and prepare.
    :return: Path to the generated config file.
    """
    logger.info(called_from())

    is_hindcast = run.hindcast_run_id is not None
    parent_run = run.parent_run
    calibration_run = parent_run.calibration_run
    configuration_internal_name = parent_run.configuration.internal_name

    config = copy.deepcopy(CONFIG_TEMPLATE)

    # Add hard-coded file paths to YAML
    file_paths: dict[str, Any] = config['file_paths']
    file_paths['base_dir'] = get_verification_run_dir(run)
    file_paths['crosswalk_file'] = {'ngen': VERF_CROSSWALK_NGEN_FILE}
    file_paths['fcst_config_file'] = generate_forecast_config_yaml()

    if is_hindcast:
        file_paths['fcst_data_dir'] = {
            calibration_run.job_name: get_hindcast_dir(run.hindcast_run)
        }
        file_paths['fcst_data_file'] = get_hindcast_output_file_name(run.hindcast_run)
    else:
        file_paths['fcst_data_file'] = {
            calibration_run.job_name: get_forecast_output_file_path(run.forecast_run)
        }

    file_paths['output_dir'] = get_verification_run_dir(run)

    general: dict[str, Any] = config['general']

    # Override values in YAML with info from our parent run / calibration run
    general['location_set_name'] = 'usgs_' + calibration_run.gage.gage_id
    general['location_list'] = [calibration_run.gage.gage_id]
    general['nwm_configuration'] = configuration_internal_name
    general['dataset_name'] = [calibration_run.job_name]
    general['nwm_version'] = ['ngen']
    general['forecast_start_date'] = [format_datetime(parent_run.cycle_date)]
    general['forecast_end_date'] = [format_datetime(parent_run.cycle_date)]

    nwm_forecast: dict[str, Any] = config['nwm_forecast']
    nwm_forecast['data_source'] = 'hindcast' if is_hindcast else 'ngenCERF'

    metrics: dict[str, Any] = config['metrics']
    metrics['lead_times'] = ['all', '1-5', '6-10', '11-18', 'all_aggregated'] if is_hindcast else ['all_aggregated']

    plots: dict[str, Any] = config['plots']
    if is_hindcast:
        plots['time_series']['lead_times'] = [1, 6, 12, 18]
        # plots['time_series']['reference_times'] = ['2025-08-20 00:00:00', '2025-08-21 00:00:00', '2025-08-22 00:00:00']
        plots['metric_table']['lead_times'] = [1, 10, '1-5', 'all_aggregated']
        plots['barchart']['lead_times'] = [1, 5, 10, 18, '1-5', '6-10', '11-18', 'all_aggregated']
        plots['barchart']['metric_subset'] = ['KGE', 'NSE', 'CORR', 'NNSE']

    # -----------------------------
    # FILE WRITE PHASE
    # -----------------------------
    config_location = get_verification_yaml_config_file(run)

    with open(config_location, 'w', encoding='utf-8') as config_file:
        yaml.safe_dump(
            config,
            config_file,
            default_flow_style=False,
            sort_keys=False
        )
        logger.info(f"Writing new YAML file to {config_location}")

    return config_location
