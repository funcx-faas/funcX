#!/usr/bin/env python
import argparse
from typing import Tuple, Dict

import zmq
import os
import sys
import platform
import random
import time
import pickle
import logging
import queue
import threading
import json
import daemon
import collections
from funcx_endpoint.executors.high_throughput.mac_safe_queue import mpQueue

from parsl.executors.errors import ScalingFailed
from parsl.version import VERSION as PARSL_VERSION

from funcx_endpoint.executors.high_throughput.messages import Message, COMMAND_TYPES, MessageType, Task
from funcx_endpoint.executors.high_throughput.messages import EPStatusReport, Heartbeat, TaskStatusCode
from funcx.sdk.client import FuncXClient
from funcx import set_file_logger
from funcx_endpoint.executors.high_throughput.interchange_task_dispatch import naive_interchange_task_dispatch
from funcx.serialize import FuncXSerializer
from funcx_endpoint.endpoint.taskqueue import TaskQueue
from queue import Queue

LOOP_SLOWDOWN = 0.0  # in seconds
HEARTBEAT_CODE = (2 ** 32) - 1
PKL_HEARTBEAT_CODE = pickle.dumps(HEARTBEAT_CODE)


class ShutdownRequest(Exception):
    """ Exception raised when any async component receives a ShutdownRequest
    """

    def __init__(self):
        self.tstamp = time.time()

    def __repr__(self):
        return "Shutdown request received at {}".format(self.tstamp)


class ManagerLost(Exception):
    """ Task lost due to worker loss. Worker is considered lost when multiple heartbeats
    have been missed.
    """

    def __init__(self, worker_id):
        self.worker_id = worker_id
        self.tstamp = time.time()

    def __repr__(self):
        return "Task failure due to loss of worker {}".format(self.worker_id)


class BadRegistration(Exception):
    ''' A new Manager tried to join the executor with a BadRegistration message
    '''

    def __init__(self, worker_id, critical=False):
        self.worker_id = worker_id
        self.tstamp = time.time()
        self.handled = "critical" if critical else "suppressed"

    def __repr__(self):
        return "Manager:{} caused a {} failure".format(self.worker_id,
                                                       self.handled)


class EndpointInterchange(object):
    """ Interchange is a task orchestrator for distributed systems.

    1. Asynchronously queue large volume of tasks (>100K)
    2. Allow for workers to join and leave the union
    3. Detect workers that have failed using heartbeats
    4. Service single and batch requests from workers
    5. Be aware of requests worker resource capacity,
       eg. schedule only jobs that fit into walltime.

    TODO: We most likely need a PUB channel to send out global commandzs, like shutdown
    """

    def __init__(self,
                 config,
                 client_address="127.0.0.1",
                 interchange_address="127.0.0.1",
                 client_ports: Tuple[int, int, int] = (50055, 50056, 50057),
                 launch_cmd=None,
                 logdir=".",
                 logging_level=logging.INFO,
                 endpoint_id=None,
                 keys_dir=".curve",
                 suppress_failure=True,
                 ):
        """
        Parameters
        ----------
        config : funcx.Config object
             Funcx config object that describes how compute should be provisioned

        client_address : str
             The ip address at which the parsl client can be reached. Default: "127.0.0.1"

        interchange_address : str
             The ip address at which the workers will be able to reach the Interchange. Default: "127.0.0.1"

        client_ports : Tuple[int, int, int]
             The ports at which the client can be reached

        launch_cmd : str
             TODO : update

        logdir : str
             Parsl log directory paths. Logs and temp files go here. Default: '.'

        logging_level : int
             Logging level as defined in the logging module. Default: logging.INFO (20)

        keys_dir : str
             Directory from where keys used for communicating with the funcX service (forwarders)
             are stored

        endpoint_id : str
             Identity string that identifies the endpoint to the broker

        suppress_failure : Bool
             When set to True, the interchange will attempt to suppress failures. Default: False
        """
        self.logdir = logdir
        try:
            os.makedirs(self.logdir)
        except FileExistsError:
            pass

        global logger

        logger = set_file_logger(os.path.join(self.logdir, "EndpointInterchange.log"), name="funcx_endpoint", level=logging_level)
        logger.info("Initializing EndpointInterchange process with Endpoint ID: {}".format(endpoint_id))
        self.config = config
        logger.info("Got config : {}".format(config))

        self.client_address = client_address
        self.interchange_address = interchange_address
        self.client_ports = client_ports
        self.suppress_failure = suppress_failure

        self.heartbeat_period = self.config.heartbeat_period
        self.heartbeat_threshold = self.config.heartbeat_threshold
        # initalize the last heartbeat time to start the loop
        self.last_heartbeat = time.time()
        self.keys_dir = keys_dir
        self.serializer = FuncXSerializer()
        logger.info("Attempting connection to client at {} on ports: {},{},{}".format(
            client_address, client_ports[0], client_ports[1], client_ports[2]))

        self.command_channel = TaskQueue(client_address,
                                         port=client_ports[2],
                                         identity=endpoint_id,
                                         mode='client',
                                         RCVTIMEO=1000,  # in milliseconds
                                         keys_dir=keys_dir,
                                         set_hwm=True)

        # TODO :Register all channels with the authentication string.
        self.command_channel.put('forwarder', pickle.dumps({"registration": endpoint_id}))
        logger.info(f"Connected to funcX forwarder at {client_address}")

        self.pending_task_queue = Queue()
        self.containers = {}
        self.total_pending_task_count = 0
        self.fxs = FuncXClient()

        logger.info("Interchange address is {}".format(self.interchange_address))

        self.endpoint_id = endpoint_id

        self.current_platform = {'parsl_v': PARSL_VERSION,
                                 'python_v': "{}.{}.{}".format(sys.version_info.major,
                                                               sys.version_info.minor,
                                                               sys.version_info.micro),
                                 'libzmq_v': zmq.zmq_version(),
                                 'pyzmq_v': zmq.pyzmq_version(),
                                 'os': platform.system(),
                                 'hname': platform.node(),
                                 'dir': os.getcwd()}

        logger.info("Platform info: {}".format(self.current_platform))
        try:
            self.load_config()
        except Exception:
            logger.exception("Caught exception")
            raise

        self.tasks = set()
        self.task_status_deltas = {}

    def load_config(self):
        """ Load the config
        """
        logger.info("Loading endpoint local config")

        self.results_passthrough = mpQueue()
        self.executors = {}
        for executor in self.config.executors:
            logger.info(f"Initializing executor: {executor.label}")
            executor.funcx_service_address = self.config.funcx_service_address
            if not executor.endpoint_id:
                executor.endpoint_id = self.endpoint_id
            else:
                if not executor.endpoint_id == self.endpoint_id:
                    raise Exception('InconsistentEndpointId')
            self.executors[executor.label] = executor
            if executor.run_dir is None:
                executor.run_dir = self.logdir
            if hasattr(executor, 'passthrough') and executor.passthrough is True:
                executor.start(results_passthrough=self.results_passthrough)
                # executor._start_remote_interchange_process()

    def migrate_tasks_to_internal(self, kill_event, status_request):
        """Pull tasks from the incoming tasks 0mq pipe onto the internal
        pending task queue

        Parameters:
        -----------
        kill_event : threading.Event
              Event to let the thread know when it is time to die.
        """
        logger.info("[TASK_PULL_THREAD] Starting")
        task_counter = 0
        # Create the incoming queue in the thread to keep
        # zmq.context in the same thread. zmq.context is not thread-safe
        self.task_incoming = TaskQueue(self.client_address,
                                       port=self.client_ports[0],
                                       identity=self.endpoint_id,
                                       mode='client',
                                       set_hwm=True,
                                       keys_dir=self.keys_dir,
                                       RCVTIMEO=1000)
        self.task_incoming.put('forwarder', pickle.dumps({"registration": self.endpoint_id}))
        logger.info(f"Task incoming on tcp://{self.client_address}:{self.client_ports[0]}")

        poller = zmq.Poller()
        poller.register(self.task_incoming, zmq.POLLIN)

        while not kill_event.is_set():

            try:
                if int(time.time() - self.last_heartbeat) > self.heartbeat_threshold:
                    logger.critical("[TASK_PULL_THREAD] Missed too many heartbeats. Setting kill event.")
                    kill_event.set()
                    break

                try:
                    # TODO : Check the kwarg options for get
                    raw_msg = self.task_incoming.get()[0]
                    self.last_heartbeat = time.time()
                except zmq.Again:
                    # We just timed out while attempting to receive
                    logger.debug("[TASK_PULL_THREAD] {} tasks in internal queue".format(self.total_pending_task_count))
                    continue
                except Exception:
                    logger.exception("[TASK_PULL_THREAD] Unknown exception while waiting for tasks")

                # YADU: TODO We need to do the routing here
                try:
                    msg = Message.unpack(raw_msg)
                except Exception:
                    logger.exception("[TASK_PULL_THREAD] Failed to unpack message from forwarder")
                    pass

                if msg == 'STOP':
                    kill_event.set()
                    break

                elif isinstance(msg, Heartbeat):
                    logger.info("[TASK_PULL_THREAD] Got heartbeat from funcx-forwarder")

                elif isinstance(msg, Task):
                    logger.info(f"[TASK_PULL_THREAD] Received task:{msg.task_id}")
                    self.pending_task_queue.put(msg)
                    self.total_pending_task_count += 1
                    self.task_status_deltas[msg.task_id] = TaskStatusCode.WAITING_FOR_NODES
                    task_counter += 1
                    logger.debug(f"[TASK_PULL_THREAD] Task counter:{task_counter} Pending Tasks: {self.total_pending_task_count}")

                else:
                    logger.warning(f"[TASK_PULL_THREAD] Unknown message type received: {msg}")

            except Exception:
                logger.exception("[TASK_PULL_THREAD] Something really bad happened")
                continue

    def get_container(self, container_uuid):
        """ Get the container image location if it is not known to the interchange"""
        if container_uuid not in self.containers:
            if container_uuid == 'RAW' or not container_uuid:
                self.containers[container_uuid] = 'RAW'
            else:
                try:
                    container = self.fxs.get_container(container_uuid, self.config.container_type)
                except Exception:
                    logger.exception("[FETCH_CONTAINER] Unable to resolve container location")
                    self.containers[container_uuid] = 'RAW'
                else:
                    logger.info("[FETCH_CONTAINER] Got container info: {}".format(container))
                    self.containers[container_uuid] = container.get('location', 'RAW')
        return self.containers[container_uuid]

    def _status_report_loop(self, kill_event, status_report_queue: queue.Queue):
        logger.debug("[STATUS] Status reporting loop starting")
        """
        while not kill_event.is_set():
            msg = EPStatusReport(
                self.endpoint_id,
                self.get_status_report(),
                self.task_status_deltas
            )
            logger.info("[STATUS] Sending status report to forwarder, and clearing task deltas.")
            status_report_queue.put(msg.pack())
            self.task_status_deltas.clear()
            time.sleep(self.heartbeat_period)
        """
        pass

    def _command_server(self, kill_event):
        """ Command server to run async command to the interchange

        We want to be able to receive the following not yet implemented/updated commands:
         - OutstandingCount
         - ListManagers (get outstanding broken down by manager)
         - HoldWorker
         - Shutdown
        """
        logger.debug("[COMMAND] Command Server Starting")

        while not kill_event.is_set():
            try:
                # Wait for 1000 ms
                buffer = self.command_channel.get(timeout=1000)
                logger.debug(f"[COMMAND] Received command request {buffer}")
                command = Message.unpack(buffer)
                if command.type not in COMMAND_TYPES:
                    logger.error("Received incorrect message type on command channel")
                    self.command_channel.put(bytes())
                    continue

                if command.type is MessageType.HEARTBEAT_REQ:
                    logger.info("[COMMAND] Received synchonous HEARTBEAT_REQ from hub")
                    logger.info(f"[COMMAND] Replying with Heartbeat({self.endpoint_id})")
                    reply = Heartbeat(self.endpoint_id)

                logger.debug("[COMMAND] Reply: {}".format(reply))
                self.command_channel.put(reply.pack())

            except zmq.Again:
                # logger.debug("[COMMAND] is alive")
                continue

    def stop(self):
        """Prepare the interchange for shutdown"""
        self._kill_event.set()
        self._task_puller_thread.join()
        self._command_thread.join()

    def start(self):
        """ Start the Interchange
        """
        logger.info("Starting EndpointInterchange")

        start = time.time()
        count = 0

        self._kill_event = threading.Event()
        self._status_request = threading.Event()
        self._task_puller_thread = threading.Thread(target=self.migrate_tasks_to_internal,
                                                    args=(self._kill_event, self._status_request, ))
        self._task_puller_thread.start()

        self._command_thread = threading.Thread(target=self._command_server,
                                                args=(self._kill_event, ))
        self._command_thread.start()

        status_report_queue = queue.Queue()
        self._status_report_thread = threading.Thread(target=self._status_report_loop,
                                                      args=(self._kill_event, status_report_queue))
        self._status_report_thread.start()

        self.results_outgoing = TaskQueue(self.client_address,
                                          port=self.client_ports[1],
                                          identity=self.endpoint_id,
                                          mode='client',
                                          keys_dir=self.keys_dir,
                                          # Fail immediately if results cannot be sent back
                                          SNDTIMEO=0,
                                          set_hwm=True)
        self.results_outgoing.put('forwarder', pickle.dumps({"registration": self.endpoint_id}))

        executor = list(self.executors.values())[0]
        last = time.time()

        while True:
            if last + self.heartbeat_threshold < time.time():
                logger.debug("[MAIN] alive")
                last = time.time()
                try:
                    # Adding results heartbeat to essentially force a TCP keepalive
                    # without meddling with OS TCP keepalive defaults
                    self.results_outgoing.put('forwarder', b'HEARTBEAT')
                except Exception:
                    logger.exception("[MAIN] Sending heartbeat to the forwarder over the results channel has failed")
                    raise

            try:
                task = self.pending_task_queue.get(block=True, timeout=0.01)
                executor.submit_raw(task.pack())
            except queue.Empty:
                pass
            except Exception:
                logger.exception("[MAIN] Unhandled issue while waiting for pending tasks")
                pass

            try:
                results = self.results_passthrough.get(False, 0.01)

                # results will be a pickled dict with task_id, container_id, and results/exception
                self.results_outgoing.put('forwarder', results)
                logger.info("Passing result to forwarder")

            except queue.Empty:
                pass

            except Exception:
                logger.exception("[MAIN] Something broke while forwarding results from executor to forwarder queues")
                continue

        delta = time.time() - start
        logger.info("Processed {} tasks in {} seconds".format(count, delta))
        logger.warning("Exiting")

    def get_status_report(self):
        """ Get utilization numbers
        """
        total_cores = 0
        total_mem = 0
        core_hrs = 0
        active_managers = 0
        free_capacity = 0
        outstanding_tasks = self.get_total_tasks_outstanding()
        pending_tasks = self.total_pending_task_count
        num_managers = len(self._ready_manager_queue)
        live_workers = self.get_total_live_workers()

        for manager in self._ready_manager_queue:
            total_cores += self._ready_manager_queue[manager]['cores']
            total_mem += self._ready_manager_queue[manager]['mem']
            active_dur = abs(time.time() - self._ready_manager_queue[manager]['reg_time'])
            core_hrs += (active_dur * total_cores) / 3600
            if self._ready_manager_queue[manager]['active']:
                active_managers += 1
            free_capacity += self._ready_manager_queue[manager]['free_capacity']['total_workers']

        result_package = {'task_id': -2,
                          'info': {'total_cores': total_cores,
                                   'total_mem': total_mem,
                                   'new_core_hrs': core_hrs - self.last_core_hr_counter,
                                   'total_core_hrs': round(core_hrs, 2),
                                   'managers': num_managers,
                                   'active_managers': active_managers,
                                   'total_workers': live_workers,
                                   'idle_workers': free_capacity,
                                   'pending_tasks': pending_tasks,
                                   'outstanding_tasks': outstanding_tasks,
                                   'worker_mode': self.config.worker_mode,
                                   'scheduler_mode': self.config.scheduler_mode,
                                   'scaling_enabled': self.config.scaling_enabled,
                                   'mem_per_worker': self.config.mem_per_worker,
                                   'cores_per_worker': self.config.cores_per_worker,
                                   'prefetch_capacity': self.config.prefetch_capacity,
                                   'max_blocks': self.config.provider.max_blocks,
                                   'min_blocks': self.config.provider.min_blocks,
                                   'max_workers_per_node': self.config.max_workers_per_node,
                                   'nodes_per_block': self.config.provider.nodes_per_block
        }}

        self.last_core_hr_counter = core_hrs
        return result_package

    def scale_out(self, blocks=1, task_type=None):
        """Scales out the number of blocks by "blocks"

        Raises:
             NotImplementedError
        """
        r = []
        for i in range(blocks):
            if self.config.provider:
                self._block_counter += 1
                external_block_id = str(self._block_counter)
                if not task_type and self.config.scheduler_mode == 'hard':
                    launch_cmd = self.launch_cmd.format(block_id=external_block_id, worker_type='RAW')
                else:
                    launch_cmd = self.launch_cmd.format(block_id=external_block_id, worker_type=task_type)
                if not task_type:
                    internal_block = self.config.provider.submit(launch_cmd, 1)
                else:
                    internal_block = self.config.provider.submit(launch_cmd, 1, task_type)
                logger.debug("Launched block {}->{}".format(external_block_id, internal_block))
                if not internal_block:
                    raise(ScalingFailed(self.provider.label,
                                        "Attempts to provision nodes via provider has failed"))
                self.blocks[external_block_id] = internal_block
                self.block_id_map[internal_block] = external_block_id
            else:
                logger.error("No execution provider available")
                r = None
        return r

    def scale_in(self, blocks=None, block_ids=[], task_type=None):
        """Scale in the number of active blocks by specified amount.

        Parameters
        ----------
        blocks : int
            # of blocks to terminate

        block_ids : [str.. ]
            List of external block ids to terminate
        """
        if task_type:
            logger.info("Scaling in blocks of specific task type {}. Let the provider decide which to kill".format(task_type))
            if self.config.scaling_enabled and self.config.provider:
                to_kill, r = self.config.provider.cancel(blocks, task_type)
                logger.info("Get the killed blocks: {}, and status: {}".format(to_kill, r))
                for job in to_kill:
                    logger.info("[scale_in] Getting the block_id map {} for job {}".format(self.block_id_map, job))
                    block_id = self.block_id_map[job]
                    logger.info("[scale_in] Holding block {}".format(block_id))
                    self._hold_block(block_id)
                    self.blocks.pop(block_id)
                return r

        if block_ids:
            block_ids_to_kill = block_ids
        else:
            block_ids_to_kill = list(self.blocks.keys())[:blocks]

        # Try a polite terminate
        # TODO : Missing logic to hold blocks
        for block_id in block_ids_to_kill:
            self._hold_block(block_id)

        # Now kill via provider
        to_kill = [self.blocks.pop(bid) for bid in block_ids_to_kill]

        if self.config.scaling_enabled and self.config.provider:
            r = self.config.provider.cancel(to_kill)

        return r

    def provider_status(self):
        """ Get status of all blocks from the provider
        """
        status = []
        if self.config.provider:
            logger.debug("[MAIN] Getting the status of {} blocks.".format(list(self.blocks.values())))
            status = self.config.provider.status(list(self.blocks.values()))
            logger.debug("[MAIN] The status is {}".format(status))

        return status


def starter(comm_q, *args, **kwargs):
    """Start the interchange process

    The executor is expected to call this function. The args, kwargs match that of the Interchange.__init__
    """
    # logger = multiprocessing.get_logger()
    ic = EndpointInterchange(*args, **kwargs)
    # comm_q.put((ic.worker_task_port,
    #            ic.worker_result_port))
    ic.start()


def cli_run():

    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--client_address", required=True,
                        help="Client address")
    parser.add_argument("--client_ports", required=True,
                        help="client ports as a triple of outgoing,incoming,command")
    parser.add_argument("--worker_port_range",
                        help="Worker port range as a tuple")
    parser.add_argument("-l", "--logdir", default="./parsl_worker_logs",
                        help="Parsl worker log directory")
    parser.add_argument("--worker_ports", default=None,
                        help="OPTIONAL, pair of workers ports to listen on, eg --worker_ports=50001,50005")
    parser.add_argument("--suppress_failure", action='store_true',
                        help="Enables suppression of failures")
    parser.add_argument("--endpoint_id", required=True,
                        help="Endpoint ID, used to identify the endpoint to the remote broker")
    parser.add_argument("--hb_threshold",
                        help="Heartbeat threshold in seconds")
    parser.add_argument("--config", default=None,
                        help="Configuration object that describes provisioning")
    parser.add_argument("-d", "--debug", action='store_true',
                        help="Enables debug logging")

    print("Starting HTEX Intechange")
    args = parser.parse_args()

    optionals = {}
    optionals['suppress_failure'] = args.suppress_failure
    optionals['logdir'] = os.path.abspath(args.logdir)
    optionals['client_address'] = args.client_address
    optionals['client_ports'] = [int(i) for i in args.client_ports.split(',')]
    optionals['endpoint_id'] = args.endpoint_id

    # DEBUG ONLY : TODO: FIX
    if args.config is None:
        from funcx_endpoint.endpoint.utils.config import Config
        from parsl.providers import LocalProvider

        config = Config(
            worker_debug=True,
            scaling_enabled=True,
            provider=LocalProvider(
                init_blocks=1,
                min_blocks=1,
                max_blocks=1,
            ),
            max_workers_per_node=2,
            funcx_service_address='http://127.0.0.1:8080'
        )
        optionals['config'] = config
    else:
        optionals['config'] = args.config

    if args.debug:
        optionals['logging_level'] = logging.DEBUG
    if args.worker_ports:
        optionals['worker_ports'] = [int(i) for i in args.worker_ports.split(',')]
    if args.worker_port_range:
        optionals['worker_port_range'] = [int(i) for i in args.worker_port_range.split(',')]

    ic = EndpointInterchange(**optionals)
    ic.start()

    """
    with daemon.DaemonContext():
    """
