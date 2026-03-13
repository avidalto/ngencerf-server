import logging
import os
import sys

from django.apps import AppConfig
from django.conf import settings

from calibration.util.cloud_util import check_aws_credentials, S3CredentialsExpired
from calibration.util.db_diagnostics import patch_ensure_connection_with_diagnostics
from calibration.util.git_util import print_git_info_all
from calibration.views.mpi_rules import log_mpi_rules

logger = logging.getLogger(__name__)


def print_db_info():
    db_info = settings.DATABASES['default']
    logger.info(f"Database Engine: {db_info['ENGINE']}")
    logger.info(f"Database Name: {db_info['NAME']}")
    logger.info(f"Database URL: {db_info['HOST']}:{db_info['PORT']}")
    logger.info(f"Database User: {db_info['USER']}")


def log_worker_info():
    pid = os.getpid()
    argv = " ".join(sys.argv)
    logger.info(f"Worker PID: {pid} | argv: {argv}")


def print_banner():
    banner = """

███╗   ██╗ ██████╗ ███████╗███╗   ██╗ ██████╗███████╗██████╗ ███████╗
████╗  ██║██╔════╝ ██╔════╝████╗  ██║██╔════╝██╔════╝██╔══██╗██╔════╝
██╔██╗ ██║██║  ███╗█████╗  ██╔██╗ ██║██║     █████╗  ██████╔╝█████╗  
██║╚██╗██║██║   ██║██╔══╝  ██║╚██╗██║██║     ██╔══╝  ██╔══██╗██╔══╝ta  
██║ ╚████║╚██████╔╝███████╗██║ ╚████║╚██████╗███████╗██║  ██║██║     
╚═╝  ╚═══╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝ ╚═════╝╚══════╝╚═╝  ╚═╝╚═╝     

███████╗███████╗██████╗ ██╗   ██╗███████╗██████╗                     
██╔════╝██╔════╝██╔══██╗██║   ██║██╔════╝██╔══██╗                    
███████╗█████╗  ██████╔╝██║   ██║█████╗  ██████╔╝                    
╚════██║██╔══╝  ██╔══██╗╚██╗ ██╔╝██╔══╝  ██╔══██╗                    
███████║███████╗██║  ██║ ╚████╔╝ ███████╗██║  ██║                    
╚══════╝╚══════╝╚═╝  ╚═╝  ╚═══╝  ╚══════╝╚═╝  ╚═╝ """

    logger.info(banner)


class CalibrationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'calibration'

    def ready(self) -> None:
        # Must import here even though we're not referencing it
        from calibration.views.email_verification_views import mark_email_verified
        _ = mark_email_verified  # Ensures import is used

        # -------------------------------------------------------------
        # Detect dev server or gunicorn
        # -------------------------------------------------------------
        running_dev_server = any(cmd in sys.argv for cmd in ('runserver', 'runsslserver'))
        running_gunicorn = any('gunicorn' in arg for arg in sys.argv)

        # -------------------------------------------------------------
        # Banner + basic info
        # -------------------------------------------------------------
        if running_dev_server or running_gunicorn:
            # Fail fast. Do NOT let the server limp along with bad creds.
            try:
                check_aws_credentials()
            except S3CredentialsExpired:
                logger.error("AWS credential sanity check failed at startup")
                raise

            print_banner()
        else:
            # Management command
            cmd = sys.argv[1] if len(sys.argv) > 1 else os.path.basename(sys.argv[0])
            logger.info(f'*** Running {cmd}')

        logger.info(f'Environment: {settings.NGEN_ENVIRONMENT_STR}')
        log_worker_info()

        # ------------------------------------------------------------------
        # ALWAYS display Git, DB and environment info
        # ------------------------------------------------------------------
        logger.info('')
        print_git_info_all()

        logger.info('')
        print_db_info()
        logger.info('')

        logger.info(f'NGWPC Enterprise Data Server url: {settings.ENTERPRISE_DATA_URL}\n')
        logger.info(f'NGEN_CAL_MOUNT_POINT: {settings.NGEN_CAL_MOUNT_POINT}')
        logger.info(f'NGEN_STATIC_DIR: {settings.NGEN_STATIC_DIR}')
        logger.info(f'NGENCERF_ARCHIVE_S3_PATH: {settings.NGENCERF_ARCHIVE_S3_PATH}')
        logger.info(f'DJANGO DEBUG: {settings.DEBUG}')
        logger.info(f'USE_BMI_FORCING: {settings.USE_BMI_FORCING}')
        log_mpi_rules()

        from calibration.util.ngen_locations import check_files

        check_files()

        # Diagnostics wrapper for DB
        patch_ensure_connection_with_diagnostics()
