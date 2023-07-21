from __future__ import annotations

import typing as t
from collections import defaultdict

from globus_compute_sdk.sdk.utils.uuid_like import UUID_LIKE_T
from globus_compute_sdk.serialize import ComputeSerializer


class Batch:
    """Utility class for creating batch submission in Globus Compute"""

    def __init__(
        self,
        endpoint_id: UUID_LIKE_T,
        task_group_id: UUID_LIKE_T | None,
        request_queue=False,
    ):
        """
        :param task_group_id: UUID of task group to which to submit the batch
        :param endpoint_id: UUID of endpoint where tasks should be executed
        :param request_queue: Whether to request a result queue from the web service;
            typically only used by the Executor
        """
        self.task_group_id = task_group_id
        self.endpoint_id = endpoint_id
        self.tasks: dict[str, list[str]] = defaultdict(list)
        self._serde = ComputeSerializer()
        self.request_queue = request_queue

    def __repr__(self):
        return str(self.prepare())

    def __bool__(self):
        """Return true if all functions in batch have at least one task"""
        return all(bool(fns) for fns in self.tasks.values())

    def __len__(self):
        """Return the total number of tasks in batch (includes all functions)"""
        return sum(len(fns) for fns in self.tasks.values())

    def add(
        self,
        function_id: str,
        args: tuple[t.Any, ...] | None = None,
        kwargs: dict[str, t.Any] | None = None,
    ) -> None:
        """
        Add a function invocation to a batch submission

        :param function_id : UUID of registered function as registered.  (Required)
        :param args: arguments as required by the function signature
        :param kwargs: Keyword arguments as required by the function signature
        """
        if args is None:
            args = ()
        if kwargs is None:
            kwargs = {}
        ser_args = self._serde.serialize(args)
        ser_kwargs = self._serde.serialize(kwargs)
        payload = self._serde.pack_buffers([ser_args, ser_kwargs])

        self.tasks[function_id].append(payload)

    def prepare(self) -> dict[str, str | list[tuple[str, str, str]]]:
        """
        Prepare the payload to be POSTed to web service in a batch

        :returns: a dictionary suitable for JSONification for POSTing to the web service
        """
        data = {
            "endpoint_id": str(self.endpoint_id),
            "create_queue": self.request_queue,
            "tasks": dict(self.tasks),
        }
        if self.task_group_id:
            data["task_group_id"] = str(self.task_group_id)

        return data
