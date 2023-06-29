import logging
import queue
import threading
import time
import typing as t
import uuid
from abc import ABC, abstractmethod
from concurrent.futures import Future

from globus_compute_common import messagepack
from globus_compute_common.messagepack.message_types import (
    EPStatusReport,
    Result,
    TaskTransition,
)
from globus_compute_common.tasks import ActorName, TaskState
from globus_compute_endpoint.engines.helper import execute_task
from globus_compute_endpoint.exception_handling import (
    get_error_string,
    get_result_error_details,
)

logger = logging.getLogger(__name__)


class ReportingThread:
    def __init__(
        self, target: t.Callable, args: t.List, reporting_period: float = 30.0
    ):
        """This class wraps threading.Thread to run a callable in a loop
        periodically until the user calls `stop`. A status attribute can
        report exceptions to the parent thread upon failure.
        Parameters
        ----------
        target: Target function to be invoked to get report and post to queue
        args: args to be passed to target fn
        kwargs: kwargs to be passed to target fn
        reporting_period
        """
        self.status: Future = Future()
        self._shutdown_event = threading.Event()
        self.reporting_period = reporting_period
        self._thread = threading.Thread(
            target=self.run_in_loop, args=[target] + args, name="GCReportingThread"
        )

    def start(self):
        logger.info("Start called")
        self._thread.start()

    def run_in_loop(self, target: t.Callable, *args) -> None:
        while True:
            try:
                target(*args)
            except Exception as e:
                # log and update future before exiting, if it is not already set
                self.status.set_exception(exception=e)
                self._shutdown_event.set()
            if self._shutdown_event.wait(timeout=self.reporting_period):
                break

        logger.warning("ReportingThread exiting")

    def stop(self) -> None:
        self._shutdown_event.set()
        self._thread.join(timeout=0.1)


class GlobusComputeEngineBase(ABC):
    """Shared functionality and interfaces required by all GlobusCompute Engines.
    This is designed to plug-in executors following the concurrent.futures.Executor
    interface as execution backends to GlobusCompute
    """

    def __init__(
        self,
        *args: object,
        heartbeat_period_s: float = 30.0,
        endpoint_id: t.Optional[uuid.UUID] = None,
        **kwargs: object,
    ):
        self._shutdown_event = threading.Event()
        self._heartbeat_period_s = heartbeat_period_s
        self.endpoint_id = endpoint_id

        # remove these unused vars that we are adding to just keep
        # endpoint interchange happy
        self.container_type: t.Optional[str] = None
        self.funcx_service_address: t.Optional[str] = None
        self.run_dir: t.Optional[str] = None
        # This attribute could be set by the subclasses in their
        # start method if another component insists on owning the queue.
        self.results_passthrough: queue.Queue = queue.Queue()

    @abstractmethod
    def start(
        self,
        *args,
        **kwargs,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_status_report(self) -> EPStatusReport:
        raise NotImplementedError

    def report_status(self):
        status_report = self.get_status_report()
        packed_status = messagepack.pack(status_report)
        self.results_passthrough.put(packed_status)

    def _status_report(
        self, shutdown_event: threading.Event, heartbeat_period_s: float
    ):
        while not shutdown_event.wait(timeout=heartbeat_period_s):
            status_report = self.get_status_report()
            packed = messagepack.pack(status_report)
            self.results_passthrough.put(packed)

    def _setup_future_done_callback(self, task_id: uuid.UUID, future: Future) -> None:
        """
        Set up the done() callback for the provided future.

        The done callback handles
        Callback to post result to the passthrough queue
        Parameters
        ----------
        future: Future for which the callback is triggerd
        """

        exec_beg = TaskTransition(  # Reminder: used by *closure*, below
            timestamp=time.time_ns(),
            actor=ActorName.INTERCHANGE,
            state=TaskState.WAITING_FOR_LAUNCH,
        )

        def _done_cb(f: Future):
            if f.exception():
                exc = f.exception()
                code, user_message = get_result_error_details(exc)
                error_details = {"code": code, "user_message": user_message}
                exec_end = TaskTransition(
                    timestamp=time.time_ns(),
                    actor=ActorName.INTERCHANGE,
                    state=TaskState.EXEC_END,
                )
                result_message = dict(
                    task_id=task_id,
                    data=get_error_string(exc=exc),
                    exception=get_error_string(exc=exc),
                    error_details=error_details,
                    task_statuses=[exec_beg, exec_end],  # only transition info we have
                )
                packed_result = messagepack.pack(Result(**result_message))
            else:
                packed_result = f.result()

            self.results_passthrough.put(packed_result)

        future.add_done_callback(_done_cb)

    @abstractmethod
    def _submit(
        self,
        func: t.Callable,
        *args: t.Any,
        **kwargs: t.Any,
    ) -> Future:
        """Subclass should use the internal execution system to implement this"""
        raise NotImplementedError()

    def submit(self, task_id: uuid.UUID, packed_task: bytes) -> Future:
        """GC Endpoints should submit tasks via this method so that tasks are
        tracked properly.
        Parameters
        ----------
        packed_task: messagepack bytes buffer
        Returns
        -------
        future
        """

        future = self._submit(execute_task, task_id, packed_task)
        self._setup_future_done_callback(task_id, future)
        return future
