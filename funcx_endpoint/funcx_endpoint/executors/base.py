import logging
import multiprocessing
import threading
import time
import typing as t
from abc import ABC, abstractmethod
from concurrent.futures import Future

from funcx_common import messagepack
from funcx_common.messagepack.message_types import (
    EPStatusReport,
    Result,
    Task,
    TaskTransition,
)
from funcx_common.tasks import ActorName, TaskState

from funcx_endpoint.exception_handling import get_error_string, get_result_error_details
from funcx_endpoint.executors.execution_helper.helper import execute_task

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
        logger.warning("Start called")
        self._thread.start()

    def run_in_loop(self, target: t.Callable, *args) -> None:
        while True:
            try:
                target(*args)
            except Exception as e:
                # log and update future before exiting
                logger.exception("Callback failed")
                self.status.set_exception(exception=e)

            if self._shutdown_event.wait(timeout=self.reporting_period):
                break

        logger.warning("ReportingThread exiting")

    def stop(self) -> None:
        self._shutdown_event.set()
        self._thread.join(timeout=0.1)


class GCExecutorBase(ABC):
    """This executor base class holds shared behavior required by
    all Globus Compute executors.
    """

    def __init__(
        self,
        heartbeat_period: float = 30.0,
        endpoint_id: str = None,
        *args: object,
        **kwargs: object,
    ) -> object:
        self._shutdown_event = threading.Event()
        self._heartbeat_period = heartbeat_period
        self.endpoint_id = endpoint_id

        # remove these unused vars that we are adding to just keep
        # endpoint interchange happy
        self.container_type: t.Optional[str] = None
        self.funcx_service_address: t.Optional[str] = None
        self.run_dir: t.Optional[str] = None
        # This attribute could be set by the subclasses in their
        # start method if another component insists on owning the queue.
        self.results_passthrough: multiprocessing.Queue = multiprocessing.Queue()

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

    def _status_report(self, shutdown_event: threading.Event, heartbeat_period: float):
        while not shutdown_event.is_set():
            # waiting for the event returns True and wakes us early
            if shutdown_event.wait(timeout=heartbeat_period):
                pass
            else:
                status_report = self.get_status_report()
                packed = messagepack.pack(status_report)
                self.results_passthrough.put(packed)

    def _future_done_callback(self, future: Future):
        """Callback to post result to the passthrough queue

        Parameters
        ----------
        future: Future for which the callback is triggerd

        """
        logger.warning(f"[YADU] : Future done: {future}")

        if future.exception():
            code, user_message = get_result_error_details()
            error_details = {"code": code, "user_message": user_message}
            exec_end = TaskTransition(
                timestamp=time.time_ns(),
                state=TaskState.EXEC_END,
                actor=ActorName.WORKER,
            )
            result_message = dict(
                task_id=future.task_id,  # type: ignore
                data=get_error_string(),
                exception=get_error_string(),
                error_details=error_details,
                task_statuses=[exec_end],  # We don't have any more info transitions
            )
            packed_result = messagepack.pack(Result(**result_message))
        else:
            packed_result = future.result()

        self.results_passthrough.put(packed_result)

    @abstractmethod
    def submit(
        self,
        func: t.Callable,
        *args: t.Any,
        **kwargs: t.Any,
    ) -> Future:
        raise NotImplementedError()

    def submit_raw(self, packed_task: bytes) -> Future:
        """GC Endpoints should submit tasks via this method so that tasks are
        tracked properly.
        Parameters
        ----------
        packed_task: messagepack bytes buffer

        Returns
        -------
        future
        """

        task = messagepack.unpack(packed_task)
        assert isinstance(task, Task)
        future: Future = self.submit(execute_task, packed_task)

        # Executors mark futures are failed in the event of faults
        # We need to tie the task_id info into the future to identify
        # which tasks have failed
        future.task_id = task.task_id  # type: ignore
        future.add_done_callback(self._future_done_callback)
        return future
