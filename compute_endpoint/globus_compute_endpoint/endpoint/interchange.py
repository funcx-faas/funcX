from __future__ import annotations

import concurrent.futures
import logging
import multiprocessing
import os
import platform
import queue
import random
import signal
import sys
import threading
import time
import typing as t
from concurrent.futures import Future

import globus_compute_endpoint.endpoint.config
import pika.exceptions
import setproctitle
from globus_compute_common.messagepack import InvalidMessageError, pack
from globus_compute_common.messagepack.message_types import (
    EPStatusReport,
    Result,
    ResultErrorDetails,
)
from globus_compute_endpoint import __version__ as funcx_endpoint_version
from globus_compute_endpoint.endpoint.rabbit_mq import (
    ResultPublisher,
    TaskQueueSubscriber,
)
from globus_compute_endpoint.endpoint.result_store import ResultStore
from globus_compute_endpoint.engines.base import GlobusComputeEngineBase
from globus_compute_endpoint.exception_handling import get_result_error_details
from globus_compute_sdk import __version__ as funcx_sdk_version
from parsl.version import VERSION as PARSL_VERSION

log = logging.getLogger(__name__)


class _ResultPassthroughType(t.TypedDict):
    message: bytes
    task_id: str | None


class EndpointInterchange:
    """Interchange is a task orchestrator for distributed systems.

    1. Asynchronously queue large volume of tasks (>100K)
    2. Allow for workers to join and leave the union
    3. Detect workers that have failed using heartbeats
    4. Service single and batch requests from workers
    5. Be aware of requests worker resource capacity,
       eg. schedule only jobs that fit into walltime.

    """

    def __init__(
        self,
        config: globus_compute_endpoint.endpoint.config.Config,
        reg_info: dict[str, dict],
        logdir=".",
        endpoint_id=None,
        endpoint_dir=".",
        result_store: ResultStore | None = None,
        reconnect_attempt_limit: int = 5,
        parent_pid: int = 0,
    ):
        """
        Parameters
        ----------
        config : globus_compute_sdk.Config object
             Globus Compute config object describing how compute should be provisioned

        reg_info : dict[str, dict]
             Dictionary containing connection information for both the task and
             result queues.  The required data structure is returned from the
             Endpoint registration API call, encapsulated in the SDK by
             `Client.register_endpoint()`.

        logdir : str
             Parsl log directory paths. Logs and temp files go here. Default: '.'

        endpoint_id : str
             Identity string that identifies the endpoint to the broker

        endpoint_dir : pathlib.Path
             Endpoint directory path to store registration info in
        """
        self.logdir = logdir
        log.info(
            "Initializing EndpointInterchange process with Endpoint ID: {}".format(
                endpoint_id
            )
        )
        self.config = config

        self.endpoint_dir = endpoint_dir

        self.task_q_info = reg_info["task_queue_info"]
        self.result_q_info = reg_info["result_queue_info"]

        self.time_to_quit = False
        self.heartbeat_period = self.config.heartbeat_period

        self.pending_task_queue: queue.Queue = queue.Queue()

        self._reconnect_fail_counter = 0
        self.reconnect_attempt_limit = max(1, reconnect_attempt_limit)
        self._quiesce_event = multiprocessing.Event()
        self._parent_pid = parent_pid

        if result_store is None:
            result_store = ResultStore(endpoint_dir=endpoint_dir)
        self.result_store = result_store

        self.endpoint_id = endpoint_id

        self.current_platform = {
            "parsl_v": PARSL_VERSION,
            "python_v": "{}.{}.{}".format(
                sys.version_info.major, sys.version_info.minor, sys.version_info.micro
            ),
            "os": platform.system(),
            "hname": platform.node(),
            "funcx_sdk_version": funcx_sdk_version,
            "funcx_endpoint_version": funcx_endpoint_version,
            "registration": self.endpoint_id,
            "dir": os.getcwd(),
        }
        log.info(f"Platform info: {self.current_platform}")

        self.results_passthrough: queue.Queue[_ResultPassthroughType] = queue.Queue()
        # Rename self.executor -> self.engine in second round
        self.executor: GlobusComputeEngineBase = self.config.executors[0]
        self._test_start = False

    def start_engine(self):
        log.info("Starting Engine")
        self.executor.start(
            results_passthrough=self.results_passthrough,
            endpoint_id=self.endpoint_id,
            run_dir=self.logdir,
        )

    # def migrate_tasks_to_internal(
    #     self,
    #     connection_params: dict,
    #     endpoint_uuid: str,
    #     pending_task_queue: multiprocessing.Queue,
    #     quiesce_event: EventType,
    # ) -> multiprocessing.Process:
    #     """Pull tasks from the incoming tasks 0mq pipe onto the internal
    #     pending task queue

    #     Parameters:
    #     -----------
    #     connection_params: pika.connection.Parameters
    #           Connection params to connect to the service side Tasks queue

    #     endpoint_uuid: endpoint_uuid str

    #     pending_task_queue: multiprocessing.Queue
    #           Internal queue to which tasks should be migrated

    #     quiesce_event : EventType
    #           Event to let the thread know when it is time to die.
    #     """
    #     try:
    #         log.info(f"Starting the TaskQueueSubscriber as {endpoint_uuid}")
    #         task_q_proc = TaskQueueSubscriber(
    #             queue_info=connection_params,
    #             external_queue=pending_task_queue,
    #             quiesce_event=quiesce_event,
    #             endpoint_id=endpoint_uuid,
    #         )
    #         task_q_proc.start()
    #     except Exception:
    #         log.exception("Unhandled exception in TaskQueueSubscriber")
    #         raise

    #     return task_q_proc

    def quiesce(self):
        """Temporarily stop everything on the interchange in order to reach a consistent
        state before attempting to start again. This must be called on the main thread
        """
        log.info("Interchange Quiesce in progress (stopping and joining processes)")
        self._quiesce_event.set()

        # log.info("Waiting for quiesce complete")
        # self._task_puller_proc.join()

        log.info("Quiesce done")

    def stop(self):
        """Prepare the interchange for shutdown"""
        log.info("Shutting down EndpointInterchange")

        # set `time_to_quit` prior to `quiesce_event` to ensure that the interchange
        # will not start again after quiesce completes
        self.time_to_quit = True
        self._quiesce_event.set()

    def cleanup(self):
        self.executor.shutdown(block=True)

    def handle_sigterm(self, sig_num, curr_stack_frame):
        log.warning("Received SIGTERM, setting termination flag.")
        self.time_to_quit = True

    def function_allowed(self, function_id: str):
        if self.config.allowed_functions is None:
            # Not a restricted endpoint
            return True

        return function_id in self.config.allowed_functions

    def start(self):
        """Start the Interchange"""
        if self.time_to_quit:
            # Reminder to the dev: create afresh, don't reuse
            log.debug("Interchange object has stopped; it cannot be restarted")
            return

        signal.signal(signal.SIGHUP, self.handle_sigterm)
        signal.signal(signal.SIGQUIT, self.handle_sigterm)  # hint: Ctrl+\
        signal.signal(signal.SIGTERM, self.handle_sigterm)
        # Intentionally ignoring SIGINT for now, as we're unstable enough to
        # warrant Python's default developer-friendly Ctrl+C handling

        self._quiesce_event.clear()

        if self._parent_pid:
            if self._parent_pid != os.getppid():
                # initial check to save extra noise
                log.warning(f"Pid {self._parent_pid} not parent; refusing to start")
                self.stop()
                return

            def _parent_watcher(ppid: int):
                while ppid == os.getppid():
                    if not self._quiesce_event.wait(timeout=1):
                        return
                log.warning(f"Parent ({ppid}) has gone away; initiating shut down")
                self.stop()

            threading.Thread(
                target=_parent_watcher,
                args=(self._parent_pid,),  # copy; to discourage shenanigans
                daemon=True,
                name="ParentPidWatcher",
            ).start()

        log.info("Starting EndpointInterchange")

        # NOTE: currently we only start the executors once because
        # the current behavior is to keep them running decoupled while
        # the endpoint is waiting for reconnection
        self.start_engine()

        while not self.time_to_quit:
            if self._reconnect_fail_counter >= self.reconnect_attempt_limit:
                log.critical(
                    f"Failed {self._reconnect_fail_counter} consecutive times."
                    "  Shutting down."
                )
                self.stop()
                self.quiesce()
                break

            if self._quiesce_event.is_set():
                idle_for = random.uniform(2.0, 10.0)
                log.warning(f"Interchange will retry connecting in {idle_for:.2f}s")
                time.sleep(idle_for)
                self._quiesce_event.clear()
            else:
                log.debug("Starting threads and main loop")

            if self.time_to_quit:
                self.stop()
                self.quiesce()
                break

            try:
                # self._start_threads_and_main()
                self._main_loop()
            except pika.exceptions.ConnectionClosedByBroker as e:
                log.warning(f"AMQP service closed connection: {e}")
            except pika.exceptions.ProbableAuthenticationError as e:
                log.error(f"Unable to connect to AMQP service: {e}")
                self.time_to_quit = True
            except Exception as e:
                log.error(
                    f"Unhandled exception in main kernel: ({type(e).__name__}) {e}"
                )
                log.debug("Unhandled exception in main kernel.", exc_info=e)
            finally:
                if not self.time_to_quit:
                    self._reconnect_fail_counter += 1
                    log.info(
                        "Reconnection count: %s (of %s)",
                        self._reconnect_fail_counter,
                        self.reconnect_attempt_limit,
                    )

            self.quiesce()

        self.cleanup()
        log.info("EndpointInterchange shutdown complete.")

    # def _start_threads_and_main(self):
    #     self._task_puller_proc = self.migrate_tasks_to_internal(
    #         self.task_q_info,
    #         self.endpoint_id,
    #         self.pending_task_queue,
    #         self._quiesce_event,
    #     )

    #     self._main_loop()

    def _main_loop(self):
        """
        This is the "kernel" of the endpoint interchange process.  Conceptually, there
        are three actions of consequence: forward task messages to the executors,
        forward results from the executors back to the Globus Compute web services
        (RMQ), and forward any previous results that may have failed to send previously
        (e.g., if a RMQ connection was dropped).

        We accomplish this via three threads, one each for each task.

        Of special note is that this kernel does not try very hard to handle the
        non-happy path.  If an error occurs that is too "unhappy" (e.g., communication
        with RMQ fails), the catch-all solution is to "reboot" and try again.  That is,
        this method exits, communication is shutdown, started again, and we restart
        this method.
        """
        log.debug("_main_loop begins")

        task_q_subscriber = TaskQueueSubscriber(
            queue_info=self.task_q_info, external_queue=self.pending_task_queue
        )
        task_q_subscriber.start()

        results_publisher = ResultPublisher(queue_info=self.result_q_info)
        results_publisher.start()

        executor = self.executor

        num_tasks_forwarded = 0
        num_results_forwarded = 0

        def process_stored_results():
            # Handle any previously stored results, either from a previous run or
            # from a quarter of a second-ago.  Basically, check every second
            # (.wait()) if there are any items on disk to be sent.  If there are,
            # don't treat them any differently than "fresh" results: put the into
            # the same multiprocessing queue as results incoming directly from
            # the executors.  The normal processing by `process_pending_results()`
            # will take over from there.
            while not self._quiesce_event.wait(timeout=1):
                for task_id, packed_result in self.result_store:
                    log.debug("Retrieved stored result (%s)", task_id)
                    msg = {"task_id": task_id, "message": packed_result}
                    self.result_store.discard(task_id)
                    self.results_passthrough.put(msg)

                    if self._quiesce_event.is_set():
                        # important to check every iteration as well, so as not to
                        # potentially hang up the shutdown procedure
                        return
            log.debug("Exit process-stored-results thread.")

        def process_pending_tasks() -> None:
            # Pull tasks from upstream (RMQ) and send them down the ZMQ pipe to the
            # globus-compute-manager.  In terms of shutting down (or "rebooting")
            # gracefully, iterate once a second whether a task has arrived.
            nonlocal num_tasks_forwarded
            while not self._quiesce_event.is_set():
                if getattr(executor, "executor_exception", False):
                    self.time_to_quit = True
                    log.exception(executor.executor_exception)

                if self.time_to_quit:
                    self.stop()
                    continue  # nominally == break; but let event do it

                try:
                    prop_headers, body = self.pending_task_queue.get(timeout=1)

                    fid: str = prop_headers.get("function_uuid")
                    tid: str = prop_headers.get("task_uuid")
                    if not fid or not tid:
                        raise InvalidMessageError("Task message missing headers")

                    if fid and not self.function_allowed(fid):
                        # Same as web-service message but packed in a
                        # result error
                        reject_msg = (
                            f"Function {fid} not permitted on "
                            f"endpoint {self.endpoint_id}"
                        )
                        log.warning(reject_msg)

                        failed_result = Result(
                            task_id=tid,
                            data=reject_msg,
                            error_details=ResultErrorDetails(
                                code="FUNCTION_NOT_ALLOWED", user_message=reject_msg
                            ),
                        )
                        results_publisher.publish(pack(failed_result))
                        continue

                except queue.Empty:
                    continue

                except Exception:
                    log.exception("Unhandled error processing incoming task")
                    continue

                try:
                    executor.submit(task_id=tid, packed_task=body)
                    num_tasks_forwarded += 1  # Safe given GIL

                except Exception as exc:
                    log.exception(f"Failed to process task {tid}")
                    code, msg = get_result_error_details()
                    failed_result = Result(
                        task_id=tid,
                        data=f"Failed to start task: {exc}",
                        error_details=ResultErrorDetails(code=code, user_message=msg),
                        task_statuses=[],
                    )
                    res: _ResultPassthroughType = {
                        "task_id": tid,
                        "message": pack(failed_result),
                    }
                    self.results_passthrough.put(res)

            log.debug("Exit process-pending-tasks thread.")

        def process_pending_results() -> None:
            # Forward incoming results from the globus-compute-manager to the
            # Globus Compute services. For graceful handling of shutdown
            # (or "reboot"), wait up to a second or incoming results before
            # iterating the loop regardless.
            nonlocal num_results_forwarded

            def _create_done_cb(mq_msg: bytes, tid: str | None):
                def _done_cb(pub_fut: Future):
                    _exc = pub_fut.exception()
                    if _exc:
                        # Publishing didn't work -- quiesce and see if a simple
                        # restart fixes the issue.
                        if tid:
                            log.info(f"Storing result for later: {tid}")
                            self.result_store[tid] = mq_msg

                        self._quiesce_event.set()
                        log.error("Failed to publish results", exc_info=_exc)

                return _done_cb

            while not self._quiesce_event.is_set():
                try:
                    msg = self.results_passthrough.get(timeout=1)
                    packed_message: bytes = msg["message"]
                    task_id: str | None = msg.get("task_id")

                except queue.Empty:
                    continue

                except Exception as exc:
                    log.warning(
                        "Invalid message received.  Ignoring."
                        f"  ([{type(exc).__name__}] {exc})"
                    )
                    continue

                if task_id:
                    num_results_forwarded += 1
                    log.debug("Forwarding result for task: %s", task_id)

                try:
                    f = results_publisher.publish(packed_message)
                    f.add_done_callback(_create_done_cb(packed_message, task_id))

                except Exception:
                    # Publishing didn't work -- quiesce and see if a simple restart
                    # fixes the issue.
                    self._quiesce_event.set()

                    log.exception(
                        "Something broke while forwarding results; setting quiesce"
                        " event"
                    )
                    if task_id:
                        log.info("Storing result for later: %s", task_id)
                        self.result_store[task_id] = packed_message
                    continue  # just be explicit

            log.debug("Exit process-pending-results thread.")

        stored_processor_thread = threading.Thread(
            target=process_stored_results, daemon=True, name="Stored Result Handler"
        )
        task_processor_thread = threading.Thread(
            target=process_pending_tasks, daemon=True, name="Pending Task Handler"
        )
        result_processor_thread = threading.Thread(
            target=process_pending_results, daemon=True, name="Pending Result Handler"
        )
        stored_processor_thread.start()
        task_processor_thread.start()
        result_processor_thread.start()

        connection_stable_hearbeats = 0
        last_t, last_r = 0, 0

        soft_idle_limit = max(0, self.config.idle_heartbeats_soft)
        hard_idle_limit = max(soft_idle_limit + 1, self.config.idle_heartbeats_hard)
        soft_idle_heartbeats = 0  # "happy path" idle timeout
        hard_idle_heartbeats = 0  # catch-all idle timeout

        live_proc_title = setproctitle.getproctitle()
        log.debug("_main_loop entered running state")
        while not self._quiesce_event.wait(self.heartbeat_period):
            # Possibly TOCTOU here, but we don't need to be super precise.  The
            # point here is to mention "still alive" and that we're still working
            num_t, num_r = num_tasks_forwarded, num_results_forwarded
            diff_t, diff_r = num_t - last_t, num_r - last_r
            log.debug(
                "Heartbeat.  Approximate Tasks and Results forwarded since last "
                "heartbeat: %s (T), %s (R)",
                diff_t,
                diff_r,
            )
            last_t, last_r = num_t, num_r

            # only reset come 2 heartbeats and still alive
            if self._reconnect_fail_counter:
                connection_stable_hearbeats += 1
                if connection_stable_hearbeats > 1:
                    log.info("Connection stable for 2 heartbeats; reset fail count")
                    self._reconnect_fail_counter = 0

            if not soft_idle_limit:
                # idle timeout not enabled; "always on"
                continue

            if diff_t or diff_r:
                # a task moved; reset idle heartbeat counter
                if soft_idle_heartbeats or hard_idle_heartbeats:
                    log.info(
                        "Moved to active state (due to tasks processed since"
                        " last heartbeat)."
                    )
                setproctitle.setproctitle(live_proc_title)
                soft_idle_heartbeats = 0
                hard_idle_heartbeats = 0
                continue

            # only start "timer" if we've at least done *some* work
            hard_idle_heartbeats += 1
            if (num_t or num_r) and num_r >= num_t:
                # similar to above, only start "timer" if *idle* ... but
                # note that given self.result_store, it's possible to
                # have forwarded more results than tasks received.
                soft_idle_heartbeats += 1
                shutdown_s = soft_idle_limit - soft_idle_heartbeats
                shutdown_s *= self.heartbeat_period

                if soft_idle_heartbeats == 1:
                    log.info(
                        "In idle state (due to no task or result movement);"
                        f" shut down in {shutdown_s:,}s.  (idle_heartbeats_soft)"
                    )
                idle_proc_title = "[idle; shut down in {:,}s] {}"
                setproctitle.setproctitle(
                    idle_proc_title.format(shutdown_s, live_proc_title)
                )

                if soft_idle_heartbeats >= soft_idle_limit:
                    log.info("Idle heartbeats reached.  Shutting down.")
                    self.time_to_quit = True
                    self.stop()

            elif hard_idle_heartbeats > hard_idle_limit:
                log.warning("Shutting down due to idle heartbeats HARD limit.")
                self.time_to_quit = True
                self.stop()

            elif hard_idle_heartbeats > soft_idle_limit:
                # Reminder: this branch only hit if EP started and no tasks
                # or results have moved.  If *any* movement occurs, this branch
                # won't get executed.
                shutdown_s = hard_idle_limit - hard_idle_heartbeats
                shutdown_s *= self.heartbeat_period
                if hard_idle_heartbeats == soft_idle_limit + 1:
                    # only log the first time; no sense in filling logs
                    log.info(
                        "Possibly idle -- no task or result movement.  Will"
                        f" shut down in {shutdown_s:,}s.  (idle_heartbeats_hard)"
                    )
                idle_proc_title = "[possibly idle; shut down in {:,}s] {}"
                setproctitle.setproctitle(
                    idle_proc_title.format(shutdown_s, live_proc_title)
                )

        # The timeouts aren't nominally necessary because if the above loop has
        # quit, then the _quiesce_event is set, and both threads check that event
        # every internal iteration.  But "for kicks."
        stored_processor_thread.join(timeout=5)
        task_processor_thread.join(timeout=5)
        result_processor_thread.join(timeout=5)

        # let higher-level error handling take over if the following excepts
        message = EPStatusReport(
            endpoint_id=self.endpoint_id,
            global_state={
                "managers": 0,
                "total_workers": 0,
                "idle_workers": 0,
                "pending_tasks": 0,
                "outstanding_tasks": {},
                "heartbeat_period": 0,  # 0 == "shutting down now"
            },
            task_statuses={},
        )
        try:
            f = results_publisher.publish(pack(message))
            f.result(timeout=5)
        except concurrent.futures.TimeoutError:
            log.warning(
                "Unable to send final heartbeat (timeout sending); ignoring for quiesce"
            )

        task_q_subscriber.stop()
        results_publisher.stop()

        log.debug("_main_loop exits")
