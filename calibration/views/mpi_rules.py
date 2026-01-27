import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Global table of MPI rules.  Can be updated dynamically with endpoint update_mpi_rules or using env variable MPI_NODE_RULES
# Each pair represents [max_catchments, num_nodes]
DEFAULT_MPI_NODE_RULES: list[list[int]] = [
    [10, 1],
    [50, 2],
    [250, 8],
    [-1, 16],
]


def _validate_and_normalize_mpi_rules(rules: Any) -> list[list[int]]:
    if not isinstance(rules, list) or not rules:
        raise ValueError("MPI_NODE_RULES must be a non-empty JSON list of [max_catchments, nodes] pairs")

    normalized: list[list[int]] = []
    saw_minus_one = False

    for i, item in enumerate(rules):
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], int)
            or not isinstance(item[1], int)
        ):
            raise ValueError(f"MPI_NODE_RULES item {i} must be [int max_catchments, int nodes]; got: {item!r}")

        max_catchments, nodes = item
        if nodes <= 0:
            raise ValueError(f"MPI_NODE_RULES item {i} has invalid nodes={nodes}; must be > 0")

        if max_catchments == -1:
            saw_minus_one = True

        normalized.append([max_catchments, nodes])

    # If you use -1 as the catch-all sentinel, enforce it as the last rule.
    if saw_minus_one:
        minus_one_rules = [r for r in normalized if r[0] == -1]
        other_rules = [r for r in normalized if r[0] != -1]
        if len(minus_one_rules) != 1:
            raise ValueError("MPI_NODE_RULES must contain at most one [-1, nodes] catch-all rule")
        normalized = other_rules + minus_one_rules

    return normalized


def load_mpi_node_rules_from_env() -> list[list[int]]:
    """
    Load MPI node allocation rules from the `MPI_NODE_RULES` environment variable.

    The expected format is a JSON-encoded list of `[max_catchments, nodes]` pairs, for example:
        [[10, 1], [50, 2], [250, 8], [-1, 16]]

    Semantics:
    - `max_catchments` is an integer upper bound (inclusive).
    - A value of `-1` is treated as a catch-all rule and should appear at most once.
    - `nodes` must be a positive integer and represents the number of MPI processes/nodes to use.

    Behavior:
    - If `MPI_NODE_RULES` is not set, returns `DEFAULT_MPI_NODE_RULES`.
    - If the variable is set but malformed, raises `ValueError` with a clear message.
    - If a `-1` (catch-all) rule is present, it is enforced as the final rule.

    :return: A validated and normalized list of `[max_catchments, nodes]` rules.
    :raises ValueError: If the environment variable is present but invalid.
    """
    raw = os.getenv("MPI_NODE_RULES")
    if not raw:
        return DEFAULT_MPI_NODE_RULES

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid MPI_NODE_RULES JSON: {e}") from e

    return _validate_and_normalize_mpi_rules(parsed)


# Evaluate rules once per worker process at import time
MPI_NODE_RULES = load_mpi_node_rules_from_env()


def log_mpi_rules() -> None:
    # Call this from AppConfig.ready()
    logger.info(f"MPI_NODE_RULES: {MPI_NODE_RULES}")


def get_mpi_nodes(num_catchments: int) -> int:
    mpi_nodes: int | None = None

    for max_catchments, mpi_nodes in MPI_NODE_RULES:
        if max_catchments == -1 or num_catchments <= max_catchments:
            break

    if mpi_nodes is None:
        raise RuntimeError("MPI_NODE_RULES has no matching rule (missing catch-all?)")

    logger.info(f"{num_catchments} catchments using {mpi_nodes} nodes")
    return mpi_nodes
