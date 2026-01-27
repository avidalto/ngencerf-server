import logging
import os
import platform
import signal
import threading
import time

"""
Gunicorn configuration for ngenCERF.

This config EXPECTS Gunicorn is started with --preload.

Why --preload matters:
- Without --preload:
  - Django is NOT loaded in the master process
  - Django settings/logging are unavailable in the master
  - when_ready() cannot safely use Django APIs or Django-installed logging handlers
- With --preload:
  - Django loads ONCE in the master process
  - Workers fork from an initialized Django state

runCerf.sh already uses --preload.
"""


# ---------------------------------------------------------------------
# Environment / flags
# ---------------------------------------------------------------------

def _env_bool(name: str, default: bool = False) -> bool:
    """
    Read a boolean value from the environment.

    Accepts: 1, true, yes, on (case-insensitive).
    """
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------
# Gunicorn native logging settings
# ---------------------------------------------------------------------

"""
Gunicorn's native logging controls.

Django's LOGGING is the single source of truth for formatting and handlers.
These settings minimize Gunicorn's own logging outputs, and _configure_logging()
attaches Django's handlers to gunicorn.* and uvicorn.* loggers instead.
"""

# Note: accesslog=None disables Gunicorn's access log output, so wiring handlers for
# gunicorn.access does not enable access logs by itself.
accesslog = None

# Do not write Gunicorn error logs to a separate destination/file.
# Gunicorn may still emit certain lifecycle lines to stderr/stdout.
errorlog = None

# Threshold for Gunicorn's own loggers and for gunicorn/uvicorn loggers
# when handlers are attached in _configure_logging()
loglevel = os.getenv("GUNICORN_LOGLEVEL", "info")

# Do not redirect worker stdout/stderr into Gunicorn's logging system
capture_output = False

# Do not globally disable loggers created during --preload.
# Django's LOGGING controls logger behavior and propagation.
disable_existing_loggers = False

# Enable extra diagnostics only when explicitly requested
_DIAG = _env_bool("GUNICORN_DIAGNOSTICS", False)
_DIAG_POLL_SECONDS = int(os.getenv("GUNICORN_DIAGNOSTICS_POLL_SECONDS", "10"))


# ---------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------

def _configure_logging(worker=None):
    """
    Route Gunicorn and Uvicorn logs through Django's logging system.

    Behavior:
    - Reads handlers from Django's root logger (already installed due to --preload)
    - Attaches those handlers to gunicorn.* and uvicorn.* loggers
    - Sets propagate=False to prevent duplicate handling
    - De-duplicates handlers on repeated calls by object identity


    This allows all Gunicorn/Uvicorn output to land in ngencerf.log using
    Django's formatting and file/console configuration.
    """
    try:
        # Django logging handlers should already be installed because --preload loads
        # the app in the master before when_ready(), and workers inherit that state at fork.
        django_root = logging.getLogger()
        django_handlers = django_root.handlers

        level_name = loglevel.upper()
        level = getattr(logging, level_name, logging.INFO)

        for name in (
                "gunicorn.error",
                "gunicorn.access",
                "uvicorn",
                "uvicorn.error",
                "uvicorn.access",
                "uvicorn.asgi",
        ):
            glog = logging.getLogger(name)
            glog.setLevel(level)
            glog.propagate = False

            existing = {id(h) for h in glog.handlers}
            for h in django_handlers:
                if id(h) not in existing:
                    glog.addHandler(h)

    except Exception as e:
        # Safe to ignore during very early startup before Django initializes
        print(f"[gunicorn_conf] Logging hook failed (safe to ignore early): {e}")


def _log_signal_handlers(logger: logging.Logger, where: str) -> None:
    """
    Log the current signal handlers in the master process.

    Used to confirm that Gunicorn's arbiter owns SIGCHLD, SIGTERM,
    and other lifecycle signals, and to detect unexpected overrides.
    """

    def _h(sig_name: str) -> str:
        sig = getattr(signal, sig_name, None)
        if sig is None:
            return "<missing>"
        try:
            return repr(signal.getsignal(sig))
        except Exception as e:
            return f"<error: {e}>"

    logger.info(
        "[gunicorn_conf] %s signal handlers: SIGCHLD=%s SIGTERM=%s SIGINT=%s SIGHUP=%s SIGQUIT=%s SIGUSR1=%s SIGUSR2=%s",
        where,
        _h("SIGCHLD"),
        _h("SIGTERM"),
        _h("SIGINT"),
        _h("SIGHUP"),
        _h("SIGQUIT"),
        _h("SIGUSR1"),
        _h("SIGUSR2"),
    )


def _start_sigchld_monitor(logger: logging.Logger) -> None:
    """
    Start a background thread in the master process that periodically checks
    whether the SIGCHLD handler has changed.

    This is a diagnostic tool to detect external code or libraries that override
    Gunicorn's child-process signal handling, which can prevent worker respawning.
    """
    try:
        last = signal.getsignal(signal.SIGCHLD)
    except Exception as e:
        logger.warning("[gunicorn_conf] SIGCHLD monitor could not read handler: %s", e)
        return

    def _run():
        nonlocal last
        while True:
            time.sleep(_DIAG_POLL_SECONDS)
            try:
                cur = signal.getsignal(signal.SIGCHLD)
            except Exception:
                continue
            if cur != last:
                logger.warning("[gunicorn_conf] SIGCHLD handler changed: %r -> %r", last, cur)
                last = cur

    t = threading.Thread(target=_run, name="sigchld-monitor", daemon=True)
    t.start()


# ---------------------------------------------------------------------
# Gunicorn lifecycle hooks
# ---------------------------------------------------------------------

def on_starting(_server):
    """
    Runs once in the master process when Gunicorn is starting.

    This executes before workers are forked. With --preload, Django will be
    loaded during Gunicorn startup before when_ready() is called.
    """
    logging.getLogger("gunicorn.error").info("[gunicorn_conf] Master starting up (PID=%s)", os.getpid())


def when_ready(_server):
    """
    Runs once in the master process after Gunicorn has finished --preload.

    At this point:
    - Django is loaded in the master process
    - Django logging handlers are installed and can be attached to gunicorn/uvicorn
    - Signal handlers reflect Gunicorn's arbiter state

    This hook:
    - Configures logging for gunicorn/uvicorn loggers
    - Emits version and platform diagnostics
    - Snapshots SIGCHLD and other signal handlers
    - Optionally starts a background monitor to detect SIGCHLD handler changes
    """
    _configure_logging()

    logger = logging.getLogger("gunicorn.error")
    logger.info("[gunicorn_conf] Master ready (PID=%s)", os.getpid())

    try:
        import gunicorn  # type: ignore
        logger.info("[gunicorn_conf] gunicorn=%s", getattr(gunicorn, "__version__", "<unknown>"))
    except Exception:
        pass

    try:
        import uvicorn  # type: ignore
        logger.info("[gunicorn_conf] uvicorn=%s", getattr(uvicorn, "__version__", "<unknown>"))
    except Exception:
        pass

    _log_signal_handlers(logger, "when_ready(master)")

    # Background diagnostic thread: periodically checks the master's SIGCHLD handler
    # and logs only if it changes, to detect external code or libraries interfering
    # with Gunicorn's child-process signal handling.
    if _DIAG:
        logger.info(
            "[gunicorn_conf] DIAGNOSTICS enabled. python=%s platform=%s",
            platform.python_version(),
            platform.platform(),
        )
        _start_sigchld_monitor(logger)


def pre_fork(_server, worker):
    """
    Runs in the master process immediately before forking a worker.

    Used for diagnostics to confirm the master is attempting to respawn
    workers when they exit or exceed max_requests.
    """
    if _DIAG:
        logging.getLogger("gunicorn.error").info(
            "[gunicorn_conf] pre_fork: about to fork worker (worker_tmp=%s)",
            getattr(worker, "tmp", None)
        )


def post_fork(_server, worker):
    """
    Runs once in each worker process after it has been forked.

    Responsibilities:
    - Close any database connections inherited from the master
    - Attach Django logging handlers to gunicorn/uvicorn loggers
    - Enforce per-worker umask
    """
    try:
        from django.db import connections
        connections.close_all()
    except Exception:
        # If Django isn't ready for any reason, don't break worker boot.
        pass

    _configure_logging(worker)

    # Enforce umask per-worker
    os.umask(0o022)

    worker.log.info("[gunicorn_conf] post_fork: Worker started PID=%s with umask=022", worker.pid)


def worker_exit(_server, worker):
    """
    Runs in the master when a worker process exits.

    Useful for correlating exits with SIGCHLD handling and respawn behavior.
    """
    logging.getLogger("gunicorn.error").warning("[gunicorn_conf] worker_exit: Worker PID=%s exiting", worker.pid)


def worker_int(worker):
    """
    Runs in the worker when it receives SIGINT.
    """
    worker.log.warning("[gunicorn_conf] worker_int: Worker %s got SIGINT", worker.pid)


def worker_abort(worker):
    """
    Runs in the worker when it is forcefully aborted by Gunicorn.
    """
    worker.log.error("[gunicorn_conf] worker_abort: Worker %s aborted", worker.pid)
