# This is a template for local_settings.py.
# It should be copied to local_settings.py and add your custom local settings for your environment (e.g., staging, production, etc.)
# local_settings.py should not be checked into git.  Only this template.
# The values in this template are suitable for use in Development.
# Any Django or other properties can be added to this file.  For secure values, such as passwords,
# you can reference os.getenv and store the value in the .env file or in the environment
import os

from cerfServer.settings import LOGGING

print('Loading local settings from', __name__)

ALLOWED_HOSTS = ['.localhost', '127.0.0.1', '10.6.2.27']

# SQL logging
LOGGING['loggers']['django.db.backends']['level'] = 'DEBUG'

# Calibration logging
LOGGING['loggers']['calibration']['level'] = 'DEBUG'

# Regular logging
LOGGING['root']['level'] = 'INFO'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('CERF_SERVER_DATABASE_NAME', 'postgres'),
        'USER': os.getenv('CERF_SERVER_DATABASE_USER', 'postgres'),
        'PASSWORD': os.getenv('CERF_SERVER_DATABASE_PASSWORD', 'postgres'),
        'HOST': os.getenv('CERF_SERVER_DATABASE_HOST', 'localhost'),
        # Blackhole ip for timeout testing
        # 'HOST': '10.255.255.1',
        'PORT': 5432,
        'CONN_MAX_AGE': 60,
        'OPTIONS': {
            'connect_timeout': 10,
            'options': '-c statement_timeout=10000ms',
            #'sslmode': 'require',
            'sslmode': os.getenv('CERF_SERVER_DATABASE_SSLMODE', 'disable'),
            # 'sslrootcert': /ngencerf/aws_cert/global-bundle.pem',
        }
    }
}
