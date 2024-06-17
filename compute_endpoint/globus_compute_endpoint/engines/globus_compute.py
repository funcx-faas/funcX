from __future__ import annotations

import logging
import os
import queue
import shlex
import typing as t
import uuid
from concurrent.futures import Future

from globus_compute_common.messagepack.message_types import (
    EPStatusReport,
    TaskTransition,
)
from globus_compute_endpoint.engines.base import (
    GlobusComputeEngineBase,
    ReportingThread,
)
from parsl.executors.high_throughput.executor import HighThroughputExecutor
from parsl.jobs.job_status_poller import JobStatusPoller

logger = logging.getLogger(__name__)
DOCKER_CMD_TEMPLATE = "docker run {options} -v {rundir}:{rundir} -t {image} {command}"
APPTAINER_CMD_TEMPLATE = "apptainer run {options} {image} {command}"
SINGULARITY_CMD_TEMPLATE = "singularity run {options} {image} {command}"
VALID_CONTAINER_TYPES = ("docker", "singularity", "apptainer", "custom", None)


class JobStatusPollerKwargs(t.TypedDict, total=False):
    strategy: str | None
    max_idletime: float
    strategy_period: float | int


class GlobusComputeEngine(GlobusComputeEngineBase):
    """GlobusComputeEngine is a wrapper over Parsl's HighThroughputExecutor"""

    def __init__(
        self,
        *args,
        label: str = "GlobusComputeEngine",
        max_retries_on_system_failure: int = 0,
        executor: t.Optional[HighThroughputExecutor] = None,
        container_type: t.Literal[VALID_CONTAINER_TYPES] = None,  # type: ignore
        container_uri: t.Optional[str] = None,
        container_cmd_options: t.Optional[str] = None,
        encrypted: bool = True,
        strategy: str | None = None,
        job_status_kwargs: t.Optional[JobStatusPollerKwargs] = None,
        working_dir: str | os.PathLike | None = None,
        run_in_sandbox: bool = False,
        **kwargs,
    ):
        """``GlobusComputeEngine`` is a shim over `Parsl's HighThroughputExecutor
        <parslhtex_>`_, almost all of arguments are passed along, unfettered.
        Consequently, please reference `Parsl's HighThroughputExecutor <parslhtex_>`_
        documentation for a complete list of arguments; we list below only the
        arguments specific to the ``GlobusComputeEngine``.

        .. _parslhtex: https://parsl.readthedocs.io/en/stable/stubs/parsl.executors.HighThroughputExecutor.html
        .. _parslstrategy: https://parsl.readthedocs.io/en/stable/stubs/parsl.jobs.strategy.Strategy.html
        .. _parsljobstatuspoller: https://parsl.readthedocs.io/en/stable/stubs/parsl.jobs.job_status_poller.JobStatusPoller.html

        Parameters
        ----------

        label: str
           Label used to name engine log directories and batch jobs
           default: "GlobusComputeEngine"

        max_retries_on_system_failure: int
           Set the number of retries for functions that fail due to
           system failures such as node failure/loss. Since functions
           can fail after partial runs, consider additional cleanup
           logic before enabling this functionality
           default: 0

        strategy: str | None
            Specify which scaling strategy to use; this is eventually given to
            `Parsl's Strategy <parslstrategy_>`_.  [Deprecated; use
            ``job_status_kwargs``]

        job_status_kwargs: dict | None
            Keyword arguments to be passed through to `Parsl's JobStatusPoller
            <parsljobstatuspoller_>`_ class that drives strategy to do auto-scaling.

        encrypted: bool
            Flag to enable/disable encryption (CurveZMQ). Default is True.

        working_dir: str | os.PathLike | None
            Directory within which functions should execute. When set to None,
            defaults to the endpoint_dir (~/.globus_compute/<endpoint_name>/)

        run_in_sandbox: bool
            Functions will run in a sandbox directory under the working_dir
            if this option is enabled. Default: False

        """  # noqa: E501
        self.run_dir = os.getcwd()
        self.label = label
        self._status_report_thread = ReportingThread(target=self.report_status, args=[])
        super().__init__(
            *args, max_retries_on_system_failure=max_retries_on_system_failure, **kwargs
        )
        self.strategy = strategy

        self.container_type = container_type
        assert (
            self.container_type in VALID_CONTAINER_TYPES
        ), f"{self.container_type} is not a valid container_type"
        self.container_uri = container_uri
        self.container_cmd_options = container_cmd_options

        if executor is None:
            executor = HighThroughputExecutor(  # type: ignore
                *args,
                label=label,
                encrypted=encrypted,
                **kwargs,
            )
        self.executor = executor
        self.working_dir = working_dir
        self.run_in_sandbox = run_in_sandbox
        if strategy is None:
            strategy = "simple"

        # Set defaults for JobStatusPoller
        self._job_status_kwargs: JobStatusPollerKwargs = {
            "max_idletime": 120.0,
            "strategy": strategy,
            "strategy_period": 5.0,
        }
        self._job_status_kwargs.update(job_status_kwargs or {})

    @property
    def max_workers_per_node(self):
        # Needed for strategies (e.g., SimpleStrategy)
        return self.executor.max_workers_per_node

    @property
    def encrypted(self):
        return self.executor.encrypted

    def containerized_launch_cmd(self) -> str:
        """Recompose executor's launch_cmd to launch with containers

        Returns
        -------
        str launch_cmd
        """
        launch_cmd = self.executor.launch_cmd
        # Adding assert here since mypy can't figure out launch_cmd's type
        assert launch_cmd
        if self.container_type == "docker":
            launch_cmd = DOCKER_CMD_TEMPLATE.format(
                image=self.container_uri,
                rundir=self.run_dir,
                command=launch_cmd,
                options=self.container_cmd_options or "",
            )
        elif self.container_type == "apptainer":
            launch_cmd = APPTAINER_CMD_TEMPLATE.format(
                image=self.container_uri,
                command=launch_cmd,
                options=self.container_cmd_options or "",
            )
        elif self.container_type == "singularity":
            launch_cmd = SINGULARITY_CMD_TEMPLATE.format(
                image=self.container_uri,
                command=launch_cmd,
                options=self.container_cmd_options or "",
            )
        elif self.container_type == "custom":
            assert (
                self.container_cmd_options
            ), "GCE.container_cmd_options is required for GCE.container_type=custom"
            template = self.container_cmd_options.replace(
                "{EXECUTOR_RUNDIR}", str(self.run_dir)
            )
            launch_cmd = template.replace("{EXECUTOR_LAUNCH_CMD}", launch_cmd)

        # Remove extra whitespace between tokens
        launch_cmd = " ".join(shlex.split(launch_cmd))
        return launch_cmd

    def start(
        self,
        *args,
        endpoint_id: t.Optional[uuid.UUID] = None,
        run_dir: t.Optional[str] = None,
        results_passthrough: t.Optional[queue.Queue] = None,
        **kwargs,
    ):
        assert endpoint_id, "GCExecutor requires kwarg:endpoint_id at start"
        assert run_dir, "GCExecutor requires kwarg:run_dir at start"

        if self.working_dir:
            if not os.path.isabs(self.working_dir):
                self.working_dir = os.path.join(run_dir, self.working_dir)
        else:
            self.working_dir = run_dir

        self.endpoint_id = endpoint_id
        self.run_dir = run_dir
        self.executor.run_dir = self.run_dir
        script_dir = os.path.join(self.run_dir, "submit_scripts")
        self.executor.provider.script_dir = script_dir
        if self.container_type:
            self.executor.launch_cmd = self.containerized_launch_cmd()
            logger.info(
                f"Containerized launch cmd template: {self.executor.launch_cmd}"
            )

        if (
            hasattr(self.executor.provider, "channel")
            and not self.executor.provider.channel.script_dir
        ):
            self.executor.provider.channel.script_dir = script_dir

        os.makedirs(self.executor.provider.script_dir, exist_ok=True)
        if results_passthrough:
            # Only update the default queue in GCExecutorBase if
            # a queue is passed in
            self.results_passthrough = results_passthrough
        self.executor.start()
        self._status_report_thread.start()
        # Add executor to poller *after* executor has started
        self.job_status_poller = JobStatusPoller(dfk=None, **self._job_status_kwargs)
        self.job_status_poller.add_executors([self.executor])

    def _submit(
        self,
        func: t.Callable,
        *args: t.Any,
        **kwargs: t.Any,
    ) -> Future:
        return self.executor.submit(func, {}, *args, **kwargs)

    @property
    def provider(self):
        return self.executor.provider

    def get_connected_managers(self) -> t.List[t.Dict[str, t.Any]]:
        """
        Returns
        -------
        List of dicts containing info for all connected managers
        """
        return self.executor.connected_managers()

    def get_total_managers(self, managers: t.List[t.Dict[str, t.Any]]) -> int:
        """
        Parameters
        ----------
        managers: list[dict[str, Any]]
            List of dicts containing info for all connected managers

        Returns
        -------
        Total number of managers
        """
        return len(managers)

    def get_total_active_managers(self, managers: t.List[t.Dict[str, t.Any]]) -> int:
        """
        Parameters
        ----------
        managers: list[dict[str, Any]]
            List of dicts containing info for all connected managers

        Returns
        -------
        Number of managers that have capacity for new tasks
        """
        return sum(1 for m in managers if m["active"])

    def get_outstanding_breakdown(
        self, managers: t.Optional[t.List[t.Dict[str, t.Any]]] = None
    ) -> t.List[t.Tuple[str, int, bool]]:
        """
        Parameters
        ----------
        managers: list[dict[str, Any]] | None
            List of dicts containing info for all connected managers

        Returns
        -------
        List of tuples of the form (component, # of tasks on component, active?)
        """
        if managers is None:
            managers = self.get_connected_managers()
        total_task_count = self.executor.outstanding
        breakdown = [(m["manager"], m["tasks"], m["active"]) for m in managers]
        total_count_managers = sum([m["tasks"] for m in managers])
        task_count_interchange = total_task_count - total_count_managers
        breakdown = [("interchange", task_count_interchange, True)] + breakdown
        return breakdown

    def get_total_tasks_outstanding(self) -> dict:
        """
        Returns
        -------
        Dict of type {str_task_type: count_tasks}
        """
        return {"RAW": self.executor.outstanding}

    def get_total_tasks_pending(self, managers: t.List[t.Dict[str, t.Any]]) -> int:
        """
        Parameters
        ----------
        managers: list[dict[str, Any]]
            List of dicts containing info for all connected managers

        Returns
        -------
        Total number of pending tasks
        """
        outstanding = self.get_outstanding_breakdown(managers=managers)
        return outstanding[0][1]  # Queued in interchange

    def provider_status(self):
        status = []
        if self.provider:
            # ex.locks is a dict of block_id:job_id mappings
            job_ids = self.executor.blocks.values()
            status = self.provider.status(job_ids=job_ids)
        return status

    def get_total_live_workers(
        self, managers: t.Optional[t.List[t.Dict[str, t.Any]]] = None
    ) -> int:
        """
        Parameters
        ----------
        managers: list[dict[str, Any]]
            List of dicts containing info for all connected managers

        Returns
        -------
        Total number of live workers
        """
        if managers is None:
            managers = self.get_connected_managers()
        return sum([mgr["worker_count"] for mgr in managers])

    def get_total_idle_workers(self, managers: t.List[t.Dict[str, t.Any]]) -> int:
        """
        Parameters
        ----------
        managers: list[dict[str, Any]]
            List of dicts containing info for all connected managers

        Returns
        -------
        Total number of workers that are not actively running tasks
        """
        idle_workers = 0
        for mgr in managers:
            workers = mgr["worker_count"]
            tasks = mgr["tasks"]
            idle_workers += max(0, workers - tasks)
        return idle_workers

    def scale_out(self, blocks: int):
        logger.info(f"Scaling out {blocks} blocks")
        return self.executor.scale_out(blocks=blocks)

    def scale_in(self, blocks: int):
        logger.info(f"Scaling in {blocks} blocks")
        return self.executor.scale_in(blocks=blocks)

    def _handle_task_exception(
        self,
        task_id: str,
        execution_begin: TaskTransition,
        exception: BaseException,
    ) -> bytes:
        result_bytes = b""
        retry_info = self._retry_table[task_id]
        if retry_info["retry_count"] < self.max_retries_on_system_failure:
            retry_info["retry_count"] += 1
            retry_info["exception_history"].append(exception)
            self.submit(task_id, retry_info["packed_task"])
        else:
            # This is a terminal state
            result_bytes = super()._handle_task_exception(
                task_id=task_id, execution_begin=execution_begin, exception=exception
            )

        return result_bytes

    @property
    def scaling_enabled(self) -> bool:
        """Indicates whether scaling is possible"""
        max_blocks = self.executor.provider.max_blocks
        return max_blocks > 0

    def get_status_report(self) -> EPStatusReport:
        """
        Returns
        -------
        Object containing info on the current status of the endpoint
        """
        managers = self.get_connected_managers()
        executor_status: t.Dict[str, t.Any] = {
            "task_id": -2,  # Deprecated
            "info": {
                "total_cores": 0,  # TODO
                "total_mem": 0,  # TODO
                "new_core_hrs": 0,  # TODO
                "total_core_hrs": 0,  # TODO
                "managers": self.get_total_managers(managers=managers),
                "active_managers": self.get_total_active_managers(managers=managers),
                "total_workers": self.get_total_live_workers(managers=managers),
                "idle_workers": self.get_total_idle_workers(managers=managers),
                "pending_tasks": self.get_total_tasks_pending(managers=managers),
                "outstanding_tasks": self.get_total_tasks_outstanding()["RAW"],
                "worker_mode": 0,  # Deprecated
                "scheduler_mode": 0,  # Deprecated
                "scaling_enabled": self.scaling_enabled,
                "mem_per_worker": self.executor.mem_per_worker,
                "cores_per_worker": self.executor.cores_per_worker,
                "prefetch_capacity": self.executor.prefetch_capacity,
                "max_blocks": self.executor.provider.max_blocks,
                "min_blocks": self.executor.provider.min_blocks,
                "max_workers_per_node": self.executor.max_workers_per_node,
                "nodes_per_block": self.executor.provider.nodes_per_block,
                "heartbeat_period": self.executor.heartbeat_period,
            },
        }
        task_status_deltas: t.Dict[str, t.List[TaskTransition]] = {}  # TODO
        return EPStatusReport(
            endpoint_id=self.endpoint_id,
            global_state=executor_status,
            task_statuses=task_status_deltas,
        )

    @property
    def executor_exception(self) -> t.Optional[Exception]:
        return self.executor.executor_exception

    def shutdown(self, /, **kwargs) -> None:
        self._status_report_thread.stop()
        self.job_status_poller.close()
        self.executor.shutdown()
