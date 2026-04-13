import json
import logging
import os
import threading
import time
import zipfile
from datetime import datetime, timezone

from django.conf import settings
from django.core.cache import cache
from django.http import FileResponse
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import StatusEnum
from calibration.util import cloud_util
from calibration.util.calibration_validators import CalibrationRunIdSerializer, GenericMessageWithIdResponseSerializer, ErrorResponseSerializer, \
    GetZipStatusSerializer, GetZipDownloadUrlResponseSerializer
from calibration.util.cloud_util import delete_expired_s3_objects_under_prefix, S3ProfileError, S3CredentialsExpired, \
    normalize_s3_prefix, s3_prefix_exists, join_url
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, get_user_email, validate_request, get_calibration_run, validate_response, get_elapsed_str, \
    ResponseError

logger = logging.getLogger(__name__)

_CLEANUP_LAST_RUN_KEY = "zip_cleanup_last_run"
_CLEANUP_LOCK_KEY = "zip_cleanup_lock"

downloadable_statuses = [s for s in StatusEnum if s not in {StatusEnum.READY, StatusEnum.SAVED, StatusEnum.SUBMITTED, StatusEnum.RUNNING}]


def get_zip_cache_key(calibration_run_id: int) -> str:
    """
    Returns the standardized cache key used to track zip job status for a given calibration run.

    - All zip-related endpoints use this key to read/write shared status in the Django cache.
    - The cache value is a dict with fields such as: status, s3_object (s3://bucket/key), started_at, download_name.

    :param calibration_run_id: CalibrationRun ID.
    :return: Cache key string used for this run's zip status.
    """
    return f'zip_status_{calibration_run_id}'


@extend_schema(
    request=CalibrationRunIdSerializer,
    responses={
        200: GenericMessageWithIdResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Starts a background process to zip calibration job files. Use `get_zip_status` to track progress."
)
@api_view(['GET', 'POST'])
@handle_exceptions
def start_zip_for_calibration_job(request: Request) -> Response:
    """
    Starts a background job to create a ZIP for the calibration run's job_data_dir.

    - Sets a shared cache entry (status=pending) keyed by get_zip_cache_key(calibration_run_id).
    - Runs the zip build in a daemon thread and updates cache to status=done (or status=error).
    - The produced ZIP file is written to settings.ZIP_TEMP_DIR, uploaded to S3, and then deleted locally.
      The S3 object is later removed by cleanup_expired_zips() based on settings.ZIP_RETENTION_SECONDS.

    Cache fields written:
    - status: "pending" | "done" | "error"
    - s3_object: S3 object URI (s3://bucket/key) for the ZIP when done (done only)
    - started_at: ISO timestamp when the job began
    - download_name: canonical filename presented to the client (done only)

    :param request: HTTP request containing calibration_run_id (POST body or query params).
    :return: JSON message with calibration_run_id, or a formatted error Response.
    """
    data = request.data if request.method == "POST" else request.query_params.dict()
    logger.debug(f"{get_caller_name()}() request from {get_user_email(request)} - {data}")

    validator, error_return = validate_request(CalibrationRunIdSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get("calibration_run_id")
    cache_key = get_zip_cache_key(calibration_run_id)

    run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=downloadable_statuses)
    if error_return:
        return error_return
    assert run is not None

    calibration_run = run

    if not settings.NGENCERF_ZIPS_S3_PATH:
        return ResponseError("NGENCERF_ZIPS_S3_PATH is undefined")

    # Make sure it's S3 and normalize to a slash-terminated prefix
    try:
        s3_prefix = normalize_s3_prefix(settings.NGENCERF_ZIPS_S3_PATH)
    except ValueError as e:
        return ResponseError(f"NGENCERF_ZIPS_S3_PATH is invalid: {e}")

    try:
        exists = s3_prefix_exists(
            s3_prefix,
            profile_name=settings.NGENCERF_RW_PROFILE,
        )
    except S3CredentialsExpired as e:
        return ResponseError(str(e))
    except S3ProfileError as e:
        return ResponseError(str(e))
    except PermissionError as e:
        return ResponseError(str(e))

    if not exists:
        return ResponseError(
            f"NGENCERF_ZIPS_S3_PATH does not exist on S3: {s3_prefix}"
        )

    cleanup_expired_zips()  # opportunistically delete old ZIPs (lazy TTL cleanup)

    zip_status = cache.get(cache_key)
    if zip_status and zip_status.get("status") == "pending":
        logger.info(f"Zip job already in progress for Calibration Job {calibration_run_id}")
        return Response(
            {
                "message": "Zip job already in progress",
                "status": zip_status["status"],
                "calibration_run_id": calibration_run_id
            }
        )

    # Mark status as pending (shared across workers)
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    cache.set(
        cache_key,
        {
            "status": "pending",
            "s3_object": None,
            "started_at": started_at,
            "download_name": None,  # canonical download name (filled in when done)
        },
        timeout=None,  # no timeout while building; "done"/"error" status gets a TTL
    )

    # Launch zip process in background
    def zip_job():
        start_time = datetime.now(timezone.utc)
        tmp_path = None
        zip_path = None
        zip_size = None

        try:
            job_data_dir = calibration_run.job_data_dir

            # Canonical download name (NO timestamp)
            zip_base_name = f"{os.path.basename(job_data_dir)}_{calibration_run.job_name}"
            download_name = f"{zip_base_name}.zip"

            # Unique on-disk filename includes timestamp to avoid collisions
            zip_filename = f"{zip_base_name}_{int(time.time())}.zip"
            zip_path = os.path.join(settings.ZIP_TEMP_DIR, zip_filename)

            # Write to a temp file first, then atomically rename into place.
            tmp_path = f"{zip_path}.tmp"

            # -------------------------
            # BUILD ZIP LOCALLY
            # -------------------------
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
                for root, _, files in os.walk(job_data_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arc_name = os.path.relpath(file_path, job_data_dir)
                        try:
                            zip_file.write(file_path, arc_name)
                        except FileNotFoundError:
                            logger.warning(f"File not found during zipping: {arc_name}")

            # Atomic replace: downloader will never see a partially-written zip.
            os.replace(tmp_path, zip_path)
            tmp_path = None  # prevent cleanup from deleting the final zip if names ever change

            # Now the file exists; size can be measured.
            zip_size = os.path.getsize(zip_path)

            logger.info(
                f"ZIP build complete (local): {zip_path} "
                f"({zip_size / 1024 / 1024:.2f} MB)"
            )

            # -------------------------
            # UPLOAD TO S3
            # -------------------------
            s3_object = join_url(s3_prefix, zip_filename)

            cloud_util.upload_file_to_s3(
                local_path=zip_path,
                s3_uri=s3_object,
                profile_name=settings.NGENCERF_RW_PROFILE
            )

            # Remove local copy immediately
            try:
                os.remove(zip_path)
                zip_path = None
            except Exception:
                logger.warning("Failed to delete local zip after upload", exc_info=True)

            # -------------------------
            # MARK COMPLETE
            # -------------------------
            cache.set(
                cache_key,
                {
                    "status": "done",
                    "s3_object": s3_object,
                    "started_at": started_at,
                    "download_name": download_name,
                },
                # Cache should live as long as the object can exist.
                timeout=settings.ZIP_RETENTION_SECONDS,
            )

            duration = datetime.now(timezone.utc) - start_time
            logger.info(
                f"Zip job completed on S3 for Calibration Job {calibration_run.id} "
                f"in {duration.total_seconds():.2f} seconds — "
                f"size: {zip_size / 1024 / 1024:.2f} MB"
            )

        except Exception:
            duration = datetime.now(timezone.utc) - start_time
            size_part = (
                f", size: {zip_size / 1024 / 1024:.2f} MB"
                if zip_size is not None
                else ""
            )

            cache.set(
                cache_key,
                {
                    "status": "error",
                    "s3_object": None,
                    "started_at": started_at,
                    "download_name": None,
                },
                # Cache should live as long as the object can exist.
                timeout=settings.ZIP_RETENTION_SECONDS,
            )

            logger.exception(
                f"Failed to zip/upload Calibration Job {calibration_run.id} "
                f"after {duration.total_seconds():.2f} seconds{size_part}: {e}"
            )

        finally:
            # Best-effort cleanup of any temp file left behind
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except FileNotFoundError:
                    pass
                except Exception:
                    logger.exception(f"Failed deleting temp zip: {tmp_path}")

            if zip_path:
                # Best-effort cleanup if local zip remains for any reason
                try:
                    os.remove(zip_path)
                except FileNotFoundError:
                    pass
                except Exception:
                    logger.exception(f"Failed deleting local zip: {zip_path}")

    threading.Thread(target=zip_job, daemon=True).start()

    response = {"message": "Zip job started", "calibration_run_id": calibration_run_id}

    response_validator, error_response = validate_response(GenericMessageWithIdResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f"Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - "
        f"{json.dumps(response_validator.data)}"
    )
    return Response(response_validator.data)


@extend_schema(
    request=CalibrationRunIdSerializer,
    responses={
        200: GetZipStatusSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Polling endpoint that returns the current status of a calibration zip job"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_zip_status(request: Request) -> Response:
    """
    Returns the current status of a background zip job started by start_zip_for_calibration_job().

    - Reads the shared cache entry keyed by get_zip_cache_key(calibration_run_id).
    - Does not start work; it only reports what is currently in cache.
    - Intended for polling until zip_status becomes "done" (or "error").

    s3_object is an internal S3 object identifier and is not directly downloadable.
    Use get_calibration_zip_download_url to obtain a presigned HTTP URL.

    Response fields:
    - calibration_run_id
    - zip_status: "pending" | "done" | "error"
    - s3_object: S3 object URI (s3://bucket/key) for the ZIP when done
    - started_at: ISO timestamp when the job began

    :param request: HTTP request containing calibration_run_id (POST body or query params).
    :return: JSON response with zip status information, or a formatted error Response.
    """
    data = request.data if request.method == "POST" else request.query_params.dict()
    logger.debug(f"{get_caller_name()}() request from {get_user_email(request)} - {data}")

    validator, error_response = validate_request(CalibrationRunIdSerializer, data)
    if error_response:
        return error_response

    calibration_run_id = validator.get("calibration_run_id")
    cache_key = get_zip_cache_key(calibration_run_id)

    cleanup_expired_zips()  # opportunistically delete old ZIPs (lazy TTL cleanup)

    zip_status = cache.get(cache_key)
    if not zip_status:
        return ResponseError(f"No zip job found for Calibration Job {calibration_run_id}")

    response = {
        "calibration_run_id": calibration_run_id,
        "zip_status": zip_status.get("status"),
        "s3_object": zip_status.get("s3_object"),
        "started_at": zip_status.get("started_at"),
    }

    response_validator, error_response = validate_response(GetZipStatusSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f"Returning to {get_user_email(request)} from {get_caller_name()}"
        f"{get_elapsed_str(request)} - {json.dumps(response_validator.data)}"
    )

    return Response(response_validator.data)


def cleanup_expired_zips() -> None:
    """
    Opportunistically deletes expired ZIP artifacts created by zip endpoints.

    Why this exists:
    - Background zips are uploaded to S3 and then deleted locally; the S3 objects must be cleaned up later.
    - For the synchronous CLI endpoint (get_calibration_job_zip), ZIPs may be left on disk temporarily and
      are deleted lazily to avoid breaking large downloads.

    Behavior:
    - Deletes S3 ZIP objects under settings.NGENCERF_ZIPS_S3_PATH that are older than
      (now - settings.ZIP_RETENTION_SECONDS).
    - Also scans settings.ZIP_TEMP_DIR for:
      - "*.zip" files older than (now - settings.ZIP_RETENTION_SECONDS) created by the synchronous CLI endpoint
      - "*.tmp" files older than (now - settings.ZIP_RETENTION_SECONDS) from interrupted builds
    - Throttled to run at most once every 5 minutes across all workers (shared cache timestamp).
    - Uses a shared cache lock so only one worker performs deletions at a time.

    :return: None
    """
    raw_s3_dir = getattr(settings, "NGENCERF_ZIPS_S3_PATH", None)
    normalized_s3_dir = None

    if isinstance(raw_s3_dir, str):
        try:
            normalized_s3_dir = normalize_s3_prefix(raw_s3_dir)
        except ValueError:
            normalized_s3_dir = None

    logger.debug(
        f"ZIP cleanup sweep: s3_dir={normalized_s3_dir or raw_s3_dir}, dir={settings.ZIP_TEMP_DIR}, "
        f"url_ttl={settings.ZIP_DOWNLOAD_URL_TTL_SECONDS}s, retention={settings.ZIP_RETENTION_SECONDS}s"
    )

    now = time.time()

    # Run at most every 5 minutes across all workers.
    last = cache.get(_CLEANUP_LAST_RUN_KEY)
    if last and (now - float(last)) < 300:
        return

    # Acquire a short-lived lock across workers to avoid multiple processes deleting simultaneously.
    if not cache.add(_CLEANUP_LOCK_KEY, "1", timeout=60):
        return

    try:
        # Record that cleanup ran (even if nothing is deleted) to prevent repeated scans.
        cache.set(_CLEANUP_LAST_RUN_KEY, now, timeout=24 * 3600)

        cutoff_unix = now - settings.ZIP_RETENTION_SECONDS
        cutoff_dt = datetime.fromtimestamp(cutoff_unix, timezone.utc).isoformat()

        logger.debug(
            "ZIP cleanup cutoff=%s (retention=%ss, now=%s)",
            cutoff_dt,
            settings.ZIP_RETENTION_SECONDS,
            datetime.fromtimestamp(now, timezone.utc).isoformat(),
        )

        # ------------------------------------------------------------
        # Delete expired ZIP objects from S3
        # ------------------------------------------------------------
        deleted_s3 = 0

        s3_dir = getattr(settings, "NGENCERF_ZIPS_S3_PATH", None)
        if isinstance(s3_dir, str):
            try:
                s3_dir = normalize_s3_prefix(s3_dir)
                deleted_s3 = delete_expired_s3_objects_under_prefix(
                    s3_dir_uri=s3_dir,
                    cutoff_unix_seconds=cutoff_unix,
                    profile_name=settings.NGENCERF_RW_PROFILE
                )
            except ValueError as e:
                logger.error(f"Invalid S3 ZIP prefix configured: {s3_dir}: {e}")
                deleted_s3 = 0
            except Exception:
                # Keep behavior minimal: log and skip S3 cleanup rather than failing endpoints.
                logger.exception(f"Failed S3 ZIP cleanup under: {s3_dir}")
                deleted_s3 = 0

        # ------------------------------------------------------------
        # Local cleanup (CLI zips and interrupted temp files)
        # ------------------------------------------------------------
        # If ZIP_TEMP_DIR doesn't exist (misconfig or first-run), just no-op.
        deleted_zip = 0
        deleted_tmp = 0

        if os.path.isdir(settings.ZIP_TEMP_DIR):
            for name in os.listdir(settings.ZIP_TEMP_DIR):
                # Only manage artifacts created by this feature.
                is_zip = name.endswith(".zip")
                is_tmp = name.endswith(".tmp")
                if not (is_zip or is_tmp):
                    continue

                path = os.path.join(settings.ZIP_TEMP_DIR, name)

                # File might disappear between listdir() and stat() if another worker deletes it.
                try:
                    st = os.stat(path)
                except FileNotFoundError:
                    continue

                # Delete files older than the retention window.
                if st.st_mtime < cutoff_unix:
                    try:
                        os.remove(path)
                        logger.info(
                            "Lazy cleanup deleted local artifact: %s (mtime=%s, cutoff=%s)",
                            path,
                            datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                            cutoff_dt,
                        )
                        if is_zip:
                            deleted_zip += 1
                        else:
                            deleted_tmp += 1
                    except FileNotFoundError:
                        # Another worker/process deleted it after our stat().
                        pass
                    except Exception:
                        logger.exception(f"Failed deleting expired artifact: {path}")

        logger.info(
            f"Lazy cleanup deleted {deleted_s3} expired s3 zip(s), {deleted_zip} expired local zip(s), "
            f"and {deleted_tmp} expired tmp file(s)"
        )

    finally:
        # Always release the lock.
        cache.delete(_CLEANUP_LOCK_KEY)


@extend_schema(
    request=CalibrationRunIdSerializer,
    responses={
        200: GetZipDownloadUrlResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        ),
    },
    description="Return a short-lived download URL for a prepared ZIP (direct from S3)."
)
@api_view(["GET", "POST"])
@handle_exceptions
def get_calibration_zip_download_url(request: Request) -> Response:
    data = request.data if request.method == "POST" else request.query_params.dict()
    logger.debug(f"{get_caller_name()}() request from {get_user_email(request)} - {data}")

    validator, error_response = validate_request(CalibrationRunIdSerializer, data)
    if error_response:
        return error_response

    calibration_run_id = validator.get("calibration_run_id")
    zip_cache_key = get_zip_cache_key(calibration_run_id)

    cleanup_expired_zips()

    run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=downloadable_statuses)
    if error_return:
        return error_return
    assert run is not None

    zip_status = cache.get(zip_cache_key)
    if not zip_status:
        return ResponseError(f"Zip job not found for Calibration Job {calibration_run_id}", http_status=status.HTTP_404_NOT_FOUND)

    if zip_status.get("status") != "done":
        return ResponseError(f"Zip file for Calibration Job {calibration_run_id} is not ready yet")

    s3_object = zip_status.get("s3_object")
    if not s3_object:
        return ResponseError("Zip missing in S3")

    download_url = cloud_util.generate_presigned_download_url(
        s3_uri=s3_object,
        expires_seconds=settings.ZIP_DOWNLOAD_URL_TTL_SECONDS,
        profile_name=settings.NGENCERF_RW_PROFILE
    )

    response = {
        "calibration_run_id": calibration_run_id,
        "download_url": download_url,
        "expires_in_seconds": settings.ZIP_DOWNLOAD_URL_TTL_SECONDS,
    }

    response_validator, error_response = validate_response(GetZipDownloadUrlResponseSerializer, response)
    if error_response:
        return error_response

    return Response(response_validator.data)


@api_view(['GET', 'POST'])
@handle_exceptions
def get_calibration_job_zip(request: Request) -> FileResponse | Response:
    """
    Synchronous ZIP download endpoint (primarily for the CLI).

    - Builds a ZIP of the calibration run's job_data_dir on disk (not in memory).
    - Writes to a temporary file first and then atomically renames it into place.
    - Streams the completed ZIP back as a FileResponse attachment.

    Notes:
    - This endpoint does not use the background cache-based zip workflow.
    - The created ZIP artifact is left on disk and later removed by cleanup_expired_zips()
      based on settings.ZIP_RETENTION_SECONDS.

    :param request: HTTP request containing calibration_run_id (POST body or query params).
    :return: FileResponse streaming the ZIP, or a formatted error Response.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunIdSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    calibration_run, error_return = get_calibration_run(calibration_run_id, request.user, run_status=downloadable_statuses)
    if error_return:
        return error_return
    assert calibration_run is not None

    cleanup_expired_zips()  # opportunistically delete old ZIPs (lazy TTL cleanup)

    job_data_dir = calibration_run.job_data_dir

    # Canonical download name (no timestamp)
    zip_base_name = f"{os.path.basename(job_data_dir)}_{calibration_run.job_name}"
    download_name = f"{zip_base_name}.zip"

    # Unique on-disk name (avoid collisions)
    zip_filename = f"{zip_base_name}_{int(time.time())}.zip"
    zip_path = os.path.join(settings.ZIP_TEMP_DIR, zip_filename)

    # Write to a temp file first, then atomically rename into place.
    tmp_path = f"{zip_path}.tmp"

    # Build zip to temp path first, then atomically place the final file
    try:
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for root, _, files in os.walk(job_data_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arc_name = os.path.relpath(file_path, job_data_dir)
                    try:
                        zip_file.write(file_path, arc_name)
                    except FileNotFoundError:
                        logger.warning(f"File not found during zipping: {arc_name}")

        os.replace(tmp_path, zip_path)
        tmp_path = None

    finally:
        # Best-effort cleanup of temp zip if something failed mid-build
        if tmp_path:
            try:
                os.remove(tmp_path)
            except FileNotFoundError:
                pass
            except Exception:
                logger.exception(f"Failed deleting temp zip: {tmp_path}")

    # Stream the finished zip back
    zip_file = None
    try:
        zip_file = open(zip_path, "rb")
        response = FileResponse(zip_file, content_type="application/zip")

        zip_size = os.path.getsize(zip_path)
        response["Content-Length"] = str(zip_size)

        apply_zip_download_headers(response, download_name)

        return response

    except Exception as e:
        if zip_file is not None:
            try:
                zip_file.close()
            except Exception:
                logger.debug("Failed to close file handle", exc_info=True)
        logger.exception(f"Failed to serve zip file for Calibration Job {calibration_run_id}: {e}")
        return ResponseError(f"Failed to read zip file for Calibration Job {calibration_run_id}")


def apply_zip_download_headers(response: FileResponse, download_name: str, origin: str | None = None) -> FileResponse:
    """
    Apply consistent headers for streaming a ZIP download.

    - Forces an attachment download name via Content-Disposition.
    - Prevents caching by browsers and proxies.
    - Disables Nginx buffering to allow direct streaming of large files.
    - Prevents middleware/proxies from applying gzip or other content encodings.
    - Optionally sets Access-Control-Allow-Origin when an allowed Origin is provided.

    Notes:
    - This function does NOT fully implement CORS by itself. It only mirrors an allowed Origin.
      If you need browser JS to read Content-Disposition, you must also expose that header
      (e.g., via CORS_EXPOSE_HEADERS / Access-Control-Expose-Headers).

    :param response: FileResponse already initialized with the ZIP file handle.
    :param download_name: Filename presented to the client.
    :param origin: Request Origin header value (optional).
    :return: The same response object (mutated).
    """
    if origin and origin in settings.CORS_ALLOWED_ORIGINS:
        response["Access-Control-Allow-Origin"] = origin
        # Avoid caches mixing responses across origins.
        response["Vary"] = "Origin"

    # Force download with a stable filename.
    response["Content-Disposition"] = f'attachment; filename="{download_name}"'

    # Do not allow browsers or proxies to store this response.
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"  # for older proxies

    # Tell Nginx NOT to buffer the file before sending it downstream.
    # This avoids Nginx holding a 1.5 GB file in memory/disk buffers, which can trigger timeouts or stall the transfer.
    response["X-Accel-Buffering"] = "no"

    response["Content-Encoding"] = "identity"  # Prevent response from being gzipped (especially ZIP files) by middleware

    return response
