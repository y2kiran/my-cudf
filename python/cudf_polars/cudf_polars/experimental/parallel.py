# SPDX-FileCopyrightText: Copyright (c) 2024-2025, NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0
"""Multi-partition Dask execution."""

from __future__ import annotations

import itertools
import operator
from functools import reduce
from typing import TYPE_CHECKING, Any, ClassVar

import cudf_polars.experimental.groupby
import cudf_polars.experimental.io
import cudf_polars.experimental.join
import cudf_polars.experimental.select
import cudf_polars.experimental.shuffle  # noqa: F401
from cudf_polars.dsl.ir import IR, Cache, Filter, HStack, Projection, Select, Union
from cudf_polars.dsl.traversal import CachingVisitor, traversal
from cudf_polars.experimental.base import PartitionInfo, get_key_name
from cudf_polars.experimental.dispatch import (
    generate_ir_tasks,
    lower_ir_node,
)
from cudf_polars.experimental.utils import _concat, _lower_ir_fallback
from cudf_polars.utils.config import ConfigOptions

if TYPE_CHECKING:
    from collections.abc import MutableMapping
    from typing import Any

    from distributed import Client

    from cudf_polars.containers import DataFrame
    from cudf_polars.experimental.dispatch import LowerIRTransformer


class SerializerManager:
    """Manager to ensure ensure serializer is only registered once."""

    _serializer_registered: bool = False
    _client_run_executed: ClassVar[set[str]] = set()

    @classmethod
    def register_serialize(cls) -> None:
        """Register Dask/cudf-polars serializers in calling process."""
        if not cls._serializer_registered:
            from cudf_polars.experimental.dask_serialize import register

            register()
            cls._serializer_registered = True

    @classmethod
    def run_on_cluster(cls, client: Client) -> None:
        """Run serializer registration on the workers and scheduler."""
        if (
            client.id not in cls._client_run_executed
        ):  # pragma: no cover; Only executes with Distributed scheduler
            client.run(cls.register_serialize)
            client.run_on_scheduler(cls.register_serialize)
            cls._client_run_executed.add(client.id)


@lower_ir_node.register(IR)
def _(ir: IR, rec: LowerIRTransformer) -> tuple[IR, MutableMapping[IR, PartitionInfo]]:
    # Default logic - Requires single partition

    if len(ir.children) == 0:
        # Default leaf node has single partition
        return ir, {
            ir: PartitionInfo(count=1)
        }  # pragma: no cover; Missed by pylibcudf executor

    return _lower_ir_fallback(
        ir, rec, msg=f"Class {type(ir)} does not support multiple partitions."
    )


def lower_ir_graph(
    ir: IR, config_options: ConfigOptions | None = None
) -> tuple[IR, MutableMapping[IR, PartitionInfo]]:
    """
    Rewrite an IR graph and extract partitioning information.

    Parameters
    ----------
    ir
        Root of the graph to rewrite.
    config_options
        GPUEngine configuration options.

    Returns
    -------
    new_ir, partition_info
        The rewritten graph, and a mapping from unique nodes
        in the new graph to associated partitioning information.

    Notes
    -----
    This function traverses the unique nodes of the graph with
    root `ir`, and applies :func:`lower_ir_node` to each node.

    See Also
    --------
    lower_ir_node
    """
    config_options = config_options or ConfigOptions({})
    mapper = CachingVisitor(lower_ir_node, state={"config_options": config_options})
    return mapper(ir)


def task_graph(
    ir: IR, partition_info: MutableMapping[IR, PartitionInfo]
) -> tuple[MutableMapping[Any, Any], str | tuple[str, int]]:
    """
    Construct a task graph for evaluation of an IR graph.

    Parameters
    ----------
    ir
        Root of the graph to rewrite.
    partition_info
        A mapping from all unique IR nodes to the
        associated partitioning information.

    Returns
    -------
    graph
        A Dask-compatible task graph for the entire
        IR graph with root `ir`.

    Notes
    -----
    This function traverses the unique nodes of the
    graph with root `ir`, and extracts the tasks for
    each node with :func:`generate_ir_tasks`.

    See Also
    --------
    generate_ir_tasks
    """
    graph = reduce(
        operator.or_,
        (generate_ir_tasks(node, partition_info) for node in traversal([ir])),
    )

    key_name = get_key_name(ir)
    partition_count = partition_info[ir].count
    if partition_count > 1:
        graph[key_name] = (_concat, *partition_info[ir].keys(ir))
        return graph, key_name
    else:
        return graph, (key_name, 0)


# The true type signature for get_client() needs an overload. Not worth it.


def get_client() -> Any:
    """Get appropriate Dask client or scheduler."""
    SerializerManager.register_serialize()

    try:  # pragma: no cover; block depends on executor type and Distributed cluster
        from distributed import get_client

        client = get_client()
        SerializerManager.run_on_cluster(client)
    except (
        ImportError,
        ValueError,
    ):  # pragma: no cover; block depends on Dask local scheduler
        from dask import get

        return get
    else:  # pragma: no cover; block depends on executor type and Distributed cluster
        return client.get


def evaluate_dask(ir: IR, config_options: ConfigOptions | None = None) -> DataFrame:
    """Evaluate an IR graph with partitioning."""
    ir, partition_info = lower_ir_graph(ir, config_options)

    get = get_client()

    graph, key = task_graph(ir, partition_info)
    return get(graph, key)


@generate_ir_tasks.register(IR)
def _(
    ir: IR, partition_info: MutableMapping[IR, PartitionInfo]
) -> MutableMapping[Any, Any]:
    # Single-partition default behavior.
    # This is used by `generate_ir_tasks` for all unregistered IR sub-types.
    if partition_info[ir].count > 1:
        raise NotImplementedError(
            f"Failed to generate multiple output tasks for {ir}."
        )  # pragma: no cover

    child_names = []
    for child in ir.children:
        child_names.append(get_key_name(child))
        if partition_info[child].count > 1:
            raise NotImplementedError(
                f"Failed to generate tasks for {ir} with child {child}."
            )  # pragma: no cover

    key_name = get_key_name(ir)
    return {
        (key_name, 0): (
            ir.do_evaluate,
            *ir._non_child_args,
            *((child_name, 0) for child_name in child_names),
        )
    }


@lower_ir_node.register(Union)
def _(
    ir: Union, rec: LowerIRTransformer
) -> tuple[IR, MutableMapping[IR, PartitionInfo]]:
    # Check zlice
    if ir.zlice is not None:  # pragma: no cover
        return _lower_ir_fallback(
            ir, rec, msg="zlice is not supported for multiple partitions."
        )

    # Lower children
    children, _partition_info = zip(*(rec(c) for c in ir.children), strict=True)
    partition_info = reduce(operator.or_, _partition_info)

    # Partition count is the sum of all child partitions
    count = sum(partition_info[c].count for c in children)

    # Return reconstructed node and partition-info dict
    new_node = ir.reconstruct(children)
    partition_info[new_node] = PartitionInfo(count=count)
    return new_node, partition_info


@generate_ir_tasks.register(Union)
def _(
    ir: Union, partition_info: MutableMapping[IR, PartitionInfo]
) -> MutableMapping[Any, Any]:
    key_name = get_key_name(ir)
    partition = itertools.count()
    return {
        (key_name, next(partition)): child_key
        for child in ir.children
        for child_key in partition_info[child].keys(child)
    }


def _lower_ir_pwise(
    ir: IR, rec: LowerIRTransformer
) -> tuple[IR, MutableMapping[IR, PartitionInfo]]:
    # Lower a partition-wise (i.e. embarrassingly-parallel) IR node

    # Lower children
    children, _partition_info = zip(*(rec(c) for c in ir.children), strict=True)
    partition_info = reduce(operator.or_, _partition_info)
    counts = {partition_info[c].count for c in children}

    # Check that child partitioning is supported
    if len(counts) > 1:  # pragma: no cover
        return _lower_ir_fallback(
            ir,
            rec,
            msg=f"Class {type(ir)} does not support children with mismatched partition counts.",
        )

    # Return reconstructed node and partition-info dict
    partition = PartitionInfo(count=max(counts))
    new_node = ir.reconstruct(children)
    partition_info[new_node] = partition
    return new_node, partition_info


lower_ir_node.register(Projection, _lower_ir_pwise)
lower_ir_node.register(Cache, _lower_ir_pwise)
lower_ir_node.register(Filter, _lower_ir_pwise)
lower_ir_node.register(HStack, _lower_ir_pwise)


def _generate_ir_tasks_pwise(
    ir: IR, partition_info: MutableMapping[IR, PartitionInfo]
) -> MutableMapping[Any, Any]:
    # Generate partition-wise (i.e. embarrassingly-parallel) tasks
    child_names = [get_key_name(c) for c in ir.children]
    return {
        key: (
            ir.do_evaluate,
            *ir._non_child_args,
            *[(child_name, i) for child_name in child_names],
        )
        for i, key in enumerate(partition_info[ir].keys(ir))
    }


generate_ir_tasks.register(Projection, _generate_ir_tasks_pwise)
generate_ir_tasks.register(Cache, _generate_ir_tasks_pwise)
generate_ir_tasks.register(Filter, _generate_ir_tasks_pwise)
generate_ir_tasks.register(HStack, _generate_ir_tasks_pwise)
generate_ir_tasks.register(Select, _generate_ir_tasks_pwise)
