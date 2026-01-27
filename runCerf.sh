#! /bin/bash

#=======================================================================
# Script must be sourced for 'activate' mode
#=======================================================================
if [[ "$1" == "activate" ]] && [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "Error: This script must be sourced, not executed, when using the 'activate' command."
    echo "Use 'source ./runCerf.sh activate' to activate the virtual environment."
    exit 1
fi

#=======================================================================
# Resolve script directory
#=======================================================================
SCRIPT_DIR="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"

#=======================================================================
# Load environment variables
#   - cerfserver.env is ALWAYS loaded
#   - .env and .env-override are loaded ONLY when NOT in Docker
#=======================================================================
set -a  # auto-export

# Always load cerfserver.env
source "$SCRIPT_DIR/cerfserver.env"

#-----------------------------------------------------------------------
# Detect Docker (AFTER cerfserver.env is loaded)
#-----------------------------------------------------------------------
IN_DOCKER=false
if [ "${CERF_VENV}" = "Docker" ]; then
    IN_DOCKER=true
fi
readonly IN_DOCKER

echo "IN_DOCKER=$IN_DOCKER"

#-----------------------------------------------------------------------
# Load optional local-only env files
#-----------------------------------------------------------------------
if [ "$IN_DOCKER" = false ]; then
    echo "Non-Docker environment: checking for local env files"

    ENV_FILE="$SCRIPT_DIR/cerfServer/.env"
    ENV_OVERRIDE_FILE="$SCRIPT_DIR/cerfServer/.env-override"

    if [ -f "$ENV_FILE" ]; then
        echo "Loaded env file: $ENV_FILE"
        source "$ENV_FILE"
    else
        echo "WARNING: env file not found: $ENV_FILE"
    fi

    # When running locally, the override file usually will not exist, so we won't issue an error
    if [ -f "$ENV_OVERRIDE_FILE" ]; then
        echo "Loaded env override file: $ENV_OVERRIDE_FILE"
        source "$ENV_OVERRIDE_FILE"
    fi
else
    echo "Docker environment detected: skipping local env files (.env, .env-override)"
fi

set +a

#=======================================================================
# Validate RUN_CERF_FLAG_DIRECTORY
#=======================================================================
if [ -z "${RUN_CERF_FLAG_DIRECTORY}" ]; then
    echo "WARNING: RUN_CERF_FLAG_DIRECTORY is not set in cerfserver.env; defaulting to ./"
    RUN_CERF_FLAG_DIRECTORY="./"
fi

# Normalize: remove any trailing slash so we don't end up with // in paths
RUN_CERF_FLAG_DIRECTORY="${RUN_CERF_FLAG_DIRECTORY%/}"

# Use the same directory variable for cerfServer
cerfServer="$SCRIPT_DIR"

#=======================================================================
# Bootstrap logging
#=======================================================================
mkdir -p "$cerfServer/logs"
LOGFILE_DEV="$cerfServer/logs/ngencerf.log"
printf "\n------- Server starting at %s --------\n" "$(date)" | tee -a "$LOGFILE_DEV"

# Save original stdout/stderr
exec 3>&1 4>&2
# Redirect everything to the logfile
exec > >(tee -a "$LOGFILE_DEV") 2>&1

#=======================================================================
# Function: ensure_virtualenv
#   - If CERF_VENV is empty or “Docker”, do nothing
#   - If the directory "$cerfServer/$CERF_VENV" does not exist, create it
#   - Activate that venv so “python3” and “pip” later refer to the venv
#=======================================================================
ensure_virtualenv() {
    if [ -n "${CERF_VENV}" ] && [ "$IN_DOCKER" = false ]; then
        VENV_PATH="$cerfServer/${CERF_VENV}"

        if [ ! -d "$VENV_PATH" ]; then
            echo "Virtual environment not found at $VENV_PATH. Creating it..."
            python3.11 -m venv "$VENV_PATH"
        fi

        # shellcheck disable=SC1090
        source "$VENV_PATH/bin/activate"
        echo "Activated virtual environment at $VENV_PATH"
    fi
}

#=======================================================================
# Function: check_aws_credentials_early
#   - Uses AWS CLI (STS) for a fast sanity check
#   - Skips if running in Docker
#   - Skips if aws CLI is not installed
#   - Fails startup if credentials are invalid/expired
#   - Logs the resolved identity ARN on success
#=======================================================================
check_aws_credentials_early() {
    # Skip in Docker.  We'll rely on the server check
    if [ "$IN_DOCKER" = true ]; then
        echo "Skipping AWS credential check (Docker environment)"
        return 0
    fi

    # Skip if AWS CLI not installed
    if ! command -v aws >/dev/null 2>&1; then
        echo "Skipping AWS credential check (aws CLI not found)"
        return 0
    fi

    echo "Checking AWS credentials (early STS sanity check)..."

    if aws sts get-caller-identity \
        --output json \
        --cli-connect-timeout 3 \
        --cli-read-timeout 3 \
        >/dev/null 2>&1
    then
        IDENTITY=$(aws sts get-caller-identity --output text --query 'Arn' 2>/dev/null)
        echo "AWS credentials OK ($IDENTITY)"
        return 0
    else
        echo "ERROR: AWS credentials are missing, expired, or invalid"

         # If this script is being sourced, don't kill the caller's shell.
        if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
            return 2
        fi

        exit 2
    fi
}


#=======================================================================
# Function: run_manage_command
#   - Temporarily un-redirect stdout/stderr for interactive output
#   - Runs “python manage.py <args…>”
#   - Then re-redirects stdout/stderr back to the logfile
#=======================================================================
run_manage_command() {
    echo "Running manage.py $*"

    # Temporarily restore original stdout/stderr
    exec 1>&3 2>&4
    python "$SCRIPT_DIR/manage.py" "$@"
    local status=$?   # Capture the Python exit code

    # Re-redirect to the logfile
    exec > >(tee -a "$LOGFILE_DEV") 2>&1

    if [ $status -ne 0 ]; then
        echo "manage.py $* failed with exit code $status"
    fi
    return $status
}

#=======================================================================
# Special case: if the first argument is “manage”, just run manage.py <args>
#=======================================================================
if [ "$1" == "manage" ]; then
    shift
    ensure_virtualenv  # Activates and creates virtualenv if needed
    run_manage_command "$@"
    exit $?
fi

#=======================================================================
# Special case: if the first argument is "activate", just activate the venv and return
#=======================================================================
if [ "$1" == "activate" ]; then
    ensure_virtualenv  # Activates and creates virtualenv if needed
    echo "Virtual environment activated. You can now run Python commands in this environment."
    # Return to stop further execution but not exit the terminal
    return 0
fi

check_aws_credentials_early
echo
echo --------------------------------------------------------


#=======================================================================
# Parse flags
#   --load-gages
#   auto_reload   (enables Django auto-reloader; disables --noreload)
#=======================================================================
LOAD_GAGE_DATA=false
AUTO_RELOAD=false

for arg in "$@"; do
  case $arg in
    --load-gages)
      LOAD_GAGE_DATA=true
      ;;
    auto_reload)
      AUTO_RELOAD=true
      ;;
  esac
done

#=======================================================================
# Function: generate_git_info
#   - Writes <repo>_git_info.json with commit metadata similar to how the Dockerfile does it
#=======================================================================
generate_git_info() {
    repo_url=$(git config --get remote.origin.url)
    # Extract the repo name (everything after the last slash) and remove any trailing .git
    key=${repo_url##*/}
    key=${key%.git}
    GIT_INFO_PATH=$SCRIPT_DIR/${key}_git_info.json

    echo "Generating ${GIT_INFO_PATH}..."
    jq -n \
        --arg commit_hash "$(git rev-parse HEAD)" \
        --arg branch "$(git rev-parse --abbrev-ref HEAD)" \
        --arg tags "$(git tag --points-at HEAD | tr '\n' ' ')" \
        --arg author "$(git log -1 --pretty=format:'%an')" \
        --arg commit_date "$(date -u -d @"$(git log -1 --pretty=format:'%ct')" +'%Y-%m-%d %H:%M:%S UTC')" \
        --arg message "$(git log -1 --pretty=format:'%s' | tr '\n' ';')" \
        --arg build_date "$(date -u +'%Y-%m-%d %H:%M:%S UTC')" \
        "{\"${key}\": {commit_hash: \$commit_hash, branch: \$branch, tags: \$tags, author: \$author, commit_date: \$commit_date, message: \$message, build_date: \$build_date}}" \
        > "$GIT_INFO_PATH"
    echo "Generated $GIT_INFO_PATH"
}

#=======================================================================
# Fingerprint logic for init_gages inputs
#   - Computes a stable SHA256 for init_gages.py + files in gage_data/
#   - Stores/compares to decide whether to re-run init_gages
#=======================================================================
CERF_GAGES_FPRINT="${RUN_CERF_FLAG_DIRECTORY}/.gages_fingerprint"
echo "Gages fingerprint $CERF_GAGES_FPRINT"
[ -e "$CERF_GAGES_FPRINT" ] && ls -al "$CERF_GAGES_FPRINT"


# Compute a stable combined SHA256 of init_gages.py + all files in gage_data
compute_gages_fingerprint() {
    set -o pipefail
    local base="$SCRIPT_DIR/calibration/management/commands"
    local files=()

    # Must have init_gages.py
    if [ ! -f "$base/init_gages.py" ]; then
        echo "compute_gages_fingerprint: missing $base/init_gages.py" >&2
        return 1
    fi
    files+=("$base/init_gages.py")

    # Hash everything inside gage_data/ if present (excluding pyc/__pycache__)
    if [ -d "$base/gage_data" ]; then
        while IFS= read -r -d '' f; do
            files+=("$f")
        done < <(find "$base/gage_data" -type f ! -name '*.pyc' ! -path '*/__pycache__/*' -print0 | sort -z)
    fi

    # Hash each file then hash the list into a single digest
    sha256sum "${files[@]}" | sha256sum | awk '{print $1}'
}

store_gages_fingerprint() {
    local fp="$1"
    if [ -z "$fp" ]; then
        echo "store_gages_fingerprint: empty fingerprint" >&2
        return 1
    fi
    echo "$fp" > "$CERF_GAGES_FPRINT"
    echo "Saved gage fingerprint: ${fp:0:12}… -> $CERF_GAGES_FPRINT"
}

#=======================================================================
# Helper: run init_gages and store a provided fingerprint (or recompute if empty)
#=======================================================================
run_init_gages_and_store() {
    local fp="$1"

    echo "Running init_gages..."
    run_manage_command init_gages
    local status=$?

    if [ $status -ne 0 ]; then
        echo "init_gages failed with exit code $status"
        return $status   # propagate the exact failure code upward
    fi

    # If init_gages succeeded, store or recompute fingerprint
    if [ -n "$fp" ]; then
        store_gages_fingerprint "$fp"
    else
        if FP_NOW="$(compute_gages_fingerprint)"; then
            store_gages_fingerprint "$FP_NOW"
        else
            echo "Warning: could not compute fingerprint after init_gages"
        fi
    fi

    return 0
}

#=======================================================================
# Superuser helpers (email is the USERNAME_FIELD)
#=======================================================================
# Function: superuser_exists
#   - Checks if a superuser with the given email already exists.
#   - We have to shell out to Python/Django here because the user model
#     lives in Django (with a custom AUTH_USER_MODEL using email).
#
#   Implementation details:
#     * We run "manage.py shell -c" with a short Python snippet:
#         from django.contrib.auth import get_user_model
#         User = get_user_model()
#         print(User.objects.filter(is_superuser=True, email=...).exists())
#       This prints exactly "True" or "False".
#
#     * Unfortunately, "manage.py shell" also triggers app startup, which
#       prints a bunch of INFO log lines (database info, settings, etc.).
#       Those would otherwise get captured into our Bash variable.
#
#     * To make this robust:
#         --verbosity 0   => suppresses most management command chatter
#         2>/dev/null     => discards stderr noise
#         tail -n 1       => keeps only the *last* line of stdout (our True/False)
#         tr -d "\r"      => strip any stray carriage returns
#
#   Return value:
#     * Echoes a log line with the raw "True"/"False".
#     * Returns 0 (success) if "True", else 1.
#=======================================================================
superuser_exists() {
    local email="$1"
    echo "Checking for superuser [$email]..."
    local out
    out=$(
        DJANGO_EMAIL="$email" \
        python "$SCRIPT_DIR/manage.py" shell -c '
from django.contrib.auth import get_user_model
import os
User = get_user_model()
print(User.objects.filter(is_superuser=True, email=os.environ["DJANGO_EMAIL"]).exists())
' --verbosity 0 2>/dev/null | tail -n 1 | tr -d "\r"
    )
    echo "superuser_exists result: $out"
    [[ "$out" == "True" ]]
}

#=======================================================================
# Function: ensure_superuser
#   - If DJANGO_SUPERUSER_EMAIL/PASSWORD are set and the account is missing, create it.
#   - If vars are missing, log a WARNING and skip creation.
#=======================================================================
ensure_superuser() {
    # Pull from env; set these in cerfserver.env
    local email="${DJANGO_SUPERUSER_EMAIL}"
    local password="${DJANGO_SUPERUSER_PASSWORD}"

    if [ -z "$email" ] || [ -z "$password" ]; then
        echo "WARNING: ensure_superuser: DJANGO_SUPERUSER_EMAIL and/or DJANGO_SUPERUSER_PASSWORD not set."
        echo "WARNING: No superuser will be created automatically. Set these env vars to bootstrap an admin account."
        return 0
    fi

    if superuser_exists "$email"; then
        echo "Superuser [$email] already exists; skipping creation."
        return 0
    fi

    echo "Creating superuser [$email]..."
    DJANGO_SUPERUSER_EMAIL="$email" \
    DJANGO_SUPERUSER_PASSWORD="$password" \
    python "$SCRIPT_DIR/manage.py" createsuperuser --noinput
    local status=$?
    if [ $status -ne 0 ]; then
        echo "createsuperuser failed with exit code $status"
        return $status
    fi
}

#=======================================================================
# Function: run_migrate_with_showmigrations
#   - Always runs 'showmigrations' immediately after 'migrate'.
#   - If 'migrate' fails, still shows migrations, then exits with failure.
#   - If 'migrate' succeeds, execution continues normally.
#=======================================================================
run_migrate_with_showmigrations() {
    echo
    echo --------------------------------------------------------
    run_manage_command migrate
    local status=$?

    echo
    echo --------------------------------------------------------
    run_manage_command showmigrations calibration || true

    if [ $status -ne 0 ]; then
        echo "migrate failed"
        exit $status
    fi
}

#=======================================================================
# Non-Docker environment setup (packages, deps, git info)
#=======================================================================
if [ "$IN_DOCKER" = false ]; then
    # Docker takes care of installing dependencies in the Dockerfile
    if [ -n "${CERF_VENV}" ]; then
        ensure_virtualenv  # Activates and creates virtualenv if needed

        # Install all requirements
        echo "Upgrading pip"
        pip install --upgrade pip
        pip --version
        echo "Installing requirements.txt"
        pip install -r "$SCRIPT_DIR/requirements.txt"

        # Doing a pip install with requirements.txt does not reliably pick up changes to the other repos, so we have to force a re-install every time
        #MSWM_BRANCH='jwade_NGWPC-7589_add_aet_rootzone'
        MSWM_BRANCH='development'
        DATA_ASSIMILATION_BRANCH='development'
        NGEN_FORCING_TAG='development'

        echo
        echo "Installing mswm"
        if pip show "mswm" > /dev/null 2>&1; then
            # Package is installed, reinstall without dependencies
            pip install --force-reinstall --no-deps --no-cache-dir "git+https://github.com/NGWPC/nwm-msw-mgr.git@${MSWM_BRANCH}"
        else
            # Package is not installed, install with dependencies
            pip install "git+https://github.com/NGWPC/nwm-msw-mgr.git@${MSWM_BRANCH}"
        fi

        echo
        echo "Installing data_assimilation"
        if pip show "data_assimilation" > /dev/null 2>&1; then
            # Package is installed, reinstall without dependencies
            pip install --force-reinstall --no-deps --no-cache-dir "git+https://github.com/NGWPC/data-assimilation-engine.git@${DATA_ASSIMILATION_BRANCH}"
        else
            # Package is not installed, install with dependencies
            pip install "git+https://github.com/NGWPC/data-assimilation-engine.git@${DATA_ASSIMILATION_BRANCH}"

        fi

        generate_git_info

        echo
        echo --------------------------------------------------------

        #=======================================================================
        # Generate dev logrotate config from template using the user's path to the log directory
        #=======================================================================
        DEV_LOGROTATE_CONF="$SCRIPT_DIR/logrotate-ngencerf.dev.conf"
        if [ -f "$SCRIPT_DIR/logrotate-ngencerf.dev.template" ]; then
            sed "s#__LOG_DIR__#${SCRIPT_DIR}/logs#g" \
                "$SCRIPT_DIR/logrotate-ngencerf.dev.template" > "$DEV_LOGROTATE_CONF"
            echo "Generated dev logrotate config at $DEV_LOGROTATE_CONF"
        else
            echo "WARNING: dev logrotate template not found at $SCRIPT_DIR/logrotate-ngencerf.dev.template"
        fi

        #=======================================================================
        # Install dev logrotate cron job
        #=======================================================================
        echo "Setting up dev logrotate cron job..."
        # Remove any existing ngencerf dev cron jobs
        crontab -l 2>/dev/null | grep -v 'logrotate-ngencerf.dev.conf' > /tmp/mycron

        # Add the new cron job (points at the generated dev config)
        echo "0 10,22 * * * /usr/sbin/logrotate -s ~/.logrotate-status -f $DEV_LOGROTATE_CONF" >> /tmp/mycron

        crontab /tmp/mycron
        status=$?
        rm /tmp/mycron

        if [ $status -ne 0 ]; then
            echo "Warning: could not install cron job (exit $status)"
        fi

        echo "Current crontab:"
        crontab -l
    else
        echo "CERF_VENV is not set. Please set the virtual environment variable."
        exit 1
    fi
fi

#=======================================================================
# Run migrations, always ensure superuser, then run init_sql once
#=======================================================================
run_migrate_with_showmigrations

echo
echo --------------------------------------------------------
ensure_superuser
echo

echo
echo --------------------------------------------------------
run_manage_command init_sql
status=$?

if [ $status -ne 0 ]; then
    echo "init_sql failed with exit code $status"
    exit $status
fi

#=======================================================================
# Init data handling
#   - '--load-gages' or missing marker => unconditional init_gages
#   - Else compare fingerprint and conditionally run init_gages
#=======================================================================
GAGE_DATA_FLAG_FILE="${RUN_CERF_FLAG_DIRECTORY}/.load_gages"

# Only load gage data if the flag is provided or the flag file doesn't exist
# But we will also load gage data if the hash code detects that it has changed
if [ "$LOAD_GAGE_DATA" = true ] || [ ! -f "$GAGE_DATA_FLAG_FILE" ]; then
    echo
    echo "Loading ngenCERF gage data"

    echo
    echo --------------------------------------------------------
    # Unconditional run in this branch
    run_init_gages_and_store ""
    status=$?

    if [ $status -ne 0 ]; then
        echo "init_gages failed with exit code $status"
        exit $status
    fi

    touch "$GAGE_DATA_FLAG_FILE"
else
    echo
    echo --------------------------------------------------------
    # Auto-run init_gages if inputs changed; if hashing fails, run to be safe.
    if FP_NOW="$(compute_gages_fingerprint)"; then
        if [ ! -f "$CERF_GAGES_FPRINT" ]; then
            echo "No prior gage fingerprint found; running init_gages..."
            run_init_gages_and_store "$FP_NOW"
            status=$?
            if [ $status -ne 0 ]; then
                echo "init_gages failed with exit code $status"
                exit $status
            fi

        else
            read -r FP_OLD < "$CERF_GAGES_FPRINT" || FP_OLD=""
            if [ "$FP_NOW" != "$FP_OLD" ]; then
                echo "Gage inputs changed; running init_gages..."
                run_init_gages_and_store "$FP_NOW"
                status=$?
                if [ $status -ne 0 ]; then
                    echo "init_gages failed with exit code $status"
                    exit $status
                fi

            else
                echo "Gage inputs unchanged; skipping init_gages."
            fi
        fi
    else
        echo "Fingerprinting failed. Running init_gages to be safe…"
        run_init_gages_and_store ""
        status=$?
        if [ $status -ne 0 ]; then
            echo "init_gages failed with exit code $status"
            exit $status
        fi

    fi
fi

echo
echo --------------------------------------------------------
#=======================================================================
# Ensure bmi_forcing_templates in ngen-static-files
#   - Docker: copy from image-staged /ngencerf/prebuilt into bind-mounted dir
#   - Non-Docker: clone from Git into /ngencerf/data/ngen-static-files
#=======================================================================
STATIC_DIR="/ngencerf/data/ngen-static-files"
TARGET_DIR="${STATIC_DIR}/bmi_forcing_templates"

# Create static base and ensure a clean target location (shared logic)
mkdir -p "$STATIC_DIR"
rm -rf "$TARGET_DIR"
mkdir -p "$TARGET_DIR"

if [ "$IN_DOCKER" = true ]; then
    echo "Running in Docker: replacing bmi_forcing_templates from prebuilt data"

    PREBUILT_DIR="/ngencerf/prebuilt/bmi_forcing_templates"

    # Verify Dockerfile populated this directory
    if [ ! -d "$PREBUILT_DIR" ]; then
        echo "ERROR: Prebuilt bmi_forcing_templates not found at $PREBUILT_DIR"
        echo "Dockerfile must populate this directory during build."
        exit 1
    fi

    echo "Copying from $PREBUILT_DIR -> $TARGET_DIR"
    # Copy contents only
    cp -a "$PREBUILT_DIR"/. "$TARGET_DIR"/

else
    NGEN_FORCING_URL="https://github.com/NGWPC/ngen-forcing.git"

    echo "Not running in Docker: cloning bmi_forcing_templates from ${NGEN_FORCING_URL}, branch: ${NGEN_FORCING_TAG}"

    cd "$STATIC_DIR"

    git clone --depth 1 --filter=blob:none --sparse \
        -b "${NGEN_FORCING_TAG}" \
        "$NGEN_FORCING_URL" tmp-ngen-forcing

    cd tmp-ngen-forcing
    git sparse-checkout set NextGen_Forcings_Engine_BMI/BMI_NextGen_Configs/config_templates

    # Move *contents* of config_templates into TARGET_DIR
    cp -a NextGen_Forcings_Engine_BMI/BMI_NextGen_Configs/config_templates/. \
        "$TARGET_DIR"/

    cd "$STATIC_DIR"
    rm -rf tmp-ngen-forcing

    echo "bmi_forcing_templates updated successfully in $TARGET_DIR (non-Docker)."
    echo
fi

#=======================================================================
# Flush Redis cache at startup (all environments)
#   - Redis is cache-only; safe to clear on every server start
#   - In Docker, Redis is reached via the service name "redis"
#=======================================================================
echo "Flushing Redis cache..."
if command -v redis-cli >/dev/null 2>&1; then
    if [ "$IN_DOCKER" = true ]; then
        redis-cli -h redis -p 6379 FLUSHALL || echo "WARNING: Redis FLUSHALL failed"
    else
        redis-cli FLUSHALL || echo "WARNING: Redis FLUSHALL failed"
    fi
else
    echo "WARNING: redis-cli not found; skipping Redis flush"
fi



#=======================================================================
# Pre-start hook and start server
#=======================================================================
echo
run_manage_command pre_start
status=$?

if [ $status -ne 0 ]; then
    echo "pre_start failed with exit code $status"
    exit $status
fi

echo
echo "Starting server"

ASGI_FLAG="${CERF_ASGI:-}" # explicit override
PROD_FLAG="${CERF_PRODUCTION:-}" # general prod indicator

# Restore original stdout/stderr before starting the server (no /dev/tty dependency)
exec 1>&3 2>&4

# use ASGI server if ASGI_FLAG or PROD_FLAG are set
if [ "$ASGI_FLAG" = "1" ] || [ "$PROD_FLAG" = "1" ]; then
    echo "Launching Gunicorn (Uvicorn workers) ASGI server"
    # if GUNICORN_WORKERS is not set, calculate default value for WORKERS
    # Gunicorn recommends (2 x num of cores) + 1 as a reasonable default
    # but we cap it at 8 workers to avoid excessive memory use on small servers
    # and set a minimum of 2 workers to handle multiple requests
    WORKERS=${GUNICORN_WORKERS:-$(
        cpu=$(nproc)
        workers=$((cpu * 2 + 1))
        if [ "$workers" -lt 2 ]; then
            workers=2
        elif [ "$workers" -gt 8 ]; then
            workers=8
        fi
        echo "$workers"
    )}

    TIMEOUT=${GUNICORN_TIMEOUT:-120}
    BIND_ADDR=${GUNICORN_BIND:-0.0.0.0:8000}
    # --graceful-timeout extra time to finish in-flight requests on restart
    exec gunicorn cerfServer.asgi:application \
            --name ngencerf \
            --workers ${WORKERS} \
            --worker-class uvicorn.workers.UvicornWorker \
            --max-requests ${GUNICORN_MAX_REQUESTS:-300} \
            --max-requests-jitter ${GUNICORN_MAX_REQUESTS_JITTER:-100} \
            --preload \
            --bind ${BIND_ADDR} \
            --timeout ${TIMEOUT} \
            --graceful-timeout ${GUNICORN_GRACEFUL_TIMEOUT:-30} \
            --config "$(dirname "$0")/gunicorn_conf.py"
else
    echo "Launching Django development server (runserver)"

    if [ "$AUTO_RELOAD" = true ]; then
        echo "Auto-reload ENABLED"
        python "$cerfServer"/manage.py runserver 0.0.0.0:8000
    else
        echo "Auto-reload DISABLED (--noreload)"
        python "$cerfServer"/manage.py runserver 0.0.0.0:8000 --noreload
    fi
fi

if [ -n "${CERF_VENV}" ]; then
    deactivate
fi
