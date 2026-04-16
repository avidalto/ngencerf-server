"""
Job Retrieval Endpoints for Calibration, Forecast, Hindcast, and Verification
=============================================================================

This module provides a unified interface for retrieving job records across the
CERF workflow, including Calibration, Forecast, Hindcast, and Verification runs.

All endpoints support:
  • Server-side filtering
  • Server-side sorting
  • Pagination with offset + limit
  • Consistent date and ID ranges for client-side filtering
  • Read-only execution to reduce database contention

Calibration retrieval is implemented by `get_jobs()`.
Forecast, Hindcast, and Verification retrieval are implemented by
`get_forecast_jobs_internal()`, `get_hindcast_jobs_internal()`, and
`get_verification_jobs_internal()`.

Verification-specific query path resolution is centralized in
`get_verification_parent_paths()`.

Request payload shape
---------------------
Use this general shape for every request (omit keys you are not using):

    limit: integer page size (e.g., 25)
    offset: integer row offset (0-based)
    filters: object with any of:
        gage_id: string
        status: array of status labels (e.g. ["Done", "Failed"])
        module_filter:
            operator: "and" | "or"
            modules: list of module names
        date_filter:
            operator: "before" | "after" | "between"
            create_date: ISO-8601 datetime (e.g., "2025-01-01T12:34:56Z")
            start_date: ISO-8601 datetime (e.g., "2025-01-01T00:00:00-05:00")
            end_date: ISO-8601 datetime (e.g., "2025-02-01T23:59:59Z")
        id_filter:
            operator: "before" | "after" | "between"
            id: integer                 # before/after
            start_id: integer           # between
            end_id: integer             # between
        include_archived: boolean
    sort:
        field: allowed sort field name
        direction: "asc" or "desc"

Calibration-only request keys
-----------------------------
    ids_only: boolean

Verification-only request keys
------------------------------
    verification_job_type: "forecast" | "hindcast"

Do not send empty/default filters or sort objects.

Examples
--------

Full example:

    {
        "limit": 25,
        "offset": 0,
        "filters": {
            "gage_id": "01544887",
            "status": ["Done", "Failed"],
            "module_filter": {
                "operator": "and",
                "modules": ["CFE-X", "Noah-OWP-Modular"]
            },
            "date_filter": {
                "operator": "after",
                "create_date": "2025-01-01T00:00:00Z"
            },
            "id_filter": {
                "operator": "before",
                "id": 500
            },
            "include_archived": false
        },
        "sort": { "field": "submit_date", "direction": "asc" }
    }

Date range example:

    {
        "limit": 25,
        "offset": 0,
        "filters": {
            "date_filter": {
                "operator": "between",
                "start_date": "2025-01-01T00:00:00-05:00",
                "end_date": "2025-02-01T23:59:59Z"
            }

        }
    }

Minimal example:

    { "limit": 25, "offset": 0 }

Verification example:

    {
        "limit": 25,
        "offset": 0,
        "verification_job_type": "forecast"
    }

Key concepts
------------

Status handling (Calibration only)
    Calibration jobs compute a deterministic `combined_status` derived from:
      - the calibration status, and
      - the statuses of VALID_CONTROL and VALID_BEST validations (if present).

    User-supplied status filters for calibration jobs apply to `combined_status`.

Filtering
    All job types support gage, domain, status, module membership, date, ID, and
    archive toggles.

Sorting
    Sorting uses server-approved fields defined in Enum classes
    (CalibrationSortField, ForecastSortField, HindcastSortField, VerificationSortField).
    Multi-field sorts are supported.

Pagination
    Offset/limit pagination applies after filtering and sorting.
    total_count is returned before pagination.

Range metadata
    Each endpoint returns:
        • date_range = [min_created_at, max_created_at]
        • id_range   = [min_id, max_id]

    These ranges are computed before user-supplied filters and are restricted
    only by the base ownership constraint, and any endpoint-level run_status
    restriction where applicable.

Read-only execution
    All retrieval runs inside a read-only transaction wrapper to reduce
    lock contention.
"""

import json
import logging
from typing import Any, Type, cast, Literal

from django.db.models import Q, Exists, OuterRef, Count, Subquery, When, CharField, Value, F, Case, Sum, IntegerField, QuerySet, Min, Max
from django.db.models.functions import Lower
from drf_spectacular.utils import extend_schema, OpenApiResponse, PolymorphicProxySerializer
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from calibration.enums import GetValidationJobsScope, StatusEnum, ValidationType
from calibration.enums_vanilla import CalibrationSortField, ForecastSortField, VerificationSortField, HindcastSortField
from calibration.models import CalibrationFormulation, CalibrationRun, ValidationRun, VerificationRun, CustomUser, IterationParameter, ForecastRun, \
    CalibrationStopCriteria, HindcastRun
from calibration.models.base_run import BaseRun
from calibration.util.caching import get_cached_modules_by_id
from calibration.util.calibration_validators import ErrorResponseSerializer, \
    GetCalibrationJobsResponseSerializer, CalibrationPaginationSerializer, \
    GetCalibrationJobIDsResponseSerializer, EmptySerializer, \
    GetGagesResponseSerializer, GetGagesRequestSerializer, GetCalibrationJobsSummaryResponseSerializer, GetValidationJobsResponseSerializer, \
    CalibrationRunIdSerializer, ForecastPaginationSerializer, GetForecastJobsResponseSerializer, GetVerificationJobsResponseSerializer, \
    VerificationPaginationSerializer, GetHindcastJobsResponseSerializer, HindcastPaginationSerializer, GetVerificationGagesRequestSerializer
from calibration.views.calibration_download_views import downloadable_statuses
from calibration.views.called_from import get_caller_name
from calibration.views.common import handle_exceptions, validate_request, validate_response, truncate_large_fields, get_user_email, get_elapsed_str, \
    readonly_transaction, get_calibration_run

logger = logging.getLogger(__name__)


@extend_schema(
    request=CalibrationPaginationSerializer,
    responses={
        200: OpenApiResponse(
            response=PolymorphicProxySerializer(
                component_name='GetCalibrationJobsForEvaluationResponse',
                serializers=[
                    GetCalibrationJobsResponseSerializer,
                    GetCalibrationJobIDsResponseSerializer,
                ],
                resource_type_field_name=None,
            ),
            description="Full job list or ID-only list depending on ids_only flag"
        ),
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get all Calibration jobs for Evaluation"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_calibration_jobs_for_evaluation(request: Request) -> Response:
    """
    Retrieves calibration jobs that are DONE, FAILED, CANCELLED, or SERVER_ERROR for evaluation purposes.

    :param request: The HTTP request object.
    :return: JSON response with a list of calibration jobs or error information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationPaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")
    ids_only = validator.get("ids_only")
    filters, sort = _normalize_filters_and_sort(filters, sort)

    jobs, total_count, date_range, id_range = get_jobs(
        auth_user(request),
        run_status=[StatusEnum.DONE, StatusEnum.FAILED, StatusEnum.CANCELLED, StatusEnum.SERVER_ERROR],
        include_validation_data=GetValidationJobsScope.STATUS,
        require_both_validations_done=True,
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort,
        ids_only=ids_only
    )

    response: dict[str, Any] = {
        "jobs": jobs,
        "total_count": total_count
    }
    if total_count > 0:
        response['date_range'] = date_range
        response['id_range'] = id_range

    serializer_class = GetCalibrationJobIDsResponseSerializer if ids_only else GetCalibrationJobsResponseSerializer

    response_validator, error_response = validate_response(serializer_class, response, fields_to_truncate=['jobs'], max_length=10)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=CalibrationPaginationSerializer,
    responses={
        200: OpenApiResponse(
            response=PolymorphicProxySerializer(
                component_name='GetCalibrationJobsForEvaluationResponse',
                serializers=[
                    GetCalibrationJobsResponseSerializer,
                    GetCalibrationJobIDsResponseSerializer,
                ],
                resource_type_field_name=None,
            ),
            description="Full job list or ID-only list depending on ids_only flag"
        ),
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get all Calibration jobs for Forecast/Hindcast"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_calibration_jobs_for_forecast(request: Request) -> Response:
    """
    Returns calibration jobs eligible for forecasting/hindcasting purposes.

    Only DONE calibration jobs are returned, and VALID_CONTROL and VALID_BEST must both exist and be DONE.

    :param request: The HTTP request object.
    :return: JSON response with a list of calibration jobs or error information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationPaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")
    ids_only = validator.get("ids_only")
    filters, sort = _normalize_filters_and_sort(filters, sort)

    jobs, total_count, date_range, id_range = get_jobs(
        auth_user(request),
        run_status=[StatusEnum.DONE],
        include_validation_data=None,
        require_both_validations_done=True,
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort,
        ids_only=ids_only
    )

    response: dict[str, Any] = {
        "jobs": jobs,
        "total_count": total_count
    }
    if total_count > 0:
        response['date_range'] = date_range
        response['id_range'] = id_range

    serializer_class = GetCalibrationJobIDsResponseSerializer if ids_only else GetCalibrationJobsResponseSerializer

    response_validator, error_response = validate_response(serializer_class, response, fields_to_truncate=['jobs'], max_length=10)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=CalibrationPaginationSerializer,
    responses={
        200: OpenApiResponse(
            response=PolymorphicProxySerializer(
                component_name='GetCalibrationJobsResponse',
                serializers=[
                    GetCalibrationJobsResponseSerializer,
                    GetCalibrationJobIDsResponseSerializer,
                ],
                resource_type_field_name=None,
            ),
            description="Full job list or ID-only list depending on ids_only flag"
        ),
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get all calibration jobs"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_calibration_jobs(request: Request) -> Response:
    """
    Return all calibration jobs for the authenticated user.

    Archived jobs are excluded unless filters.include_archived is explicitly true.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationPaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")
    ids_only = validator.get("ids_only")
    filters, sort = _normalize_filters_and_sort(filters, sort)
    include_modules = validator.get("include_modules")

    jobs, total_count, date_range, id_range = get_jobs(
        auth_user(request),
        run_status=list(StatusEnum),
        include_validation_data=GetValidationJobsScope.STATUS,
        include_modules=include_modules,
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort,
        ids_only=ids_only
    )

    response: dict[str, Any] = {
        "jobs": jobs,
        "total_count": total_count
    }
    if total_count > 0:
        response['date_range'] = date_range
        response['id_range'] = id_range

    serializer_class = GetCalibrationJobIDsResponseSerializer if ids_only else GetCalibrationJobsResponseSerializer

    response_validator, error_response = validate_response(serializer_class, response, fields_to_truncate=['jobs'], max_length=10)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


def _normalize_filters_and_sort(filters: dict | None, sort: dict | None) -> tuple[dict | None, dict | None]:
    """
    Normalize and sanitize incoming filter and sort payloads.

    Goal: treat empty/blank inputs as "not provided" so downstream query-building code
    can assume that any remaining filter/sort keys are meaningful.

    Notes:
      - This does NOT enforce semantic correctness (the serializers do that).
        It only strips empties and drops incomplete nested filter objects.
      - We intentionally allow "optional" filters to be absent (or empty) without error.

    :param filters: Optional dictionary of filter parameters (may include nested objects).
    :param sort: Optional dictionary specifying sorting field and direction.
    :return: Tuple of (normalized_filters, normalized_sort) with blanks stripped out.
    """
    if filters is not None:
        filters_dict: dict[str, Any] = filters

        # Remove top-level keys that are "empty" so they don't accidentally enable logic paths.
        # Examples of values we treat as empty: "", [], {}, None
        filters_dict = {k: v for k, v in filters_dict.items() if v not in ("", [], {}, None)}

        # status is a list of strings; drop blank/whitespace entries. If it becomes empty, remove it.
        if isinstance(filters_dict.get("status"), list):
            filters_dict["status"] = [s for s in filters_dict["status"] if isinstance(s, str) and s.strip() != ""]
            if not filters_dict["status"]:
                filters_dict.pop("status", None)

        # module_filter is a nested object; normalize modules list by dropping blanks.
        # If modules becomes empty, the module filter is effectively not provided.
        if "module_filter" in filters_dict and filters_dict["module_filter"]:
            mf = filters_dict["module_filter"]
            modules = mf.get("modules") or []
            modules = [m for m in modules if isinstance(m, str) and m.strip() != ""]
            mf["modules"] = modules
            if not modules:
                filters_dict.pop("module_filter", None)

        # date_filter: only keep it if it has the required fields for the chosen operator.
        # - between: must have BOTH start_date and end_date
        # - before/after: must have operator and create_date
        if "date_filter" in filters_dict and filters_dict["date_filter"]:
            date_filter = filters_dict["date_filter"]
            op = (date_filter.get("operator") or "").lower()

            if op == "between":
                # Require both start and end
                if not date_filter.get("start_date") or not date_filter.get("end_date"):
                    filters_dict.pop("date_filter", None)
            elif not date_filter.get("operator") or not date_filter.get("create_date"):
                # for 'before' / 'after', require a single value
                filters_dict.pop("date_filter", None)

        # id_filter: only keep it if it has the required fields for the chosen operator.
        # - between: must have BOTH start_id and end_id (and they can be 0, so check is None)
        # - before/after: must have operator and id (id can be 0, so check is None)
        if "id_filter" in filters_dict and filters_dict["id_filter"]:
            id_filter = filters_dict["id_filter"]
            op = (id_filter.get("operator") or "").lower()

            if op == "between":
                # Require both start and end
                if id_filter.get("start_id") is None or id_filter.get("end_id") is None:
                    filters_dict.pop("id_filter", None)
            elif not id_filter.get("operator") or id_filter.get("id") is None:
                # for 'before' / 'after', require a single id value
                filters_dict.pop("id_filter", None)

        # If we stripped everything, treat as "no filters".
        if not filters_dict:
            filters = None
        else:
            filters = filters_dict

    # sort: if field is blank/whitespace or sort is not a dict, treat as "no sort".
    if not isinstance(sort, dict):
        sort = None
    elif sort and (not sort.get("field") or str(sort.get("field")).strip() == ""):
        sort = None

    return filters, sort


def _apply_shared_filters(
        query: Q, filters: dict, *,
        gage_prefix: str,
        module_prefix: str,
        status_field: str,
        created_field: str,
        archived_field: str = "is_archived",
        domain_field: str | None = None,
        allow_module_and: bool = True
) -> Q:
    """
    Apply shared filter logic for Calibration, Forecast, Hindcast and Verification jobs.

    For Verification queries, the caller must first resolve the correct ORM path
    set based on verification_job_type.

    :param query: Base Q object to filter (e.g., ownership constraint).
    :param filters: Dictionary of filters passed by the client.
    :param gage_prefix: ORM prefix path to gage_id (e.g., 'gage__' or 'calibration_run__gage__').
    :param module_prefix: ORM prefix path to module relationship (e.g., 'calibrationformulation__').
    :param status_field: ORM field path for status filtering (e.g., 'status__in').
    :param created_field: ORM field path to the created_at date field.
    :param archived_field: ORM field path to the archive flag field (default 'is_archived').
    :param domain_field: ORM field path to the domain name field (optional).
    :return: Updated Q object with all applicable filters applied.

    Notes:
      - include_archived: if true, include archived jobs; otherwise (false or missing), exclude them.
      - module_filter.operator="and" is only supported when the outer queryset is CalibrationRun.
        For ForecastRun, HindcastRun and VerificationRun, "and" is treated as "or".
      - Verification-specific ORM prefixes are resolved upstream before calling this helper.
    """
    if not filters:
        return query

    # ───── Gage filter ─────
    if "gage_id" in filters and filters["gage_id"]:
        query &= Q(**{f"{gage_prefix}gage_id": filters["gage_id"]})

    # ───── Domain filter ─────
    if domain_field and filters.get("domain_name"):
        # Case-insensitive exact match to avoid surprises
        query &= Q(**{f"{domain_field}__iexact": filters["domain_name"]})

    # ───── Status filter ─────
    if "status" in filters and filters["status"]:
        query &= Q(**{status_field: [StatusEnum.from_name(s).db_instance for s in filters["status"]]})

    # ───── Module filter ─────
    if "module_filter" in filters:
        mf = filters["module_filter"]
        modules = mf.get("modules") or []
        operator = (mf.get("operator") or "and").lower()

        if modules:
            modules_by_name = {m.name: m.id for m in get_cached_modules_by_id().values()}
            module_ids = [modules_by_name[name] for name in modules if name in modules_by_name]

            if module_ids:
                # NOTE: "and" module filtering is only correct when the outer queryset is CalibrationRun.
                # This subquery uses OuterRef("id") as the calibration_run_id, which is true only for
                # CalibrationRun (pk == calibration_run_id). For ForecastRun, HindcastRun, and VerificationRun,
                # the outer "id" is NOT the calibration_run_id, so "and" will not work correctly there.
                # For Forecast/Hindcast/Verification, treat "and" as "or" (or reject it) at the endpoint layer.
                if operator == "and" and allow_module_and:
                    subquery = (
                        CalibrationFormulation.objects
                        .filter(
                            calibration_run_id=OuterRef("id"),
                            module_id__in=module_ids
                        )
                        .values("calibration_run_id")
                        .annotate(match_count=Count("module_id", distinct=True))
                        .filter(match_count=len(module_ids))
                    )
                    query &= Q(Exists(subquery))
                else:
                    query &= Q(**{f"{module_prefix}module_id__in": module_ids})

    # ───── Date filter ─────
    if "date_filter" in filters:
        date_info = filters["date_filter"]
        operator = (date_info.get("operator") or "").lower()

        if operator == "before":
            date_value = date_info.get("create_date")
            if date_value:
                query &= Q(**{f"{created_field}__lte": date_value})

        elif operator == "after":
            date_value = date_info.get("create_date")
            if date_value:
                query &= Q(**{f"{created_field}__gte": date_value})

        elif operator == "between":
            start_date = date_info.get("start_date")
            end_date = date_info.get("end_date")
            if start_date and end_date:
                query &= Q(**{f"{created_field}__gte": start_date, f"{created_field}__lte": end_date})

    # ───── ID filter ─────
    if "id_filter" in filters:
        id_info = filters["id_filter"]
        operator = (id_info.get("operator") or "").lower()

        if operator == "before":
            id_value = id_info.get("id")
            if id_value is not None:
                query &= Q(id__lte=id_value)

        elif operator == "after":
            id_value = id_info.get("id")
            if id_value is not None:
                query &= Q(id__gte=id_value)

        elif operator == "between":
            start_id = id_info.get("start_id")
            end_id = id_info.get("end_id")
            if start_id is not None and end_id is not None:
                query &= Q(id__gte=start_id, id__lte=end_id)

    # ───── Archived toggle ─────
    # Default behavior: exclude archived unless include_archived is explicitly true.
    if not filters.get("include_archived", False):
        query &= Q(**{archived_field: False})

    return query


def apply_calibration_filters(query: Q, filters: dict[str, Any]) -> Q:
    """
    Apply standard calibration filters to a CalibrationRun queryset,
    excluding 'status' because it's handled later on the derived
    'combined_status' annotation.

    :param query: Base Q object (typically includes ownership constraint).
    :param filters: Dictionary of filter parameters (gage_id, domain_name, modules, date_filter, id_filter, etc.).
    :return: Q object with calibration-specific filters applied.
    """
    # Make a shallow copy and remove 'status'
    filters = {k: v for k, v in filters.items() if k != "status"}

    return _apply_shared_filters(
        query, filters,
        gage_prefix="gage__",
        module_prefix="calibrationformulation__",
        status_field="status__in",  # not used, since 'status' removed
        created_field="created_at",
        archived_field="is_archived",
        domain_field="gage__domain__name",
        allow_module_and=True
    )


def apply_forecast_filters(query: Q, filters: dict) -> Q:
    """
    Apply standard Forecast/Hindcast filters to a ForecastRun or HindcastRun queryset.

    Notes:
      - module_filter.operator="and" is not supported for Forecast/Hindcast/Verification
        and is treated as "or" by the shared filter logic.

    :param query: Base Q object (e.g., Q(calibration_run__owner=user)).
    :param filters: Dictionary of filter parameters (gage_id, domain_name, status, modules, date_filter, id_filter, etc.).
    :return: Q object with forecast/hindcast-specific filters applied.
    """
    return _apply_shared_filters(
        query, filters,
        gage_prefix="calibration_run__gage__",
        module_prefix="calibration_run__calibrationformulation__",
        status_field="status__in",
        created_field="created_at",
        archived_field="calibration_run__is_archived",
        domain_field="calibration_run__gage__domain__name",
        allow_module_and=False
    )


def apply_verification_filters(
        query: Q,
        filters: dict,
        verification_job_type: Literal['forecast', 'hindcast'],
) -> Q:
    """
    Apply standard verification filters to a VerificationRun queryset.

    The ORM path used for gage, module, archive, and domain filtering depends on
    whether the verification job belongs to a ForecastRun or a HindcastRun.

    Notes:
      - module_filter.operator="and" is only supported for Calibration jobs.
        For Forecast, Hindcast, and Verification jobs, it is treated as "or"
        by the shared filter logic.

    :param query: Base Q object constrained to the authenticated user's verification jobs.
    :param filters: Dictionary of filter parameters (gage_id, domain_name, status,
        modules, date_filter, id_filter, include_archived, etc.).
    :param verification_job_type: Parent job type for the verification jobs being
        queried. Must be either 'forecast' or 'hindcast'.
    :return: Q object with verification-specific filters applied.
    """
    paths = get_verification_parent_paths(verification_job_type)

    return _apply_shared_filters(
        query, filters,
        gage_prefix=paths["gage_prefix"],
        module_prefix=paths["module_prefix"],
        status_field="status__in",
        created_field="created_at",
        archived_field=paths["archived_field"],
        domain_field=paths["domain_field"],
        allow_module_and=False
    )


def resolve_sort(
        sort: dict | None,
        enum_class: Type[CalibrationSortField | ForecastSortField | HindcastSortField | VerificationSortField]
) -> list[str]:
    """
    Convert the validated client-provided sort object into a Django `order_by` argument list.

    - Supports single- and multi-field sorting (e.g., 'period' maps to two ORM fields).
    - If 'direction' is 'desc', all fields are prefixed with '-'.
    - Defaults to ['-id'] if no sort provided.

    This function translates the UI-provided sort configuration into the corresponding ORM field
    name(s) used for ordering querysets.
    The mapping between user-facing fields and database columns is defined by the respective Enum (e.g., CalibrationSortField,
    ForecastSortField, HindcastSortField, VerificationSortField).

    Assumptions (enforced by upstream serializers and enum validators):
      - `sort["field"]` is a valid string representation of an existing enum member.
      - It can be safely converted via `enum_class.from_name()`.
      - `enum_class` must be a subclass defining `.orm_field` mappings.
      - If `sort` is missing or empty, the function defaults to ['-id'] (descending by primary key).
      - If direction is `"desc"`, a leading "-" is applied to each field.

    Examples:
      >>> resolve_sort({"field": "gage_id", "direction": "asc"}, CalibrationSortField)
      ['gage__gage_id']
      >>> resolve_sort({"field": "period", "direction": "desc"}, CalibrationSortField)
      ['-calibration_start_period', '-calibration_end_period']

    :param sort: Dictionary with 'field' and optional 'direction' ('asc' or 'desc').
    :param enum_class: Enum class defining valid sort fields and their ORM column names.
    :return: List of Django-compatible order_by fields (e.g., ['-submit_date'] or ['gage__gage_id']).
    """
    if not sort or "field" not in sort:
        return ["-id"]

    # Convert validated string → enum member
    member = enum_class.from_name(sort["field"])

    direction = sort.get("direction", "asc").lower()
    orm_field = member.orm_field

    # Normalize single vs multi-field sorts
    orm_fields = [orm_field] if isinstance(orm_field, str) else orm_field

    # Apply direction prefix to all fields
    if direction == "desc":
        orm_fields = [f"-{f}" for f in orm_fields]

    return orm_fields


@extend_schema(
    request=GetGagesRequestSerializer,
    responses={
        200: GetGagesResponseSerializer,
        400: OpenApiResponse(response=ErrorResponseSerializer, description="Validation error or parsing error"),
        500: OpenApiResponse(response=ErrorResponseSerializer, description="Internal server error"),
    },
    description="Get distinct gage_ids for all Calibration jobs (optional domain + include_archived)"
)
@api_view(["POST", "GET"])
@handle_exceptions
def get_calibration_gages(request: Request) -> Response:
    data = request.data if request.method == "POST" else request.query_params.dict()
    logger.debug(f"{get_caller_name()}() request from {get_user_email(request)} - {data}")

    validator, error_return = validate_request(GetGagesRequestSerializer, data)
    if error_return:
        return error_return

    domain_name = validator.get("domain_name") or None
    include_archived = validator.get("include_archived")

    # Domain is optional. If not provided, include gages across all domains.
    gages = get_gages(
        auth_user(request),
        run_status=list(StatusEnum),
        require_both_validations_done=False,
        include_archived=include_archived,
        domain_name=domain_name
    )

    response = {"gages": gages}
    response_validator, error_response = validate_response(GetGagesResponseSerializer, response, fields_to_truncate=["gages"], max_length=10)
    if error_response:
        return error_response

    logger.debug(
        f"Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - "
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["gages"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=GetGagesRequestSerializer,
    responses={
        200: GetGagesResponseSerializer,
        400: OpenApiResponse(response=ErrorResponseSerializer, description="Validation error or parsing error"),
        500: OpenApiResponse(response=ErrorResponseSerializer, description="Internal server error"),
    },
    description="Get distinct gage_ids for DONE Calibration jobs eligible for Forecast/Hindcast (optional domain + include_archived)"
)
@api_view(["POST", "GET"])
@handle_exceptions
def get_calibration_gages_for_forecast(request: Request) -> Response:
    """
    Get distinct gage_ids for Calibration jobs eligible for Forecast/Hindcast (optional domain + include_archived).
    """
    data = request.data if request.method == "POST" else request.query_params.dict()
    logger.debug(f"{get_caller_name()}() request from {get_user_email(request)} - {data}")

    validator, error_return = validate_request(GetGagesRequestSerializer, data)
    if error_return:
        return error_return

    domain_name = validator.get("domain_name") or None
    include_archived = validator.get("include_archived")

    # Domain is optional. If not provided, include gages across all domains.
    gages = get_gages(
        auth_user(request),
        run_status=[StatusEnum.DONE],
        require_both_validations_done=True,
        include_archived=include_archived,
        domain_name=domain_name
    )

    response = {"gages": gages}
    response_validator, error_response = validate_response(GetGagesResponseSerializer, response, fields_to_truncate=["gages"], max_length=10)
    if error_response:
        return error_response

    logger.debug(
        f"Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - "
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["gages"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=GetGagesRequestSerializer,
    responses={
        200: GetGagesResponseSerializer,
        400: OpenApiResponse(response=ErrorResponseSerializer, description="Validation error or parsing error"),
        500: OpenApiResponse(response=ErrorResponseSerializer, description="Internal server error"),
    },
    description="Get distinct gage_ids for Calibration jobs eligible for Evaluation (no filters, no pagination)"
)
@api_view(["POST", "GET"])
@handle_exceptions
def get_calibration_gages_for_evaluation(request: Request) -> Response:
    """
    Get distinct gage_ids for Calibration jobs eligible for Evaluation (optional domain + include_archived).
    """
    data = request.data if request.method == "POST" else request.query_params.dict()
    logger.debug(f"{get_caller_name()}() request from {get_user_email(request)} - {data}")

    validator, error_return = validate_request(GetGagesRequestSerializer, data)
    if error_return:
        return error_return

    domain_name = validator.get("domain_name") or None
    include_archived = validator.get("include_archived")

    # Domain is optional. If not provided, include gages across all domains.
    gages = get_gages(
        auth_user(request),
        run_status=[StatusEnum.DONE, StatusEnum.FAILED, StatusEnum.CANCELLED, StatusEnum.SERVER_ERROR],
        require_both_validations_done=True,
        include_archived=include_archived,
        domain_name=domain_name
    )

    response = {"gages": gages}
    response_validator, error_response = validate_response(GetGagesResponseSerializer, response, fields_to_truncate=["gages"], max_length=10)
    if error_response:
        return error_response

    logger.debug(
        f"Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - "
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["gages"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=EmptySerializer,
    responses={
        200: GetCalibrationJobsSummaryResponseSerializer,
        400: OpenApiResponse(response=ErrorResponseSerializer, description="Validation error or parsing error"),
        500: OpenApiResponse(response=ErrorResponseSerializer, description="Internal server error"),
    },
    description="Get summary counts of Calibration jobs in Running / Ready / Saved status"
)
@api_view(["POST", "GET"])
@handle_exceptions
def get_calibration_jobs_summary(request: Request) -> Response:
    """
    Return counts of calibration jobs in:
      - Running
      - Ready
      - Saved

    Counts are based on the derived combined_status (same as get_jobs()).
    Archived runs are excluded (consistent with default behavior elsewhere).
    """
    data = request.data if request.method == "POST" else request.query_params.dict()
    logger.debug(f"{get_caller_name()}() request from {get_user_email(request)} - {data}")

    validator, error_return = validate_request(EmptySerializer, data)
    if error_return:
        return error_return

    running_lc = StatusEnum.RUNNING.value.lower()
    ready_lc = StatusEnum.READY.value.lower()
    saved_lc = StatusEnum.SAVED.value.lower()

    with readonly_transaction():
        query = Q(owner=auth_user(request)) & Q(is_archived=False)

        qs = annotate_calibration_combined_status(
            CalibrationRun.objects.filter(query),
            include_status_lower=True,
        )

        agg = qs.aggregate(
            running_count=Sum(
                Case(
                    When(_status_lower=running_lc, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                )
            ),
            ready_count=Sum(
                Case(
                    When(_status_lower=ready_lc, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                )
            ),
            saved_count=Sum(
                Case(
                    When(_status_lower=saved_lc, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                )
            ),
        )

    response = {
        "running_count": int(agg["running_count"] or 0),
        "ready_count": int(agg["ready_count"] or 0),
        "saved_count": int(agg["saved_count"] or 0),
    }

    response_validator, error_response = validate_response(GetCalibrationJobsSummaryResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f"Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - "
        f"{json.dumps(response_validator.data)}"
    )
    return Response(response_validator.data)


def annotate_calibration_combined_status(
        qs: QuerySet[CalibrationRun],
        *,
        include_status_lower: bool = False,
) -> QuerySet[CalibrationRun]:
    """
    Annotate a CalibrationRun queryset with the derived combined_status field.

    This centralizes the combined-status semantics so that:
      - job listing endpoints
      - summary/count endpoints
      - any future endpoints

    all compute combined_status identically and cannot drift.

    include_status_lower: if True, also annotates `_status_lower = Lower("combined_status")` which is useful
    for case-insensitive filtering/count aggregations without repeating the annotation.
    """

    DONE_ID = StatusEnum.DONE.db_instance.id
    RUNNING_ID = StatusEnum.RUNNING.db_instance.id
    SERVER_ERROR_ID = StatusEnum.SERVER_ERROR.db_instance.id
    FAILED_ID = StatusEnum.FAILED.db_instance.id
    CANCELLED_ID = StatusEnum.CANCELLED.db_instance.id
    SUBMITTED_ID = StatusEnum.SUBMITTED.db_instance.id

    # ─────────────────────────────────────────────────────────────
    # Always annotate validation_control_status_id + validation_best_status_id.
    # combined_status depends on these values, so they must be present
    # for BOTH ids_only and full-detail modes.
    #
    # Note: these are the ValidationRun.status_id values (ints), not names.
    # ─────────────────────────────────────────────────────────────
    qs = qs.annotate(
        validation_control_status_id=Subquery(
            ValidationRun.objects.filter(
                calibration_run_id=OuterRef("pk"),
                validation_type=ValidationType.VALID_CONTROL.value
            ).values("status_id")[:1]
        ),
        validation_best_status_id=Subquery(
            ValidationRun.objects.filter(
                calibration_run_id=OuterRef("pk"),
                validation_type=ValidationType.VALID_BEST.value
            ).values("status_id")[:1]
        ),
    )

    # ───── Combined status computation ─────
    # Combined status computation:
    # Django’s Case() evaluates WHEN clauses in order and stops at the first match.
    # This explicit ordering defines the severity precedence manually when
    # calibration status is DONE.
    #
    # Precedence when calibration is DONE (highest → lowest):
    #   Running → Server_Error → Failed → Cancelled → Submitted → Done
    #
    # If calibration is NOT DONE, combined_status is simply the calibration status
    # (e.g., Saved, Ready, Submitted, Running, etc.), and validation statuses are ignored.
    #
    # combined_status is ALWAYS computed (even when ids_only=True) so that:
    #   • status filters behave consistently in both modes
    #   • pagination and filtering always operate on the same rows
    #
    # Rules:
    #   • If calibration is not Done → combined = calibration status
    #   • If calibration is Done:
    #       – If any validation is Running → combined = Running
    #       – If any validation is Server_Error → combined = Server_Error
    #       – If any validation is Failed → combined = Failed
    #       – If any validation is Cancelled → combined = Cancelled
    #       – If any validation is Submitted → combined = Submitted
    #       – If all existing validations are Done (or missing) → combined = Done
    #   • Missing validations are ignored.
    # ------------------------------------------------------------------
    qs = qs.annotate(
        combined_status=Case(
            # Calibration not done → use calibration status directly
            When(~Q(status_id=DONE_ID), then=F("status__name")),

            # Calibration done but any validation running
            When(
                Q(status_id=DONE_ID)
                & (
                        Q(validation_control_status_id=RUNNING_ID)
                        | Q(validation_best_status_id=RUNNING_ID)
                ),
                then=Value(StatusEnum.RUNNING.value)
            ),

            # Calibration done but any validation server error
            When(
                Q(status_id=DONE_ID)
                & (
                        Q(validation_control_status_id=SERVER_ERROR_ID)
                        | Q(validation_best_status_id=SERVER_ERROR_ID)
                ),
                then=Value(StatusEnum.SERVER_ERROR.value)
            ),

            # Calibration done but any validation failed
            When(
                Q(status_id=DONE_ID)
                & (
                        Q(validation_control_status_id=FAILED_ID)
                        | Q(validation_best_status_id=FAILED_ID)
                ),
                then=Value(StatusEnum.FAILED.value)
            ),

            # Calibration done but any validation cancelled
            When(
                Q(status_id=DONE_ID)
                & (
                        Q(validation_control_status_id=CANCELLED_ID)
                        | Q(validation_best_status_id=CANCELLED_ID)
                ),
                then=Value(StatusEnum.CANCELLED.value)
            ),

            # Calibration done but any validation submitted
            When(
                Q(status_id=DONE_ID)
                & (
                        Q(validation_control_status_id=SUBMITTED_ID)
                        | Q(validation_best_status_id=SUBMITTED_ID)
                ),
                then=Value(StatusEnum.SUBMITTED.value)
            ),

            # Calibration done and any existing validations are DONE (missing validations allowed).
            # If VALID_CONTROL or VALID_BEST is missing, it does not block DONE here.
            When(
                Q(status_id=DONE_ID)
                & (
                        Q(validation_control_status_id__isnull=True)
                        | Q(validation_control_status_id=DONE_ID)
                )
                & (
                        Q(validation_best_status_id__isnull=True)
                        | Q(validation_best_status_id=DONE_ID)
                ),
                then=Value(StatusEnum.DONE.value)
            ),

            # Fallback (covers any future status additions)
            default=F("status__name"),
            output_field=CharField(),
        )
    )

    if include_status_lower:
        qs = qs.annotate(_status_lower=Lower("combined_status"))

    return qs


def get_forecast_base_gages(
        model: type[ForecastRun] | type[HindcastRun],
        user: CustomUser,
        *,
        run_status: list[StatusEnum] | None = None,
        include_archived: bool = False,
        domain_name: str | None = None,
) -> list[str]:
    """
    Get distinct non-null gage_ids for the authenticated user's Forecast/Hindcast runs.

    Runs in READ ONLY mode to reduce contention.

    :param model: ForecastRun or HindcastRun model class.
    :param user: Owner of the runs to inspect.
    :param run_status: Optional list of StatusEnum values to restrict the base job set
                       (matches endpoint-level restrictions).
    :param include_archived: If True, include runs whose parent CalibrationRun is archived;
                             otherwise exclude them. Default is False so endpoints exclude
                             archived calibration runs by default.
    :param domain_name: Optional domain name (validated by serializer as a DomainEnum value).
                        If None, include gages across all domains.
    :return: List of distinct gage_id strings.
    """
    with readonly_transaction():
        query = Q(calibration_run__owner=user)

        # Default behavior: exclude archived calibration runs unless include_archived is explicitly true.
        if not include_archived:
            query &= Q(calibration_run__is_archived=False)

        # Domain is optional. If not provided, include gages across all domains.
        if domain_name:
            query &= Q(calibration_run__gage__domain__name__iexact=domain_name)

        if run_status:
            query &= Q(status__in=[s.db_instance for s in run_status])

        return list(
            model.objects
            .filter(query)
            .filter(calibration_run__gage__isnull=False)
            .values_list("calibration_run__gage__gage_id", flat=True)
            .distinct()
        )


@extend_schema(
    request=GetGagesRequestSerializer,
    responses={
        200: GetGagesResponseSerializer,
        400: OpenApiResponse(response=ErrorResponseSerializer, description="Validation error or parsing error"),
        500: OpenApiResponse(response=ErrorResponseSerializer, description="Internal server error"),
    },
    description="Get distinct gage_ids for Forecast jobs (optional domain + include_archived)"
)
@api_view(["POST", "GET"])
@handle_exceptions
def get_forecast_gages(request: Request) -> Response:
    """
    Get distinct gage_ids for Forecast jobs (optional domain + include_archived).
    """
    data = request.data if request.method == "POST" else request.query_params.dict()
    logger.debug(f"{get_caller_name()}() request from {get_user_email(request)} - {data}")

    validator, error_return = validate_request(GetGagesRequestSerializer, data)
    if error_return:
        return error_return

    domain_name = validator.get("domain_name") or None
    include_archived = validator.get("include_archived")

    gages = get_forecast_base_gages(
        ForecastRun,
        auth_user(request),
        run_status=None,
        include_archived=include_archived,
        domain_name=domain_name,
    )

    response = {"gages": gages}
    response_validator, error_response = validate_response(
        GetGagesResponseSerializer, response, fields_to_truncate=["gages"], max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f"Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - "
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["gages"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=GetGagesRequestSerializer,
    responses={
        200: GetGagesResponseSerializer,
        400: OpenApiResponse(response=ErrorResponseSerializer, description="Validation error or parsing error"),
        500: OpenApiResponse(response=ErrorResponseSerializer, description="Internal server error"),
    },
    description="Get distinct gage_ids for Hindcast jobs (optional domain + include_archived)"
)
@api_view(["POST", "GET"])
@handle_exceptions
def get_hindcast_gages(request: Request) -> Response:
    """
    Get distinct gage_ids for Hindcast jobs (optional domain + include_archived).
    """
    data = request.data if request.method == "POST" else request.query_params.dict()
    logger.debug(f"{get_caller_name()}() request from {get_user_email(request)} - {data}")

    validator, error_return = validate_request(GetGagesRequestSerializer, data)
    if error_return:
        return error_return

    domain_name = validator.get("domain_name") or None
    include_archived = validator.get("include_archived")

    gages = get_forecast_base_gages(
        HindcastRun,
        auth_user(request),
        run_status=None,
        include_archived=include_archived,
        domain_name=domain_name,
    )

    response = {"gages": gages}
    response_validator, error_response = validate_response(
        GetGagesResponseSerializer, response, fields_to_truncate=["gages"], max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f"Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - "
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["gages"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=GetGagesRequestSerializer,
    responses={
        200: GetGagesResponseSerializer,
        400: OpenApiResponse(response=ErrorResponseSerializer, description="Validation error or parsing error"),
        500: OpenApiResponse(response=ErrorResponseSerializer, description="Internal server error"),
    },
    description="Get distinct gage_ids for DONE Forecast jobs eligible for Verification (optional domain + include_archived)"
)
@api_view(["POST", "GET"])
@handle_exceptions
def get_forecast_gages_for_verification(request: Request) -> Response:
    """
    Get distinct gage_ids for DONE Forecast jobs eligible for Verification (optional domain + include_archived).
    """
    data = request.data if request.method == "POST" else request.query_params.dict()
    logger.debug(f"{get_caller_name()}() request from {get_user_email(request)} - {data}")

    validator, error_return = validate_request(GetGagesRequestSerializer, data)
    if error_return:
        return error_return

    domain_name = validator.get("domain_name") or None
    include_archived = validator.get("include_archived")

    gages = get_forecast_base_gages(
        ForecastRun,
        auth_user(request),
        run_status=[StatusEnum.DONE],
        include_archived=include_archived,
        domain_name=domain_name,
    )

    response = {"gages": gages}
    response_validator, error_response = validate_response(
        GetGagesResponseSerializer, response, fields_to_truncate=["gages"], max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f"Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - "
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["gages"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=GetGagesRequestSerializer,
    responses={
        200: GetGagesResponseSerializer,
        400: OpenApiResponse(response=ErrorResponseSerializer, description="Validation error or parsing error"),
        500: OpenApiResponse(response=ErrorResponseSerializer, description="Internal server error"),
    },
    description="Get distinct gage_ids for DONE Hindcast jobs eligible for Verification (optional domain + include_archived)"
)
@api_view(["POST", "GET"])
@handle_exceptions
def get_hindcast_gages_for_verification(request: Request) -> Response:
    """
    Get distinct gage_ids for DONE Hindcast jobs eligible for Verification
    (optional domain + include_archived).
    """
    data = request.data if request.method == "POST" else request.query_params.dict()
    logger.debug(f"{get_caller_name()}() request from {get_user_email(request)} - {data}")

    validator, error_return = validate_request(GetGagesRequestSerializer, data)
    if error_return:
        return error_return

    domain_name = validator.get("domain_name") or None
    include_archived = validator.get("include_archived")

    gages = get_forecast_base_gages(
        HindcastRun,
        auth_user(request),
        run_status=[StatusEnum.DONE],
        include_archived=include_archived,
        domain_name=domain_name,
    )

    response = {"gages": gages}
    response_validator, error_response = validate_response(
        GetGagesResponseSerializer, response, fields_to_truncate=["gages"], max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f"Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - "
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["gages"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=GetVerificationGagesRequestSerializer,
    responses={
        200: GetGagesResponseSerializer,
        400: OpenApiResponse(response=ErrorResponseSerializer, description="Validation error or parsing error"),
        500: OpenApiResponse(response=ErrorResponseSerializer, description="Internal server error"),
    },
    description="Get distinct gage_ids for forecast-based or hindcast-based Verification jobs (optional domain + include_archived)"
)
@api_view(["POST", "GET"])
@handle_exceptions
def get_verification_gages(request: Request) -> Response:
    """
    Get distinct gage_ids for verification jobs for either forecast-based or
    hindcast-based verification runs.

    The request must include verification_job_type so the endpoint can use the
    correct parent ORM path.
    """
    data = request.data if request.method == "POST" else request.query_params.dict()
    logger.debug(f"{get_caller_name()}() request from {get_user_email(request)} - {data}")

    validator, error_return = validate_request(GetVerificationGagesRequestSerializer, data)
    if error_return:
        return error_return

    domain_name = validator.get("domain_name") or None
    include_archived = validator.get("include_archived")
    verification_job_type = validator.get("verification_job_type")
    paths = get_verification_parent_paths(verification_job_type)

    with readonly_transaction():
        query = Q(**{f'{paths["calibration_prefix"]}owner': auth_user(request)})

        # Default behavior: exclude archived calibration runs unless include_archived is explicitly true.
        if not include_archived:
            query &= Q(**{paths["archived_field"]: False})

        # Domain is optional. If not provided, include gages across all domains.
        if domain_name:
            query &= Q(**{f'{paths["domain_field"]}__iexact': domain_name})

        gages = list(
            VerificationRun.objects
            .filter(query)
            .filter(**{paths["gage_isnull_field"]: False})
            .values_list(paths["gage_value_field"], flat=True)
            .distinct()
        )

    response = {"gages": gages}
    response_validator, error_response = validate_response(
        GetGagesResponseSerializer, response, fields_to_truncate=["gages"], max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f"Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - "
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["gages"], max_length=10))}'
    )
    return Response(response_validator.data)


def get_gages(
        user: CustomUser,
        *,
        run_status: list[StatusEnum] | None = None,
        require_both_validations_done: bool = False,
        include_archived: bool = False,
        domain_name: str | None = None,
) -> list[str]:
    """
    Get distinct non-null gage_ids for the authenticated user's CalibrationRuns.

    Runs in READ ONLY mode to reduce contention.

    :param user: Owner of the CalibrationRuns to inspect.
    :param run_status: Optional list of StatusEnum values to restrict the base job set
                       (matches endpoint-level restrictions).
    :param require_both_validations_done: If True, only include CalibrationRuns where both
                                          VALID_CONTROL and VALID_BEST validation runs
                                          exist and are DONE.
    :param include_archived: If True, include archived CalibrationRuns; otherwise exclude them.
                             Default is False so endpoints exclude archived by default.
    :param domain_name: Optional domain name (validated by serializer as a DomainEnum value).
                   If None, include gages across all domains.
    :return: List of distinct gage_id strings.
    """
    with readonly_transaction():
        query = Q(owner=user)

        if run_status:
            query &= Q(status__in=[s.db_instance for s in run_status])

        qs = (
            CalibrationRun.objects
            .filter(query)
            .filter(gage__isnull=False)
        )

        # Default behavior: exclude archived unless include_archived is explicitly true.
        if not include_archived:
            qs = qs.filter(is_archived=False)

        # Domain is optional. If not provided, include gages across all domains.
        if domain_name:
            qs = qs.filter(gage__domain__name__iexact=domain_name)

        if require_both_validations_done:
            qs = qs.annotate(
                has_valid_control_done=Exists(
                    ValidationRun.objects.filter(
                        calibration_run_id=OuterRef("id"),
                        validation_type=ValidationType.VALID_CONTROL.value,
                        status=StatusEnum.DONE.db_instance,
                    )
                ),
                has_valid_best_done=Exists(
                    ValidationRun.objects.filter(
                        calibration_run_id=OuterRef("id"),
                        validation_type=ValidationType.VALID_BEST.value,
                        status=StatusEnum.DONE.db_instance,
                    )
                ),
            ).filter(
                has_valid_control_done=True,
                has_valid_best_done=True,
            )

        return list(
            qs.values_list("gage__gage_id", flat=True).distinct()
        )


def get_jobs(
        user: CustomUser,
        run_status: list[StatusEnum] | None = None,
        include_validation_data: GetValidationJobsScope | None = None,
        require_both_validations_done: bool = False,
        include_modules: bool = False,
        limit: int | None = None,
        offset: int = 0,
        filters: dict[str, Any] | None = None,
        sort: dict[str, str] | None = None,
        ids_only: bool = False
) -> tuple[list[dict[str, Any]] | list[int], int, list[Any], list[Any]]:
    """
    Retrieves calibration jobs for the given user with optional status filtering,
    validation data inclusion, server-side filters, sorting, and optional pagination.

    Runs in READ ONLY mode to reduce contention.

    :param user: The user for whom the jobs are being fetched.
    :param run_status: Optional list of StatusEnum values to restrict the base job set (e.g., DONE, FAILED).
                       This restriction is applied before computing date/id ranges and before user filters.
    :param include_validation_data: Determines the level of validation data to include:
        - STATUS: Includes validation status details for associated validation runs.
    :param include_modules: Whether to include module list in the response.
    :param limit: Optional maximum number of rows to return (for pagination). If None, return all.
    :param offset: Optional number of rows to skip before returning results (for pagination).
    :param filters: Optional dict of filter criteria (e.g. gage_id, status, modules, date_filter, id_filter, include_archived).
                    Note: for Calibration jobs, the client "status" filter applies to combined_status (not raw calibration status).
    :param sort: Optional dict { "field": "created_at", "direction": "asc" or "desc" }.
    :param ids_only: Only return the ids of the calibration jobs.
    :param require_both_validations_done: If True, only include calibration jobs where both VALID_CONTROL and VALID_BEST
                                          validation runs exist and are DONE.
    :return: Tuple (results, total_count, date_range, id_range).
        - total_count reflects total rows BEFORE pagination.
        - date_range reflects the possible range of created_at dates for this job set BEFORE user filtering (after run_status restriction).
        - id_range reflects the possible range of job IDs for this job set BEFORE user filtering (after run_status restriction).
    """
    filters_dict: dict[str, Any] = filters or {}
    order_by = resolve_sort(sort, CalibrationSortField)

    # These are Status model instances (not IDs)
    status_instances = [s.db_instance for s in run_status] if run_status else None

    DONE_ID = StatusEnum.DONE.db_instance.id

    with readonly_transaction():
        # Base query: filter jobs for the user
        query = Q(owner=user)

        # Endpoint-level status restriction (uses Status model instances)
        if status_instances:
            query &= Q(status__in=status_instances)

        # Range metadata before user filters (but after run_status restriction)
        date_range, id_range = compute_range(CalibrationRun, query)

        # ───── Apply user-defined filters (except status) ─────
        # Adds API-provided filters (gage, modules, dates, IDs, etc.)
        # to the base query. The 'status' filter is applied later
        # after annotation of the derived combined_status field.
        # ------------------------------------------------------------------
        query = apply_calibration_filters(query, filters_dict)

        # ───── Build base queryset ─────
        # Build base queryset; validation-status annotations will be applied next.
        base_qs = CalibrationRun.objects.filter(query)

        # Centralized combined_status + validation status annotations.
        # Also annotate _status_lower only if we will use it (status filter present).
        base_qs = annotate_calibration_combined_status(
            base_qs,
            include_status_lower=bool(filters_dict.get("status")),
        )

        if not ids_only:
            # ───── Annotate validation fields used for sorting and UI display ─────
            # The following annotations enrich each CalibrationRun with additional fields:
            #   • validation_run_count — total number of non-control validation runs
            #
            # (validation_control_status_id / validation_best_status_id are always
            # annotated above, and are used by combined_status.)
            # ------------------------------------------------------------------
            base_qs = base_qs.annotate(
                # Number of validation runs (excluding VALID_CONTROL)
                validation_run_count=Count(
                    "validations",
                    filter=~Q(validations__validation_type=ValidationType.VALID_CONTROL.value),
                    distinct=True,
                )
            )

        # ─────────────────────────────────────────────────────────────
        # Apply DONE-validation enforcement (VALID_CONTROL and VALID_BEST)
        # *after* all annotations are present, but *before* applying the
        # user-supplied "status" filter.
        #
        # This ensures:
        #   • combined_status has been computed
        #   • validation annotations exist
        #   • any user "status" filter is run against the final combined_status
        # ─────────────────────────────────────────────────────────────
        if require_both_validations_done:
            base_qs = base_qs.annotate(
                has_valid_control_done=Exists(
                    ValidationRun.objects.filter(
                        calibration_run_id=OuterRef("id"),
                        validation_type=ValidationType.VALID_CONTROL.value,
                        status_id=DONE_ID,
                    )
                ),
                has_valid_best_done=Exists(
                    ValidationRun.objects.filter(
                        calibration_run_id=OuterRef("id"),
                        validation_type=ValidationType.VALID_BEST.value,
                        status_id=DONE_ID,
                    )
                )
            ).filter(
                has_valid_control_done=True,
                has_valid_best_done=True
            )

        # ─────────────────────────────────────────────────────────────
        # Apply user-supplied status filter LAST.
        # Must come AFTER combined_status, because filtering is done on the
        # derived combined_status value, not the raw calibration status.
        # This ensures ids_only and full-detail return the same job set.
        # ─────────────────────────────────────────────────────────────
        if "status" in filters_dict and filters_dict["status"]:
            # Normalize to lowercase for case-insensitive matching
            normalized_statuses = [s.strip().lower() for s in filters_dict["status"]]

            # _status_lower already exists (include_status_lower=True above) when a status filter is present.
            base_qs = base_qs.filter(_status_lower__in=normalized_statuses)

        # ───── Finalize ordering and compute total count ─────
        #   • DO NOT apply ordering before computing count.
        #   • Count should reflect the total number of filtered rows, regardless of sort.
        #   • Never apply offset/limit before counting.
        total_count = base_qs.count()

        # ───── Fast path: ids_only ─────
        # For ID-only mode we apply ordering and pagination directly on the IDs queryset.
        if ids_only:
            # Apply sorting and pagination if specified
            ids_qs = base_qs.order_by(*order_by).values_list("id", flat=True)

            if limit:
                ids_qs = ids_qs[offset: offset + limit]
            return list(ids_qs), total_count, date_range, id_range

        # ───── Apply ordering BEFORE slicing ─────
        # Django applies LIMIT/OFFSET in SQL only when slicing occurs.
        # We must order first to ensure deterministic, correct pagination.
        ordered_qs = base_qs.order_by(*order_by)

        # ───── Apply pagination ONLY if limit provided ─────
        if limit:
            ordered_qs = ordered_qs[offset: offset + limit]

        # ───── Extract values AFTER slicing ─────
        calibration_runs_qs = ordered_qs.values(
            "id", "gage__gage_id", "gage__domain__name", "submit_date", "updated_at",
            "job_name", "calibration_start_period", "calibration_end_period",
            "status_id", "status__name", "combined_status", "job_genesis", "created_at",
            "objective_function__name", "optimization__name",
            "is_archived", "is_locked"
        )

        calibration_runs = list(calibration_runs_qs)
        run_ids = [r["id"] for r in calibration_runs]

        # ───── Preload modules if requested ─────
        modules_map: dict[int, list[str]] = {}

        if include_modules and run_ids:
            modules_qs = (
                CalibrationFormulation.objects
                .filter(calibration_run_id__in=run_ids)
                .select_related("module")
                .order_by("-id")
                .values_list("calibration_run_id", "module__name")
            )

            for run_id, module_name in modules_qs:
                modules_map.setdefault(run_id, []).append(module_name)

        # ───── Precompute which runs include an LSTM module ─────
        lstm_run_ids = set(
            CalibrationFormulation.objects
            .filter(
                calibration_run_id__in=run_ids,
                module__name__icontains="LSTM"
            )
            .values_list("calibration_run_id", flat=True)
            .distinct()
        )

        # Preload validation runs if requested (STATUS only)
        validations_map: dict[int, list] = {}
        if include_validation_data == GetValidationJobsScope.STATUS:
            validations_qs = (
                ValidationRun.objects
                .filter(calibration_run_id__in=run_ids)
                .select_related("status")
                .order_by('-id')
                .values("id", "calibration_run_id", "validation_type", "status__name")
            )

            for v in validations_qs:
                validations_map.setdefault(v["calibration_run_id"], []).append(v)

        # Preload stop criteria
        stop_qs = (
            CalibrationStopCriteria.objects
            .filter(calibration_run_id__in=run_ids)
            .values("calibration_run_id", "value")
        )
        stop_criteria_map: dict[int, str | None] = {sc["calibration_run_id"]: sc["value"] for sc in stop_qs}

        # downloadable_statuses is list[StatusEnum]
        downloadable_ids = {s.db_instance.id for s in downloadable_statuses}

        results: list[dict[str, Any]] = []
        for run in calibration_runs:
            run_id = run["id"]

            result: dict[str, Any] = {
                'calibration_run_id': run_id,
                'gage_id': run['gage__gage_id'],
                'domain_name': run['gage__domain__name'],
                'status': run['combined_status'],
                'objective_function': run.get('objective_function__name'),  # may be None
                'optimization_algorithm': run.get('optimization__name'),  # may be None
                'is_archived': run['is_archived'],
                'is_locked': run['is_locked'],
                'is_lstm': run_id in lstm_run_ids,
                'submit_date': run['submit_date'],
                'job_name': run['job_name'],
                'calibration_start_period': run['calibration_start_period'],
                'calibration_end_period': run['calibration_end_period'],
                'job_genesis': run['job_genesis'],
                'created_at': run['created_at'],
                'last_updated_on': run['updated_at'],

                # Downloadable is based on the *raw* calibration status (status_id), not combined_status.
                # This matches downloadable_statuses, which is defined over StatusEnum (calibration statuses).
                'is_downloadable': run["status_id"] in downloadable_ids,
                'stop_criteria': stop_criteria_map.get(run_id),
            }

            if include_modules:
                result['modules'] = modules_map.get(run_id, [])

            # Include detailed validation status if requested
            if include_validation_data == GetValidationJobsScope.STATUS:
                result['validations'] = [
                    {
                        "validation_run_id": v["id"],
                        "validation_type": v["validation_type"],
                        "status": v["status__name"],
                    }
                    for v in validations_map.get(run_id, [])
                ]

            results.append(result)

        return results, total_count, date_range, id_range


def get_validation_jobs_internal(
        calibration_run_id: int,
        detail_level: Literal[GetValidationJobsScope.STATUS, GetValidationJobsScope.DETAILS] = GetValidationJobsScope.STATUS,
) -> list[dict[str, Any]]:
    """
    Retrieve validation jobs for a specific calibration job.

    Only returns data when detail_level == DETAILS. For STATUS mode, returns an empty list.

    :param calibration_run_id: ID of the calibration run to fetch validation jobs for.
    :param detail_level: Must be either STATUS (summary mode) or DETAILS (full job data).
    :return: An empty list unless detail_level == DETAILS, in which case a list of detailed dicts.
    """
    # Only return detailed data for DETAILS mode
    if detail_level != GetValidationJobsScope.DETAILS:
        return []

    with readonly_transaction():
        # 1) Fetch all validation runs for this calibration run
        validation_runs = list(
            ValidationRun.objects
            .filter(calibration_run_id=calibration_run_id)
            .exclude(validation_type=ValidationType.VALID_CONTROL.value)
            .select_related("status", "iteration")
        )

        if not validation_runs:
            return []

        # Collect iteration IDs
        iteration_ids = [v.iteration_id for v in validation_runs if v.iteration_id]

        # 2) Preload iteration parameters in one query
        iteration_params_qs = IterationParameter.objects.filter(
            iteration_id__in=iteration_ids
        ).values(
            "iteration_id", "calibration_parameter__name", "tuned_value"
        )

        params_map: dict[int, list[dict[str, Any]]] = {}
        for p in iteration_params_qs:
            params_map.setdefault(p["iteration_id"], []).append({
                "name": p["calibration_parameter__name"],
                "value": p["tuned_value"]
            })

        # 3) Preload all "best params" for the calibration run in one query
        best_params_qs = IterationParameter.objects.filter(
            iteration__calibration_run_id=calibration_run_id,
            iteration__best_params=True
        ).values("calibration_parameter__name", "tuned_value")

        best_params = [{"name": bp["calibration_parameter__name"], "value": bp["tuned_value"]} for bp in best_params_qs]

        # Build result
        results: list[dict[str, Any]] = []
        for job in validation_runs:
            parameters = best_params if job.validation_type == ValidationType.VALID_BEST.value else params_map.get(job.iteration_id, [])

            results.append({
                "validation_run_id": job.id,
                "submit_date": job.submit_date,
                "status": job.status.name,
                "validation_type": job.validation_type,
                "iteration_num": job.iteration_num if job.iteration else None,
                "parameters": parameters,
                "best": job.validation_type == ValidationType.VALID_BEST.value,
            })

        return results


@extend_schema(
    request=CalibrationRunIdSerializer,
    responses={
        200: GetValidationJobsResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Retrieve validation jobs along with their starting parameter values"
)
@api_view(['GET', 'POST'])
@handle_exceptions
def get_validation_jobs(request: Request) -> Response:
    """
    Retrieves validation jobs for a specific calibration run along with initial parameter values.
    This endpoint itself doesn’t need a read-only wrapper, because
    `get_validation_jobs_internal` already enforces READ ONLY.

    - Handles user authentication and request validation.
    - Fetches validation jobs linked to a calibration run.
    - Constructs and validates the response with serialized data.

    :param request: The HTTP request object containing calibration run data.
    :return: JSON response containing validation jobs or error details.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(CalibrationRunIdSerializer, data)
    if error_return:
        return error_return

    calibration_run_id = validator.get('calibration_run_id')
    _, error_return = get_calibration_run(calibration_run_id, request.user, run_status=[StatusEnum.DONE])
    if error_return:
        return error_return

    # Retrieve validation jobs using internal helper - already runs in READ ONLY mode
    validation_jobs = get_validation_jobs_internal(calibration_run_id, detail_level=GetValidationJobsScope.DETAILS)

    response = {'validation_jobs': validation_jobs}
    response_validator, error_response = validate_response(GetValidationJobsResponseSerializer, response)
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - {json.dumps(response_validator.data)}'
    )
    return Response(response_validator.data)


def _get_forecast_or_hindcast_base_jobs_internal(
        model: type[ForecastRun] | type[HindcastRun],
        user: CustomUser,
        *,
        run_status: list[StatusEnum] | None = None,
        limit: int | None = None,
        offset: int = 0,
        filters: dict[str, Any] | None = None,
        sort: dict[str, str] | None = None,
        response_id_key: str,
        response_status_key: str,
        sort_enum: Type[ForecastSortField | HindcastSortField],
        include_hindcast_fields: bool = False,
) -> tuple[list[dict[str, Any]], int, list[Any], list[Any]]:
    """
    Shared internal helper to retrieve ForecastRun or HindcastRun rows for a user
    with optional filtering, sorting, and pagination.

    Runs in READ ONLY mode to reduce contention.

    :param model: ForecastRun or HindcastRun model class.
    :param user: Owner of the jobs to fetch.
    :param run_status: Optional list of StatusEnum values to restrict the base job set.
                       This restriction is applied before computing date/id ranges and
                       before user filters.
    :param limit: Optional maximum number of rows to return. If None, return all rows.
    :param offset: Optional number of rows to skip before returning results.
    :param filters: Optional dict of filter criteria (e.g. gage_id, status, modules).
    :param sort: Optional dict { "field": one of ForecastSortField or HindcastSortField values,
                             "direction": "asc" or "desc" }.
    :param response_id_key: Output key to use for the run id
                            (e.g. "forecast_run_id" or "hindcast_run_id").
    :param response_status_key: Output key to use for the status field
                                (e.g. "forecast_status" or "hindcast_status").
    :param include_hindcast_fields: Whether to include hindcast-only fields such as
                                    interval_cycle and num_iterations, and enforce
                                    that all cold start fields are present.
    :return: Tuple (results, total_count, date_range, id_range).
    """
    filters_dict: dict[str, Any] = filters or {}
    order_by = resolve_sort(sort, sort_enum)

    with readonly_transaction():
        # Base query (ownership constraint)
        query = Q(calibration_run__owner=user)

        # Apply status restriction early so ranges reflect run_status restriction
        if run_status:
            query &= Q(status__in=[s.db_instance for s in run_status])

        # Compute ranges BEFORE user filters (but after run_status restriction)
        date_range, id_range = compute_range(model, query)

        # Now apply user filters
        query = apply_forecast_filters(query, filters_dict)

        base_qs = model.objects.filter(query)

        # total_count must be BEFORE pagination
        total_count = base_qs.count()

        # ──────────────────────────────────────────
        # Apply ordering + pagination at the DB level
        # ──────────────────────────────────────────
        paged_qs = base_qs.order_by(*order_by)
        if limit:
            paged_qs = paged_qs[offset: offset + limit]

        value_fields = [
            'id',
            'calibration_run_id',
            'configuration__name',
            'calibration_run__gage__domain__name',
            'cycle_date',
            'submit_date',
            'calibration_run__gage__gage_id',
            'status__name',
            'cold_start_run_id',
            'cold_start_run__cold_start_date',
            'cold_start_run__cycle_date',
            'cold_start_run__status__name',
            'cold_start_run__submit_date',
        ]

        if include_hindcast_fields:
            value_fields.extend([
                'interval_cycle',
                'num_iterations',
            ])

        rows = list(paged_qs.values(*value_fields))

    # Normalize keys expected by the API response/serializer
    for row in rows:
        row[response_id_key] = row.pop('id')
        row['configuration'] = row.pop('configuration__name')
        row['domain_name'] = row.pop('calibration_run__gage__domain__name')
        row['gage_id'] = row.pop('calibration_run__gage__gage_id')
        row[response_status_key] = row.pop('status__name')

        cold_start_run_id = row.pop('cold_start_run_id')
        cold_date = row.pop('cold_start_run__cold_start_date')
        cold_cycle_date = row.pop('cold_start_run__cycle_date')
        cold_status = row.pop('cold_start_run__status__name')
        cold_submit = row.pop('cold_start_run__submit_date')

        # Hindcast always requires a cold start, so all cold start fields must be present.
        if include_hindcast_fields and cold_start_run_id is None:
            raise ValueError(
                f"{response_id_key}={row[response_id_key]} is missing required cold_start data."
            )

        if cold_start_run_id is not None:
            row['cold_start'] = {
                'cold_start_run_id': cold_start_run_id,
                'cold_start_date': cold_date,
                'cold_start_cycle_date': cold_cycle_date,
                'cold_start_status': cold_status,
                'cold_start_submit_date': cold_submit,
            }

    return rows, total_count, date_range, id_range


def get_forecast_jobs_internal(
        user: CustomUser,
        run_status: list[StatusEnum] | None = None,
        limit: int | None = None,
        offset: int = 0,
        filters: dict[str, Any] | None = None,
        sort: dict[str, str] | None = None
) -> tuple[list[dict[str, Any]], int, list[Any], list[Any]]:
    """
    Internal helper to retrieve forecast jobs for a user (READ ONLY), with optional filtering,
    sorting, and pagination.

    :param user: Owner of the jobs to fetch.
    :param run_status: Optional list of StatusEnum values to filter on (e.g., DONE).
    :param limit: Optional maximum number of rows to return (for pagination). If None, return all.
    :param offset: Optional number of rows to skip before returning results (for pagination).
    :param filters: Optional dict of filter criteria (e.g. gage_id, status, modules).
    :param sort: Optional dict { "field": one of ForecastSortField values, "direction": "asc" or "desc" }.
    :return: Tuple (results, total_count, date_range, id_range).
    """
    return _get_forecast_or_hindcast_base_jobs_internal(
        ForecastRun,
        user,
        run_status=run_status,
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort,
        response_id_key="forecast_run_id",
        response_status_key="forecast_status",
        sort_enum=ForecastSortField,
        include_hindcast_fields=False,
    )


def get_hindcast_jobs_internal(
        user: CustomUser,
        run_status: list[StatusEnum] | None = None,
        limit: int | None = None,
        offset: int = 0,
        filters: dict[str, Any] | None = None,
        sort: dict[str, str] | None = None
) -> tuple[list[dict[str, Any]], int, list[Any], list[Any]]:
    """
    Internal helper to retrieve hindcast jobs for a user (READ ONLY), with optional filtering,
    sorting, and pagination.

    :param user: Owner of the jobs to fetch.
    :param run_status: Optional list of StatusEnum values to filter on (e.g., DONE).
    :param limit: Optional maximum number of rows to return (for pagination). If None, return all.
    :param offset: Optional number of rows to skip before returning results (for pagination).
    :param filters: Optional dict of filter criteria (e.g. gage_id, status, modules).
    :param sort: Optional dict { "field": one of HindcastSortField values, "direction": "asc" or "desc" }.
    :return: Tuple (results, total_count, date_range, id_range).
    """
    return _get_forecast_or_hindcast_base_jobs_internal(
        HindcastRun,
        user,
        run_status=run_status,
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort,
        response_id_key="hindcast_run_id",
        response_status_key="hindcast_status",
        sort_enum=HindcastSortField,
        include_hindcast_fields=True,
    )


@extend_schema(
    request=ForecastPaginationSerializer,
    responses={
        200: GetForecastJobsResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get forecast jobs"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_forecast_jobs(request: Request) -> Response:
    """
    Retrieve all forecast jobs for the authenticated user.

    Runs in READ ONLY mode to reduce contention.

    :param request: The HTTP request object containing forecast pagination data.
    :return: JSON response with forecast jobs or error information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(ForecastPaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")
    filters, sort = _normalize_filters_and_sort(filters, sort)

    forecast_jobs, total_count, date_range, id_range = get_forecast_jobs_internal(
        auth_user(request),
        run_status=None,
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort
    )

    response = {
        "forecast_jobs": forecast_jobs,
        "total_count": total_count,
    }
    if total_count > 0:
        response['date_range'] = date_range
        response['id_range'] = id_range

    response_validator, error_response = validate_response(
        GetForecastJobsResponseSerializer, response,
        fields_to_truncate=['forecast_jobs'], max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["forecast_jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=HindcastPaginationSerializer,
    responses={
        200: GetHindcastJobsResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get hindcast jobs"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_hindcast_jobs(request: Request) -> Response:
    """
    Retrieve all hindcast jobs for the authenticated user.

    Runs in READ ONLY mode to reduce contention.

    :param request: The HTTP request object containing hindcast pagination data.
    :return: JSON response with hindcast jobs or error information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(HindcastPaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")
    filters, sort = _normalize_filters_and_sort(filters, sort)

    hindcast_jobs, total_count, date_range, id_range = get_hindcast_jobs_internal(
        auth_user(request),
        run_status=None,
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort
    )

    response = {
        "hindcast_jobs": hindcast_jobs,
        "total_count": total_count,
    }
    if total_count > 0:
        response['date_range'] = date_range
        response['id_range'] = id_range

    response_validator, error_response = validate_response(
        GetHindcastJobsResponseSerializer, response,
        fields_to_truncate=['hindcast_jobs'], max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["hindcast_jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=ForecastPaginationSerializer,
    responses={
        200: GetForecastJobsResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get DONE forecast jobs"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_forecast_jobs_for_verification(request: Request) -> Response:
    """
    Retrieve only DONE forecast jobs for the authenticated user.

    Runs in READ ONLY mode to reduce contention.

    :param request: The HTTP request object containing forecast pagination data.
    :return: JSON response with forecast jobs or error information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(ForecastPaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")
    filters, sort = _normalize_filters_and_sort(filters, sort)

    forecast_jobs, total_count, date_range, id_range = get_forecast_jobs_internal(
        auth_user(request),
        run_status=[StatusEnum.DONE],
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort
    )

    response = {
        "forecast_jobs": forecast_jobs,
        "total_count": total_count
    }
    if total_count > 0:
        response['date_range'] = date_range
        response['id_range'] = id_range

    response_validator, error_response = validate_response(
        GetForecastJobsResponseSerializer, response,
        fields_to_truncate=['forecast_jobs'],
        max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["forecast_jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


@extend_schema(
    request=HindcastPaginationSerializer,
    responses={
        200: GetHindcastJobsResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get DONE hindcast jobs"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_hindcast_jobs_for_verification(request: Request) -> Response:
    """
    Retrieve only DONE hindcast jobs for the authenticated user.

    Runs in READ ONLY mode to reduce contention.

    :param request: The HTTP request object containing hindcast pagination data.
    :return: JSON response with hindcast jobs or error information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(HindcastPaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")
    filters, sort = _normalize_filters_and_sort(filters, sort)

    hindcast_jobs, total_count, date_range, id_range = get_hindcast_jobs_internal(
        auth_user(request),
        run_status=[StatusEnum.DONE],
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort
    )

    response = {
        "hindcast_jobs": hindcast_jobs,
        "total_count": total_count,
    }
    if total_count > 0:
        response["date_range"] = date_range
        response["id_range"] = id_range

    response_validator, error_response = validate_response(
        GetHindcastJobsResponseSerializer,
        response,
        fields_to_truncate=["hindcast_jobs"],
        max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["hindcast_jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


def get_verification_jobs_internal(
        user: CustomUser,
        verification_job_type: Literal['forecast', 'hindcast'],
        run_status: list[StatusEnum] | None = None,
        limit: int | None = None,
        offset: int = 0,
        filters: dict[str, Any] | None = None,
        sort: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], int, list[Any], list[Any]]:
    """
    Internal helper to retrieve verification jobs for either forecast-based or
    hindcast-based verification runs, with optional filtering, sorting, and pagination.

    Runs in READ ONLY mode to reduce contention.

    Notes:
      - module_filter.operator="and" is only supported for Calibration jobs.
        For Forecast, Hindcast, and Verification jobs, module_filter.operator="and"
        is treated as "or" by the shared filter logic.

    :param user: Owner of the jobs to fetch.
    :param verification_job_type: Parent job type for the verification jobs being
        queried. Must be either 'forecast' or 'hindcast'.
    :param run_status: Optional list of StatusEnum values to restrict the base job set (e.g., DONE).
                       This restriction is applied before computing date/id ranges and before user filters.
    :param limit: Optional maximum number of rows to return (for pagination). If None, return all.
    :param offset: Optional number of rows to skip before returning results (for pagination).
    :param filters: Optional dict of filter criteria using the shared retrieval
        filter schema (e.g. gage_id, domain_name, status, modules, date_filter,
        id_filter, include_archived).
    :param sort: Optional dict { "field": one of VerificationSortField values, "direction": "asc" or "desc" }.
    :return: Tuple (results, total_count, date_range, id_range).
        - total_count reflects the total number of matching rows BEFORE pagination is applied.
        - date_range reflects the possible range of created_at dates for this job set BEFORE user filtering
          (after run_status restriction).
        - id_range reflects the possible range of job IDs for this job set BEFORE user filtering
          (after run_status restriction).
    """
    filters_dict: dict[str, Any] = filters or {}
    order_by = resolve_sort(sort, VerificationSortField)
    paths = get_verification_parent_paths(verification_job_type)

    with readonly_transaction():
        query = Q(**{f'{paths["calibration_prefix"]}owner': user})

        # Apply status restriction early so ranges reflect run_status restriction
        if run_status:
            query &= Q(status__in=[s.db_instance for s in run_status])

        # Compute ranges BEFORE user filters (but after run_status restriction)
        date_range, id_range = compute_range(VerificationRun, query)

        # Now apply user filters
        query = apply_verification_filters(query, filters_dict, verification_job_type=verification_job_type)

        base_qs = VerificationRun.objects.filter(query)

        total_count = base_qs.count()

        # ──────────────────────────────────────────
        # Apply ordering + pagination at the DB level
        # ──────────────────────────────────────────
        paged_qs = base_qs.order_by(*order_by)
        if limit:
            paged_qs = paged_qs[offset: offset + limit]

        rows = list(
            paged_qs.values(
                "id",
                paths["parent_run_id_field"],
                "status__name",
                "submit_date"
            )
        )

    # Normalize keys expected by the API response/serializer
    for row in rows:
        row["verification_run_id"] = row.pop("id")
        row["status"] = row.pop("status__name")

    return rows, total_count, date_range, id_range


@extend_schema(
    request=VerificationPaginationSerializer,
    responses={
        200: GetVerificationJobsResponseSerializer,
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Validation error or parsing error"
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Internal server error"
        )
    },
    description="Get verification jobs"
)
@api_view(['POST', 'GET'])
@handle_exceptions
def get_verification_jobs(request: Request) -> Response:
    """
    Retrieve verification jobs for the authenticated user for either forecast-based
    or hindcast-based verification runs.

    The request must include verification_job_type so the endpoint can query the
    correct parent run relationship and return the matching parent run id field.

    Runs in READ ONLY mode to reduce contention.

    :param request: The HTTP request object containing verification pagination data.
    :return: JSON response with verification jobs or error information.
    """
    data = request.data if request.method == 'POST' else request.query_params.dict()
    logger.debug(f'{get_caller_name()}() request from {get_user_email(request)} - {data}')

    validator, error_return = validate_request(VerificationPaginationSerializer, data)
    if error_return:
        return error_return

    limit = validator.get("limit")
    offset = validator.get("offset", 0)
    filters = validator.get("filters") or {}
    sort = validator.get("sort")
    verification_job_type = validator.get("verification_job_type")
    filters, sort = _normalize_filters_and_sort(filters, sort)

    verification_jobs, total_count, date_range, id_range = get_verification_jobs_internal(
        auth_user(request),
        verification_job_type=verification_job_type,
        run_status=None,
        limit=limit,
        offset=offset,
        filters=filters,
        sort=sort
    )

    response = {
        'verification_jobs': verification_jobs,
        "total_count": total_count
    }
    if total_count > 0:
        response['date_range'] = date_range
        response['id_range'] = id_range

    response_validator, error_response = validate_response(
        GetVerificationJobsResponseSerializer, response,
        fields_to_truncate=['verification_jobs'], max_length=10
    )
    if error_response:
        return error_response

    logger.debug(
        f'Returning to {get_user_email(request)} from {get_caller_name()}(){get_elapsed_str(request)} - '
        f'{json.dumps(truncate_large_fields(response_validator.data, fields_to_truncate=["verification_jobs"], max_length=10))}'
    )
    return Response(response_validator.data)


def get_verification_parent_paths(
        verification_job_type: Literal['forecast', 'hindcast'],
) -> dict[str, str]:
    """
    Return ORM path fragments for verification queries based on whether the
    verification job belongs to a forecast run or a hindcast run.

    The returned values are used to build:
      - ownership constraints
      - gage/domain filters
      - archived filters
      - module filters
      - gage extraction for verification gage endpoints

    :param verification_job_type: Parent job type for the verification jobs being
        queried. Must be either 'forecast' or 'hindcast'.
    :return: Dictionary containing the ORM prefixes/field paths needed for
        verification queries.
    """
    if verification_job_type == 'forecast':
        return {
            "parent_prefix": "forecast_run__",
            "calibration_prefix": "forecast_run__calibration_run__",
            "gage_prefix": "forecast_run__calibration_run__gage__",
            "module_prefix": "forecast_run__calibration_run__calibrationformulation__",
            "archived_field": "forecast_run__calibration_run__is_archived",
            "domain_field": "forecast_run__calibration_run__gage__domain__name",
            "gage_value_field": "forecast_run__calibration_run__gage__gage_id",
            "gage_isnull_field": "forecast_run__calibration_run__gage__isnull",
            "parent_run_id_field": "forecast_run_id",
        }

    return {
        "parent_prefix": "hindcast_run__",
        "calibration_prefix": "hindcast_run__calibration_run__",
        "gage_prefix": "hindcast_run__calibration_run__gage__",
        "module_prefix": "hindcast_run__calibration_run__calibrationformulation__",
        "archived_field": "hindcast_run__calibration_run__is_archived",
        "domain_field": "hindcast_run__calibration_run__gage__domain__name",
        "gage_value_field": "hindcast_run__calibration_run__gage__gage_id",
        "gage_isnull_field": "hindcast_run__calibration_run__gage__isnull",
        "parent_run_id_field": "hindcast_run_id",
    }


def compute_range(model: type[BaseRun], query: Q) -> tuple[
    list[Any],  # created_at range
    list[Any],  # id range
]:
    """
    Compute min/max created_at and id for any job model.
    Returns (date_range, id_range) as two lists.
    """
    agg = model.objects.filter(query).aggregate(
        min_created_at=Min('created_at'),
        max_created_at=Max('created_at'),
        min_job_id=Min('id'),
        max_job_id=Max('id'),
    )
    return (
        [agg['min_created_at'], agg['max_created_at']],
        [agg['min_job_id'], agg['max_job_id']],
    )


def auth_user(request: Request) -> CustomUser:
    """
    Return the authenticated user as the concrete CustomUser type.

    At runtime, all API views in this module are protected by DRF authentication
    (e.g. IsAuthenticated / JWT), so request.user is guaranteed to be a CustomUser.
    However, DRF types request.user as AbstractBaseUser | AnonymousUser for static
    analysis, which causes false-positive type warnings.

    This helper centralizes the explicit cast at the API boundary so that:
      - View code stays clean and readable
      - Internal helpers can assume a concrete CustomUser
      - We do not rely on fragile IDE type inference heuristics
    """
    return cast(CustomUser, request.user)
