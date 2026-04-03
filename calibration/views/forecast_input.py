import copy
import logging

from calibration.enums import StatusEnum
from calibration.models import ForecastRun, ColdStartRun
from calibration.models.hindcast_run import HindcastRun
from calibration.util.ngen_locations import get_forecast_dir, BMI_FORCING_TEMPLATES, get_cold_start_dir, get_hindcast_dir
from calibration.views.called_from import called_from
from calibration.views.common import format_datetime, join_with_or, readonly_transaction
from calibration.views.ngen_cal_input import build_config
from cerfServer.settings import NGEN_BMI_FORCING_WORK_DIR

logger = logging.getLogger(__name__)

# DO NOT MODIFY THIS TEMPLATE IN-PLACE.
# Use `copy.deepcopy(CONFIG_TEMPLATE)` to safely create per-thread instances.
CONFIG_TEMPLATE = {

    "Forcing": {
        "forcing_provider": "bmi",
        "root_dir": NGEN_BMI_FORCING_WORK_DIR,
        "forcing_configuration": "",
        "cycle_datetime": None,
        "forcing_template_dir": BMI_FORCING_TEMPLATES,
        "cold_start_datetime": None
    }
}


def create_forecast_input(run: ForecastRun | HindcastRun | ColdStartRun) -> str:
    """
    :param run: The ForecastRun, HindcastRun, or ColdStartRun instance to validate and prepare.
    :return: Path to the generated config file.
    :raises ValueError: If run is not in an allowed status.
    """
    logger.info(called_from())

    if isinstance(run, ForecastRun):
        job_name = 'Forecast'
        config_name = 'forecast-input.config'
        config_location = get_forecast_dir(run)
    elif isinstance(run, HindcastRun):
        job_name = 'Hindcast'
        config_name = 'hindcast-input.config'
        config_location = get_hindcast_dir(run)
    else:
        job_name = 'Cold Start'
        config_name = 'cold-start-input.config'
        config_location = get_cold_start_dir(run)

    # -----------------------------
    # READ-ONLY PHASE
    # -----------------------------
    with readonly_transaction():
        allowed_status_names = [StatusEnum.SUBMITTED.value]
        if run.status.name not in allowed_status_names:
            raise ValueError(
                f'{job_name} Job {run.id} is not in an allowed status: '
                f'{join_with_or(allowed_status_names)}. '
                f'Current status: {run.status.name}'
            )

    # -----------------------------
    # BUILD CONFIG
    # -----------------------------
    config = copy.deepcopy(CONFIG_TEMPLATE)
    forcing = config['Forcing']

    forcing['forcing_configuration'] = run.configuration.internal_name
    forcing['cycle_datetime'] = format_datetime(run.cycle_date)

    if isinstance(run, ColdStartRun):
        forcing['cold_start_datetime'] = format_datetime(run.cold_start_date)

    return build_config(config, config_location, config_name)
