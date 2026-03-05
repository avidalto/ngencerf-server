"""
cloud_util.py

Filesystem abstraction utilities built on top of fsspec.

This module provides a unified interface for working with both local and cloud
storage (S3, GCS, Azure Blob/ADLS, etc.). It includes helpers for:

- Path normalization (`normalize_url`, `get_filesystem`):
  Ensures that bare paths are converted into proper URLs so that fsspec can
  operate consistently across providers.

- File operations (`open_file`, `path_exists`, `is_dir`, `list_files`):
  Cloud/local agnostic wrappers that mimic Python’s built-in file and os.path
  utilities but work transparently with remote storage.

- Bulk copy (`copy_tree`):
  Recursively copy entire directory trees. Uses provider-native server-side copy
  when possible (fast, no local I/O). Otherwise streams through this process
  with multi-threaded workers.

- Caching and localization (`localize_to_path`):
  Provides persistent or ephemeral caching for remote files. Remote objects can
  be downloaded once into /var/tmp/fsspec-cache and reused across multiple runs,
  avoiding redundant S3/GCS downloads. Cache entries are validated with provider
  metadata (etag/size/mtime). For one-shot ephemeral usage, files can be staged
  into a NamedTemporaryFile and removed after use.

⚠️ Cache persistence note:
  The cache under `/var/tmp/fsspec-cache` is never automatically cleaned up.
  It may grow indefinitely as new files are downloaded. However, the cache
  contents can be deleted at any time without harm; missing files will simply
  be re-fetched from the remote provider.

Typical usage:
  * Use `open_file` when you want to stream a file directly (no caching).
  * Use `localize_to_path` when the same remote file will be accessed multiple
    times within a workflow (e.g., geopackages or forcing CSVs).
  * Use `copy_tree` for bulk movement of files between providers or to local
    disk.

Environment:
  * Relies on fsspec’s standard authentication (AWS_*, GOOGLE_APPLICATION_CREDENTIALS,
    AZURE_*).
  * Cache is stored in /var/tmp by default, which typically survives reboots.
"""

import datetime
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Tuple
from urllib.parse import urlparse

import boto3
import botocore.exceptions
import fsspec

from calibration.views.called_from import called_from

logger = logging.getLogger(__name__)

# Persistent cache for localized cloud files.
# /var/tmp survives reboots; /tmp is usually wiped at boot.
CLOUD_CACHE_DIR = "/var/tmp/fsspec-cache"

# Regexes for detecting Windows local paths
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_UNC_RE = re.compile(r"^\\\\")  # UNC paths like \\server\share

_REMOTE_SCHEMES = {"s3", "gs", "gcs", "az", "abfs", "abfss"}


# You can pass auth via env (AWS_*, GOOGLE_APPLICATION_CREDENTIALS, AZURE_*),
# or via storage_options in get_filesystem(). Keep it simple here.

class CredentialsExpired(Exception):
    """Generic credential-expired error across all cloud providers."""
    pass


class S3CredentialsExpired(CredentialsExpired):
    """Raised when AWS S3 credentials are expired."""
    pass


# ----------------------------------------------------------------------
# Filesystem utilities
# ----------------------------------------------------------------------

def get_filesystem(url: str) -> tuple[fsspec.AbstractFileSystem, str]:
    """
    Return an fsspec filesystem for the given URL and the normalized URL.

    - If passed a bare local path, we normalize to file://... so fsspec is happy.
    - The returned fs is created from the URL's scheme (s3, file, gs, az, ...).

    :param url: Full URL string for a resource (cloud or local).
    :return: (filesystem, normalized_url) tuple
    """
    url = normalize_url(url)
    parsed = urlparse(url)
    scheme = parsed.scheme or "file"
    # fsspec auto-picks backend by scheme
    fs = fsspec.filesystem(scheme)
    return fs, url


def _norm_prefix(url: str) -> tuple[str, str]:
    """
    Split a URL into (base, path) without trailing slashes in base.

    Example:
        s3://my-bucket/path/to/stuff -> ("s3://my-bucket", "path/to/stuff")

    :param url: Full URL string.
    :return: (base, path) where base includes scheme+netloc, and path is the remainder.
    """
    url = normalize_url(url)
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}".rstrip("/")
    path = p.path.lstrip("/")
    return base, path


def _same_provider(fs_a: fsspec.AbstractFileSystem,
                   fs_b: fsspec.AbstractFileSystem) -> bool:
    """
    Return True if both filesystem objects are of the same backend type.
    This is important because server-side copy is only possible when
    source and destination are managed by the same provider.
    """
    return type(fs_a) is type(fs_b)


def _server_side_cp_supported(fs: fsspec.AbstractFileSystem) -> bool:
    """
    Check if a filesystem supports a provider-native server-side copy.
    For example: S3, GCS, Azure may expose a 'cp_file' or 'copy' method.
    """
    return hasattr(fs, "cp_file") or hasattr(fs, "copy") or hasattr(fs, "cp")


def _cp_file_server_side(fs: fsspec.AbstractFileSystem, src: str, dest: str) -> None:
    """
    Attempt to perform a server-side copy using whichever method the
    backend exposes. Raises NotImplementedError if not supported.

    :param fs: The fsspec filesystem object.
    :param src: Source file URL.
    :param dest: Destination file URL.
    """
    if hasattr(fs, "cp_file"):
        return fs.cp_file(src, dest)
    if hasattr(fs, "copy"):
        return fs.copy(src, dest)
    if hasattr(fs, "cp"):
        return fs.cp(src, dest)
    raise NotImplementedError("No server-side copy method available for this backend")


def is_probably_local_path(p: str) -> bool:
    """
    Heuristic to decide if a path is local rather than a URL.

    - If it has a URL scheme (e.g. s3://), return False.
    - Windows drive letters (C:/...) or UNC paths (\\\\server\\share) return True.
    - Any other relative or absolute POSIX path returns True.
    """
    parsed = urlparse(p)
    if parsed.scheme:  # already looks like a URL
        return False
    # Windows drive or UNC counts as local
    if _DRIVE_RE.match(p) or _UNC_RE.match(p):
        return True
    # Plain relative/absolute posix path -> local
    return True  # default: assume local


def normalize_url(p: str) -> str:
    """
    Ensure a path is expressed as a proper URL.

    - If it looks like a local path, expand and absolutize it,
      then prefix with file:// (POSIX) or file:///C:/... (Windows).
    - If it's already a URL (has a scheme), return unchanged.
    """
    if is_probably_local_path(p):
        # Expand ~ and make absolute; fsspec file:// likes absolute paths
        ap = os.path.abspath(os.path.expanduser(p))
        # On Windows, file URLs need forward slashes and an extra slash before drive
        if os.name == "nt":
            ap = ap.replace("\\", "/")
            return f"file:///{ap}"  # Windows: file:///C:/path
        return f"file://{ap if ap.startswith('/') else '/' + ap}"  # POSIX
    return p


def join_url(base: str, *parts: str) -> str:
    """
    Join a URL base and path parts with single slashes, preserving scheme form.
    - If base ends with '://', do not strip slashes (keeps 'file://' intact).
    - Ensures 'file://' is normalized to 'file:///' if needed.
    """
    if base.endswith("://"):
        b = base
    else:
        b = base.rstrip("/")
    segs = [p.strip("/") for p in parts if p]
    url = f"{b}/{'/'.join(segs)}" if segs else b
    if url.startswith("file://") and not url.startswith("file:///"):
        url = url.replace("file://", "file:///")
    return url


# ----------------------------------------------------------------------
# File operations
# ----------------------------------------------------------------------

def copy_tree(src_url: str,
              dst_url: str,
              workers: int = 16,
              buffer_size: int = 8 * 1024 * 1024,
              verify: bool = False) -> int:
    """
    Generic and reliable tree copy between:
        • EFS → S3/GCS/Azure
        • S3/GCS/Azure → EFS
        • Cloud → Cloud (server-side when supported)
        • Local → Local

    Preserves directory structure. Can optionally verify via SHA256.
    When copying S3→local with verify=True, uses the source manifest instead
    of re-hashing cloud objects.

    Manifest rules:
      • LOCAL → CLOUD with verify=True: manifest.json is CREATED on cloud.
      • CLOUD → LOCAL with verify=True: manifest.json is USED but NOT RESTORED.
      • Symlinks are preserved: stored as metadata in manifest and recreated on restore.


    ------------------------------------------------------------------
    URL HANDLING
    ------------------------------------------------------------------
    fsspec requires well-formed URLs. Local paths such as:
        /ngencerf/data/run/123
        ../../relative/path
        ~/stuff
    are *not* proper URLs. Depending on the backend, fsspec may:
        • reject them,
        • treat them as relative paths,
        • generate inconsistent behavior across providers.

    normalize_url():
        • expands ~
        • absolutizes the path
        • converts it into a proper file URL:
              /path/to/x  →  file:///path/to/x

    This guarantees:
        • fsspec sees a real URL (s3://, gs://, az://, file://)
        • local and cloud code paths behave consistently
        • path comparisons (prefix stripping, relpath, etc.) work predictably
        • no surprises with Windows-style paths

    When normalize_url() is needed:
        ✓ any time the caller provides a bare local filesystem path
        ✓ any time the caller provides a relative path
        ✓ any time fsspec must process the path through filesystem(s)

    When normalize_url() is NOT strictly required:
        • when the user already provides valid URLs:
              s3://bucket/key
              gs://bucket/key
              file:///abs/path

    BUT it’s still safe and recommended to run normalize_url() on everything,
    because it standardizes all inputs and prevents subtle bugs.

    ------------------------------------------------------------------
    Copy Strategy
    ------------------------------------------------------------------
      * Local source  → enumerated with os.walk()
      * Cloud source  → enumerated with fs.find()
      * Cloud→Cloud   → attempt provider server-side copy
      * Otherwise     → streamed copy via threads

    Note: copy_tree does NOT use the caching layer (localize_to_path).
    Use localize_to_path() yourself if you need persistent reuse of remote
    files (e.g., large GeoPackages reused across workflows).

    :param src_url: Source prefix. Accepts:
                        • A full cloud URL (s3://bucket/prefix, gs://…, az://…)
                        • A full local URL (file:///path/to/dir)
                        • A bare local path (/ngencerf/data/run/123 or relative paths)

                    Bare local paths are automatically normalized into fully-qualified
                    file:// URLs via normalize_url(). The caller does NOT need to
                    pre-normalize them.

    :param dst_url: Destination prefix. Same rules as src_url:
                        • Cloud URLs stay as-is
                        • file:/// URLs stay as-is
                        • Bare local paths are automatically converted to file:/// form

                    Normalization ensures fsspec always receives a valid URL and can
                    resolve the correct backend.

    :param workers: Number of parallel threads for streamed copies. Higher values
                    increase throughput when copying many small-to-medium files.
    :param buffer_size:
        Size of the memory buffer used during streamed copies.
        Only applies when copying via this process (EFS↔S3, EFS↔Local, etc.).
        Ignored for server-side cloud copies.

    :param verify:
        When True:
          • LOCAL → CLOUD:
                - SHA256 both sides (src + dst)
                - Manifest is written at the cloud destination (_manifest.json)
          • CLOUD → LOCAL:
                - Manifest must already exist at source
                - Each file’s SHA256 is checked against manifest entries
                - _manifest.json is copied but skipped during verification

    :return:
        Number of files successfully copied. If source prefix is empty,
        returns 0. Errors propagated to caller unless captured as
        S3CredentialsExpired for AWS credential issues.
    """
    logger.info(called_from())

    # Normalize both URLs (converts bare paths → file:///)
    src_url = normalize_url(src_url)
    dst_url = normalize_url(dst_url)

    # Parse schemes
    src_fs, _ = get_filesystem(src_url)
    dst_fs, _ = get_filesystem(dst_url)

    src_scheme = urlparse(src_url).scheme or "file"
    dst_scheme = urlparse(dst_url).scheme or "file"

    # Split source/dest into (base, prefix)
    src_base, src_prefix = _norm_prefix(src_url)  # e.g. ("file:///","/ngen/.../1_peter")
    dst_base, dst_prefix = _norm_prefix(dst_url)

    # Used ONLY during local→cloud verification.
    # manifest_entries collects file hash entries for writing new manifest.json.
    manifest_entries = [] if verify and dst_scheme != "file" else None

    # Symlink metadata (stored only during local→cloud to recreate symlinks on restore)
    manifest_symlinks = [] if verify and dst_scheme != "file" else None

    # ------------------------------------------------------------
    # Helper to compute SHA256 when verify=True
    # ------------------------------------------------------------
    def compute_sha256(fs, path) -> str:
        h = hashlib.sha256()
        with fs.open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    # ------------------------------------------------------------
    # If verify and source is cloud → load manifest.json
    # ------------------------------------------------------------
    source_manifest_dict = None  # fast lookup dict: rel_path → sha256
    source_manifest_raw = None  # full manifest JSON (includes symlinks)
    if verify and src_scheme != "file":
        manifest_url = join_url(src_base, src_prefix, "_manifest.json")

        # Explicit existence check — DO NOT use fs.find() for this
        if src_fs.exists(manifest_url):
            try:
                with src_fs.open(manifest_url, "r") as mf:
                    source_manifest_raw = json.load(mf)  # keep full structure (files + symlinks)

                # Convert file list to dict for fast hash verification
                source_manifest_dict = {
                    entry["relative"]: entry["sha256"]
                    for entry in source_manifest_raw.get("files", [])
                }

                logger.info(f"Loaded manifest for cloud→local verify: {manifest_url}")

            except Exception as e:
                logger.error(f"Failed to load manifest.json at {manifest_url}: {e}")
                source_manifest_dict = None
        else:
            logger.warning("Verification enabled, but no manifest.json found on source cloud directory.")
            source_manifest_dict = None

    # ------------------------------------------------------------
    # STEP 1 — Generate the file list correctly
    # ------------------------------------------------------------
    def list_local_files(base_path: str) -> list[tuple[str, str]]:
        """
        Return list of (absolute_file_path, relative_path_from_base)
        for a local directory source.
        """
        root_path = urlparse(base_path).path  # file:///... → /path
        out = []
        for dirpath, _, filenames in os.walk(root_path):
            for name in filenames:
                abs_path = os.path.join(dirpath, name)
                rel = os.path.relpath(abs_path, root_path).replace("\\", "/")
                out.append((abs_path, rel))
        return out

    def list_cloud_files(prefix_url: str) -> list[tuple[str, str]]:
        """
        Return list of (full_url, relative_path_from_prefix)
        for a cloud-provider source.

        IMPORTANT:
          fs.find() returns backend-dependent paths:
            • 'bucket/key'
            • 'key'
            • or fully-qualified URLs (s3://bucket/key)

        The implementation normalizes all cases into full URLs by rebuilding
        the provider URL manually (using join_url), rather than using
        normalize_url(), because normalize_url() would incorrectly treat
        provider keys as local paths.
        """
        try:
            all_objs = src_fs.find(prefix_url)
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in ("ExpiredToken", "InvalidAccessKeyId"):
                raise S3CredentialsExpired("Your AWS credentials have expired") from e
            raise

        # Normalize prefix for path comparison
        parsed = urlparse(prefix_url)
        prefix_bucket = parsed.netloc  # "ngwpc-dev"
        prefix_path = parsed.path.lstrip("/")  # "peter/.../1_peter"
        out = []

        for obj in all_objs:
            if obj.endswith("/"):
                continue

            # obj may be:
            #   "bucket/key"            (S3-style)
            #   "key"                   (GCS/other)
            #   "s3://bucket/key"       (full URL)
            # We must rebuild the full provider URL WITHOUT using normalize_url()

            if "://" in obj:
                full = obj
                full_path = urlparse(obj).path.lstrip("/")
            else:
                # Extract the key portion
                if obj.startswith(prefix_bucket + "/"):
                    key = obj.split("/", 1)[1]
                else:
                    key = obj

                # Rebuild full provider URL
                full = join_url(src_base, key)
                full_path = key

            # Compute rel-path strictly from the provider path
            if full_path.startswith(prefix_path):
                rel = full_path[len(prefix_path):].lstrip("/")
            else:
                rel = os.path.basename(full_path)

            out.append((full, rel))

        return out

    src_files = list_local_files(src_url) if src_scheme == "file" else list_cloud_files(src_url)

    if not src_files:
        logger.warning(f"No files found at {src_url}")
        return 0

    # ------------------------------------------------------------
    # SKIP restoring manifest.json when CLOUD → LOCAL with verify=True
    # ------------------------------------------------------------
    if verify and src_scheme != "file":
        before = len(src_files)
        src_files = [(a, r) for (a, r) in src_files if r != "_manifest.json"]
        after = len(src_files)
        if before != after:
            logger.info("Skipped restoring _manifest.json (manifest is used but not copied).")

    logger.info(f"Copying {len(src_files)} files from {src_url} to {dst_url} using {workers} workers")

    # Check server-side cp possibility
    use_server_side = (
            src_scheme == dst_scheme
            and _same_provider(src_fs, dst_fs)
            and _server_side_cp_supported(src_fs)
    )

    # Build destination path from relative path
    def make_dst(rel: str) -> str:
        return join_url(dst_base, dst_prefix, rel)

    # ------------------------------------------------------------
    # STEP 2 — Copy + (optional) verify one file
    # ------------------------------------------------------------
    def _copy_one(abs_src: str, rel_path: str) -> tuple[str, float, int]:
        t0 = time.perf_counter()
        dst_full = make_dst(rel_path)

        # ------------------------------------------------------------
        # Handle symlinks: preserve metadata instead of copying
        # ------------------------------------------------------------
        if src_scheme == "file" and os.path.islink(abs_src):
            target = os.readlink(abs_src)

            # Record symlink for local→cloud write
            if manifest_symlinks is not None:
                manifest_symlinks.append({
                    "relative": rel_path,
                    "target": target,
                })

            logger.info(f"Recorded symlink {rel_path} -> {target}")
            return dst_full, 0.0, -1

        # Make parent directory on destination
        dst_parent = os.path.dirname(urlparse(dst_full).path)
        try:
            dst_fs.mkdirs(join_url(dst_base, dst_parent), exist_ok=True)
        except Exception:
            pass

        # Try to get size (best-effort)
        size_bytes = -1
        try:
            info = src_fs.info(abs_src)
            size_bytes = int(info.get("size", -1))
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in ("ExpiredToken", "InvalidAccessKeyId"):
                raise S3CredentialsExpired("Your AWS credentials have expired") from e
            raise
        except Exception:
            pass

        # Copy file (server-side or streamed)
        try:
            if use_server_side:
                _cp_file_server_side(src_fs, abs_src, dst_full)
            else:
                with src_fs.open(abs_src, "rb") as r, dst_fs.open(dst_full, "wb") as w:
                    shutil.copyfileobj(r, w, length=buffer_size)
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in ("ExpiredToken", "InvalidAccessKeyId"):
                raise S3CredentialsExpired("Your AWS credentials have expired") from e
            raise

        dt = time.perf_counter() - t0

        # Base message (without throughput yet)
        base_msg = f"Copied {abs_src} -> {dst_full} in {dt:.3f}s"

        # Compute throughput if possible
        if size_bytes > 0:
            mib = size_bytes / (1024 * 1024)
            rate = mib / dt if dt > 0 else 0
            throughput = f" ({mib:.2f} MiB @ {rate:.2f} MiB/s)"
        else:
            throughput = ""

        # ------------------------------------------------------------
        # Verification - ensure copy is accurate
        # ------------------------------------------------------------
        if verify:
            # Skip verification for manifest on restore
            if rel_path == "_manifest.json":
                logger.info(f"{base_msg}{throughput} — skipped manifest verification")
                return dst_full, dt, size_bytes

            # CLOUD → LOCAL verification (use manifest for hash lookup)
            if source_manifest_dict and src_scheme != "file":
                expected = source_manifest_dict.get(rel_path)  # O(1) lookup
                if expected is None:
                    raise RuntimeError(f"No manifest entry for {rel_path}")

                # Hash ONLY destination (local)
                dst_hash = compute_sha256(dst_fs, dst_full)

                if expected != dst_hash:
                    raise RuntimeError(f"Verification FAILED for {rel_path}: {expected} != {dst_hash}")

            else:
                # LOCAL → CLOUD verification (hash both)
                src_hash = compute_sha256(src_fs, abs_src)
                dst_hash = compute_sha256(dst_fs, dst_full)

                if src_hash != dst_hash:
                    raise RuntimeError(f"Verification FAILED for {rel_path}: {src_hash} != {dst_hash}")

                # Store manifest entry ONLY for local→cloud case
                if dst_scheme != "file" and manifest_entries is not None:
                    manifest_entries.append({
                        "relative": rel_path,
                        "sha256": dst_hash,
                        "size": size_bytes if size_bytes > 0 else None
                    })

        # ----------------------------
        # UNIFIED LOG LINE
        # ----------------------------
        if verify:
            logger.info(f"{base_msg}{throughput} — verified OK")

        else:
            # Unified no-verify line
            logger.info(f"{base_msg}{throughput}")

        return dst_full, dt, size_bytes

    # ------------------------------------------------------------
    # STEP 4 — Fan-out threads to copy
    # ------------------------------------------------------------
    wall_start = time.perf_counter()
    total_bytes = 0
    completed = 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_copy_one, abs_src, rel) for abs_src, rel in src_files]
        for fut in as_completed(futures):
            _, _, sz = fut.result()
            completed += 1
            if sz > 0:
                total_bytes += sz

    wall = time.perf_counter() - wall_start

    if total_bytes > 0:
        mib = total_bytes / (1024 * 1024)
        rate = mib / wall if wall > 0 else 0
        logger.info(
            f"Copy summary: {completed} files, {mib:.2f} MiB in {wall:.3f}s "
            f"({rate:.2f} MiB/s, workers={workers}, "
            f"{'server-side' if use_server_side else 'streamed'})"
        )
    else:
        logger.info(f"Copy summary: {completed} files in {wall:.3f}s (workers={workers})")

    # ------------------------------------------------------------
    # Write manifest.json ONLY when verify=True AND destination is cloud
    # ------------------------------------------------------------
    if verify and dst_scheme != "file" and manifest_entries:
        try:
            manifest_path = join_url(dst_base, dst_prefix, "_manifest.json")
            with dst_fs.open(manifest_path, "w") as mf:
                mf.write(json.dumps({
                    "files": manifest_entries,
                    "symlinks": manifest_symlinks or [],
                }, indent=2))
            logger.info(f"Wrote manifest: {manifest_path}")
        except Exception as e:
            logger.error(f"Failed to write manifest.json: {e}")

    # ------------------------------------------------------------
    # Recreate symlinks after CLOUD → LOCAL restore
    # (uses source_manifest_raw since it contains symlink metadata)
    # ------------------------------------------------------------
    if (
            verify
            and src_scheme != "file"
            and source_manifest_raw
            and "symlinks" in source_manifest_raw
    ):
        dest_root = urlparse(dst_url).path
        for entry in source_manifest_raw["symlinks"]:
            rel = entry["relative"]
            target = entry["target"]
            link_path = os.path.join(dest_root, rel)

            os.makedirs(os.path.dirname(link_path), exist_ok=True)
            try:
                os.symlink(target, link_path)  # creates symlink even if target missing
                logger.info(f"Restored symlink {rel} -> {target}")
            except Exception as e:
                logger.error(f"Failed to recreate symlink {rel}: {e}")

    return completed


def path_exists(path: str) -> bool:
    """
    Cloud/local agnostic exists() check.
    Works for file://, s3://, gcs://, az://, etc.
    Raises S3CredentialsExpired if AWS credentials are expired.

    :param path: URL or local filesystem path.
    :return: True if path exists, False otherwise.
    """
    if not path:
        return False

    parsed = urlparse(path)
    scheme = parsed.scheme or "file"

    if scheme == "file":
        return os.path.exists(parsed.path or path)

    try:
        fs = fsspec.filesystem(scheme)
        return fs.exists(path)
    except botocore.exceptions.ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ExpiredToken":
            raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
        raise
    except PermissionError as e:
        if "expired" in str(e).lower():
            raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
        raise
    except Exception:
        return False


def is_dir(path: str) -> bool:
    """
    Cloud/local agnostic directory check.
    Works for file://, s3://, gcs://, az://, etc.
    Raises S3CredentialsExpired if AWS credentials are expired.

    :param path: URL or local filesystem path.
    :return: True if path exists and is a directory/prefix, False otherwise.
    """
    parsed = urlparse(normalize_url(path))
    scheme = parsed.scheme or "file"

    if scheme == "file":
        return os.path.isdir(parsed.path or path)

    try:
        fs = fsspec.filesystem(scheme)
        return fs.isdir(path)
    except botocore.exceptions.ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ExpiredToken":
            raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
        raise
    except PermissionError as e:
        if "expired" in str(e).lower():
            raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
        raise
    except Exception:
        return False


# def open_file(path: str, mode: str = "r", **kwargs):
#     """
#     Open a local or cloud file for reading or writing.
#
#     Uses fsspec under the hood, so `s3://`, `gs://`, `az://`, etc. all work.
#     This does not use the caching layer — it always streams directly.
#
#     :param path: Local path or cloud URL.
#     :param mode: File mode, e.g. "r", "rb", "w".
#     :param kwargs: Passed through to fsspec.open().
#     :return: A file-like object.
#     """
#     fs, norm_url = get_filesystem(path)
#     return fs.open(norm_url, mode, **kwargs)


def list_files(path: str, pattern: str = "*.csv") -> list[str]:
    """
    List files under a local or cloud directory and return normalized URLs.

    Supports both local paths and cloud URLs (file://, s3://, gs://, az://, etc.).
    Raises S3CredentialsExpired if AWS credentials are expired.
    Raises FileNotFoundError if the given path is not a directory.

    Behavior:
      * Uses fsspec to glob all files under the given path that match the pattern.
      * Filters out directories (entries ending with "/").
      * Normalizes outputs so all results are fully-qualified URLs:
          - Local files → file:///absolute/path/to/file
          - Cloud keys  → scheme://bucket/key
      * Returns only files; directories are skipped.

    :param path: Directory path (local or cloud).
    :param pattern: Glob pattern for files (default "*.csv").
    :return: List of normalized file URLs.
    """
    fs, norm_url = get_filesystem(path)
    # Ensure trailing slash on directory
    norm_url = norm_url.rstrip("/")
    try:
        if not fs.isdir(norm_url):
            raise FileNotFoundError(f"{norm_url} is not a directory")
    except botocore.exceptions.ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ExpiredToken":
            raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
        raise
    except PermissionError as e:
        if "expired" in str(e).lower():
            raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
        raise

    try:
        # fsspec.glob may return fully-qualified URLs or bare keys
        files = fs.glob(f"{norm_url}/{pattern}")
    except botocore.exceptions.ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ExpiredToken":
            raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
        raise
    except PermissionError as e:
        if "expired" in str(e).lower():
            raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
        raise

    out_files = []
    base, _ = _norm_prefix(norm_url)
    scheme = urlparse(norm_url).scheme or "file"

    for f in files:
        if f.endswith("/"):  # skip dirs
            continue

        # Case 1: already a fully-qualified URL
        if "://" in f:
            # Already fully-qualified (e.g., s3://bucket/key or file:///...)
            candidate = f
        elif scheme == "file":
            # Local filesystem: f is relative, join with real directory path
            dir_path = urlparse(norm_url).path or norm_url
            candidate = os.path.join(dir_path, f)
        else:
            # Cloud provider: f may be "bucket/key" or just "key"
            if f.startswith(base.split("://", 1)[1] + "/"):
                # Case: "bucket/key"
                candidate = f"{base}/{f.split('/', 1)[1]}"
            else:
                # Case: plain "key"
                candidate = f"{base}/{f}"

        # Ensure consistent normalization (e.g., file:/// for local paths)
        out_files.append(normalize_url(candidate))

    return out_files


# ----------------------------------------------------------------------
# Caching + localization
# ----------------------------------------------------------------------

def _is_remote(url_or_path: str) -> bool:
    """
    Return True if the given path is remote (cloud storage).

    Remote schemes include: s3, gs/gcs, az/abfs/abfss.
    """
    p = urlparse(url_or_path)
    return bool(p.scheme) and p.scheme.lower() in _REMOTE_SCHEMES


def _cache_key(url: str) -> str:
    """
    Generate a stable SHA256 hash for a URL.

    Used as the basename for cached files and metadata sidecars.
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _meta_path(cache_dir: str, key: str) -> str:
    """
    Construct the path to the JSON metadata file in the cache directory.
    Metadata is stored alongside cached files to record etag/size/mtime.
    """
    return os.path.join(cache_dir, f"{key}.meta.json")


def _data_path(cache_dir: str, key: str, suffix=".gpkg") -> str:
    """
    Construct the path to the cached file contents in the cache directory.
    The suffix is typically the file type (.gpkg, .csv, etc.).
    """
    return os.path.join(cache_dir, f"{key}{suffix}")


def _read_meta(path: str) -> dict:
    """
    Read a JSON metadata file, returning {} if unreadable or missing.
    """
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_meta(path: str, meta: dict) -> None:
    """
    Atomically write JSON metadata to disk for a cached file.
    Ensures partially-written metadata files aren’t left behind.
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f)
    os.replace(tmp, path)


def _info_for(fs, url: str) -> dict:
    """
    Wrapper for fs.info(url) that returns {} instead of raising.
    Safe for missing files or backends with limited support.
    """
    try:
        return fs.info(url)
    except Exception:
        return {}


@contextmanager
def localize_to_path(
        url_or_path: str,
        *,
        enable_cache: bool = True,
        suffix: str = ".gpkg",
) -> Iterator[Tuple[str, str]]:
    """
    Yield (original_path, local_path) for local or remote resources.
    Raises S3CredentialsExpired if AWS credentials are expired.

    Local paths:
      - Yields (p, p) without copying or caching.

    Remote URLs (s3/gs/az/abfs):
      - If enable_cache=True (default): persist under CLOUD_CACHE_DIR (/var/tmp/fsspec-cache)
        and reuse across runs.
        * Cache validation uses provider metadata: {etag, size, mtime}.
        * Cache hit when local file exists and metadata matches → reuse cached file.
        * Cache miss → download to <basename>.downloading, then atomically rename to <basename>
          and update metadata.
        * Metadata stored in JSON sidecar with {etag, size, mtime, url, t}.
        * If two different remote files share the same basename, the newer download
          will overwrite the older one.  We don't expect this to happen

      - If enable_cache=False: download into a NamedTemporaryFile and delete on exit.
        (Use this for one-shot reads that do not need persistence.)

    Typical use cases:
      * Geopackage inputs or forcing/observational CSVs that are read multiple times in a workflow —
        avoid redundant downloads by reusing the cached copy.
      * Pipelines where a file is validated (first pass) and then copied or transformed (second pass).
        Both passes will use the same cached local file.
      * Unit tests or short-lived processes may set enable_cache=False to avoid polluting /var/tmp.

    This mechanism is separate from copy_tree(); copy_tree streams directly and
    does not populate this cache.
    """
    orig = url_or_path
    if not _is_remote(orig):
        # Local path: no caching layer involved
        yield orig, orig
        return

    os.makedirs(CLOUD_CACHE_DIR, exist_ok=True)
    p = urlparse(orig)
    scheme = p.scheme.lower()
    fs = fsspec.filesystem(scheme)

    # Remote metadata to validate cache freshness
    meta_remote = _info_for(fs, orig)
    etag = str(meta_remote.get("ETag") or meta_remote.get("etag") or "")
    size = int(meta_remote.get("Size") or meta_remote.get("size") or -1)

    lm = meta_remote.get("LastModified") or meta_remote.get("last_modified")
    if isinstance(lm, datetime.datetime):
        mtime = int(lm.timestamp())
    elif isinstance(lm, (int, float)):
        mtime = int(lm)
    elif isinstance(lm, str):
        try:
            mtime = int(float(lm))
        except Exception:
            mtime = 0
    else:
        mtime = 0

    # Always use original basename for local cache filename
    basename = Path(p.path).name
    data_path = os.path.join(CLOUD_CACHE_DIR, basename)
    meta_path = data_path + ".meta.json"

    if enable_cache:
        meta_local = _read_meta(meta_path)
        ok = (
                os.path.exists(data_path)
                and meta_local.get("etag") == etag
                and meta_local.get("size") == size
                and meta_local.get("mtime") == mtime
        )

        if ok:
            # Cache hit
            yield orig, data_path
            return

        # Cache miss → download then promote
        tmp_download = data_path + ".downloading"
        logger.info(f"Downloading remote file to cache: {orig} → {data_path}")
        try:
            # Use fs.get to persist efficiently; same-bucket copies may be server-side
            fs.get(orig, tmp_download)
        except botocore.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ExpiredToken":
                raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
            raise
        except PermissionError as e:
            if "expired" in str(e).lower():
                raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
            raise

        try:
            os.replace(tmp_download, data_path)
            _write_meta(meta_path, {
                "etag": etag,
                "size": size,
                "mtime": mtime,
                "url": orig,
                "t": time.time()
            })
            yield orig, data_path
            return
        finally:
            if os.path.exists(tmp_download):
                os.remove(tmp_download)

    # Fallback: temp file if cache disabled
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmpf:
        tmp_path = tmpf.name
    try:
        logger.info(f"Downloading remote file to temp: {orig} → {tmp_path}")
        try:
            fs.get(orig, tmp_path)
        except botocore.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ExpiredToken":
                raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
            raise
        except PermissionError as e:
            if "expired" in str(e).lower():
                raise S3CredentialsExpired("Your AWS S3 credentials are expired") from e
            raise
        yield orig, tmp_path
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def check_aws_credentials(*, timeout_seconds: int = 3) -> None:
    """
    Fast sanity check that AWS credentials are present and valid.

    Raises S3CredentialsExpired if credentials are missing, expired,
    or otherwise invalid. Intended for startup / readiness checks.
    """
    try:
        sts = boto3.client(
            "sts",
            config=boto3.session.Config(
                connect_timeout=timeout_seconds,
                read_timeout=timeout_seconds,
                retries={"max_attempts": 1},
            ),
        )

        identity = sts.get_caller_identity()

        logger.info(
            "AWS credentials OK: account=%s arn=%s",
            identity.get("Account"),
            identity.get("Arn"),
        )

    except (botocore.exceptions.NoCredentialsError, botocore.exceptions.PartialCredentialsError):
        # Boto3 could not construct a usable credential set locally
        # (missing, incomplete, unreadable, or unresolved credentials).
        # No request was made to AWS.
        raise S3CredentialsExpired("AWS credentials are missing or incomplete") from None

    except botocore.exceptions.ClientError as e:
        # Credentials were constructed successfully and a request reached AWS STS,
        # but STS rejected the request due to invalid, expired, or otherwise
        # unacceptable credentials.
        code = e.response.get("Error", {}).get("Code", "Unknown")

        if code in {"ExpiredToken", "InvalidClientTokenId"}:
            raise S3CredentialsExpired("AWS credentials are expired or invalid") from None

        # Any other STS error at startup still indicates unusable credentials
        # (e.g. wrong account, broken assume-role chain, signature issues).
        raise S3CredentialsExpired(f"AWS credential check failed: {code}") from None
