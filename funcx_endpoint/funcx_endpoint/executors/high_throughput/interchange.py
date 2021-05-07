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

from logging.handlers import RotatingFileHandler

from parsl.executors.errors import ScalingFailed
from parsl.version import VERSION as PARSL_VERSION
from parsl.app.errors import RemoteExceptionWrapper

from funcx_endpoint.executors.high_throughput.messages import Message, COMMAND_TYPES, MessageType, Task
from funcx_endpoint.executors.high_throughput.messages import EPStatusReport, Heartbeat, TaskStatusCode
from funcx.sdk.client import FuncXClient
from funcx import set_file_logger
from funcx_endpoint.executors.high_throughput.interchange_task_dispatch import naive_interchange_task_dispatch
from funcx.serialize import FuncXSerializer

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

    def __str__(self):
        return self.__repr__()


class ManagerLost(Exception):
    """ Task lost due to worker loss. Worker is considered lost when multiple heartbeats
    have been missed.
    """

    def __init__(self, worker_id):
        self.worker_id = worker_id
        self.tstamp = time.time()

    def __repr__(self):
        return "Task failure due to loss of manager {}".format(self.worker_id)

    def __str__(self):
        return self.__repr__()


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

    def __str__(self):
        return self.__repr__()


class Interchange(object):
    """ Interchange is a task orchestrator for distributed systems.

    1. Asynchronously queue large volume of tasks (>100K)
    2. Allow for workers to join and leave the union
    3. Detect workers that have failed using heartbeats
    4. Service single and batch requests from workers
    5. Be aware of requests worker resource capacity,
       eg. schedule only jobs that fit into walltime.

    TODO: We most likely need a PUB channel to send out global commands, like shutdown
    """

    def __init__(self,
                 #
                 strategy=None,
                 poll_period=None,
                 heartbeat_period=None,
                 heartbeat_threshold=None,
                 working_dir=None,
                 provider=None,
                 max_workers_per_node=None,
                 mem_per_worker=None,
                 prefetch_capacity=None,
                 scheduler_mode=None,
                 container_type=None,
                 funcx_service_address=None,
                 worker_mode=None,
                 scaling_enabled=True,
                 #
                 client_address="127.0.0.1",
                 interchange_address="127.0.0.1",
                 client_ports: Tuple[int, int, int] = (50055, 50056, 50057),
                 worker_ports=None,
                 worker_port_range=(54000, 55000),
                 cores_per_worker=1.0,
                 worker_debug=False,
                 launch_cmd=None,
                 logdir=".",
                 logging_level=logging.INFO,
                 endpoint_id=None,
                 suppress_failure=False,
                 log_max_bytes=256 * 1024 * 1024,
                 log_backup_count=1,
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

        worker_ports : tuple(int, int)
             The specific two ports at which workers will connect to the Interchange. Default: None

        worker_port_range : tuple(int, int)
             The interchange picks ports at random from the range which will be used by workers.
             This is overridden when the worker_ports option is set. Defauls: (54000, 55000)

        cores_per_worker : float
             cores to be assigned to each worker. Oversubscription is possible
             by setting cores_per_worker < 1.0. Default=1

        worker_debug : Bool
             Enables worker debug logging.

        logdir : str
             Parsl log directory paths. Logs and temp files go here. Default: '.'

        logging_level : int
             Logging level as defined in the logging module. Default: logging.INFO (20)

        endpoint_id : str
             Identity string that identifies the endpoint to the broker

        suppress_failure : Bool
             When set to True, the interchange will attempt to suppress failures. Default: False
        """

        self.logdir = logdir
        os.makedirs(self.logdir, exist_ok=True)

        global logger
        logger = set_file_logger(os.path.join(self.logdir, 'interchange.log'),
                                 name="interchange",
                                 level=logging_level,
                                 max_bytes=log_max_bytes,
                                 backup_count=log_backup_count)

        logger.info("logger location {}, logger filesize: {}, logger backup count: {}".format(logger.handlers,
                                                                                              log_max_bytes,
                                                                                              log_backup_count))

        logger.info("Initializing Interchange process with Endpoint ID: {}".format(endpoint_id))

        #
        self.max_workers_per_node = max_workers_per_node
        self.mem_per_worker = mem_per_worker
        self.cores_per_worker = cores_per_worker
        self.prefetch_capacity = prefetch_capacity
        self.scheduler_mode = scheduler_mode
        self.container_type = container_type
        self.log_max_bytes = log_max_bytes
        self.log_backup_count = log_backup_count
        self.working_dir = working_dir
        self.provider = provider
        self.worker_debug = worker_debug
        self.worker_mode = worker_mode
        self.scaling_enabled = scaling_enabled
        #

        self.strategy = strategy
        self.client_address = client_address
        self.interchange_address = interchange_address
        self.suppress_failure = suppress_failure

        self.poll_period = poll_period
        self.heartbeat_period = heartbeat_period
        self.heartbeat_threshold = heartbeat_threshold
        # initalize the last heartbeat time to start the loop
        self.last_heartbeat = time.time()

        self.serializer = FuncXSerializer()
        logger.info("Attempting connection to forwarder at {} on ports: {},{},{}".format(
            client_address, client_ports[0], client_ports[1], client_ports[2]))
        self.context = zmq.Context()
        self.task_incoming = self.context.socket(zmq.DEALER)
        self.task_incoming.set_hwm(0)
        self.task_incoming.RCVTIMEO = 10  # in milliseconds
        logger.info("Task incoming on tcp://{}:{}".format(client_address, client_ports[0]))
        self.task_incoming.connect("tcp://{}:{}".format(client_address, client_ports[0]))

        self.results_outgoing = self.context.socket(zmq.DEALER)
        self.results_outgoing.set_hwm(0)
        logger.info("Results outgoing on tcp://{}:{}".format(client_address, client_ports[1]))
        self.results_outgoing.connect("tcp://{}:{}".format(client_address, client_ports[1]))

        self.command_channel = self.context.socket(zmq.DEALER)
        self.command_channel.RCVTIMEO = 1000  # in milliseconds
        # self.command_channel.set_hwm(0)
        logger.info("Command channel on tcp://{}:{}".format(client_address, client_ports[2]))
        self.command_channel.connect("tcp://{}:{}".format(client_address, client_ports[2]))
        logger.info("Connected to forwarder")

        self.pending_task_queue = {}
        self.containers = {}
        self.total_pending_task_count = 0
        self.fxs = FuncXClient(funcx_service_address=funcx_service_address)

        logger.info("Interchange address is {}".format(self.interchange_address))
        self.worker_ports = worker_ports
        self.worker_port_range = worker_port_range

        self.task_outgoing = self.context.socket(zmq.ROUTER)
        self.task_outgoing.set_hwm(0)
        self.results_incoming = self.context.socket(zmq.ROUTER)
        self.results_incoming.set_hwm(0)

        self.endpoint_id = endpoint_id
        if self.worker_ports:
            self.worker_task_port = self.worker_ports[0]
            self.worker_result_port = self.worker_ports[1]

            self.task_outgoing.bind("tcp://*:{}".format(self.worker_task_port))
            self.results_incoming.bind("tcp://*:{}".format(self.worker_result_port))

        else:
            self.worker_task_port = self.task_outgoing.bind_to_random_port('tcp://*',
                                                                           min_port=worker_port_range[0],
                                                                           max_port=worker_port_range[1], max_tries=100)
            self.worker_result_port = self.results_incoming.bind_to_random_port('tcp://*',
                                                                                min_port=worker_port_range[0],
                                                                                max_port=worker_port_range[1], max_tries=100)

        logger.info("Bound to ports {},{} for incoming worker connections".format(
            self.worker_task_port, self.worker_result_port))

        self._ready_manager_queue = {}

        self.blocks = {}  # type: Dict[str, str]
        self.block_id_map = {}
        self.launch_cmd = launch_cmd
        self.last_core_hr_counter = 0
        if not launch_cmd:
            self.launch_cmd = ("funcx-manager {debug} {max_workers} "
                               "-c {cores_per_worker} "
                               "--poll {poll_period} "
                               "--task_url={task_url} "
                               "--result_url={result_url} "
                               "--logdir={logdir} "
                               "--block_id={{block_id}} "
                               "--hb_period={heartbeat_period} "
                               "--hb_threshold={heartbeat_threshold} "
                               "--worker_mode={worker_mode} "
                               "--scheduler_mode={scheduler_mode} "
                               "--log_max_bytes={log_max_bytes} "
                               "--log_backup_count={log_backup_count} "
                               "--worker_type={{worker_type}} ")

        self.current_platform = {'parsl_v': PARSL_VERSION,
                                 'python_v': "{}.{}.{}".format(sys.version_info.major,
                                                               sys.version_info.minor,
                                                               sys.version_info.micro),
                                 'os': platform.system(),
                                 'hname': platform.node(),
                                 'dir': os.getcwd()}

        logger.info("Platform info: {}".format(self.current_platform))
        self._block_counter = 0
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
        working_dir = self.working_dir
        if self.working_dir is None:
            working_dir = os.path.join(self.logdir, "worker_logs")
        logger.info("Setting working_dir: {}".format(working_dir))

        self.provider.script_dir = working_dir
        if hasattr(self.provider, 'channel'):
            self.provider.channel.script_dir = os.path.join(working_dir, 'submit_scripts')
            self.provider.channel.makedirs(self.provider.channel.script_dir, exist_ok=True)
            os.makedirs(self.provider.script_dir, exist_ok=True)

        debug_opts = "--debug" if self.worker_debug else ""
        max_workers = "" if self.max_workers_per_node == float('inf') \
                      else "--max_workers={}".format(self.max_workers_per_node)

        worker_task_url = f"tcp://{self.interchange_address}:{self.worker_task_port}"
        worker_result_url = f"tcp://{self.interchange_address}:{self.worker_result_port}"

        l_cmd = self.launch_cmd.format(debug=debug_opts,
                                       max_workers=max_workers,
                                       cores_per_worker=self.cores_per_worker,
                                       # mem_per_worker=self.mem_per_worker,
                                       prefetch_capacity=self.prefetch_capacity,
                                       task_url=worker_task_url,
                                       result_url=worker_result_url,
                                       nodes_per_block=self.provider.nodes_per_block,
                                       heartbeat_period=self.heartbeat_period,
                                       heartbeat_threshold=self.heartbeat_threshold,
                                       poll_period=self.poll_period,
                                       worker_mode=self.worker_mode,
                                       scheduler_mode=self.scheduler_mode,
                                       logdir=working_dir,
                                       log_max_bytes=self.log_max_bytes,
                                       log_backup_count=self.log_backup_count)

        self.launch_cmd = l_cmd
        logger.info("Launch command: {}".format(self.launch_cmd))

        if self.scaling_enabled:
            logger.info("Scaling ...")
            self.scale_out(self.provider.init_blocks)

    def get_tasks(self, count):
        """ Obtains a batch of tasks from the internal pending_task_queue

        Parameters
        ----------
        count: int
            Count of tasks to get from the queue

        Returns
        -------
        List of upto count tasks. May return fewer than count down to an empty list
            eg. [{'task_id':<x>, 'buffer':<buf>} ... ]
        """
        tasks = []
        for i in range(0, count):
            try:
                x = self.pending_task_queue.get(block=False)
            except queue.Empty:
                break
            else:
                tasks.append(x)

        return tasks

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
        poller = zmq.Poller()
        poller.register(self.task_incoming, zmq.POLLIN)

        while not kill_event.is_set():
            # We are no longer doing heartbeats on the task side.
            try:
                raw_msg = self.task_incoming.recv()
                self.last_heartbeat = time.time()
            except zmq.Again:
                # We just timed out while attempting to receive
                logger.debug("[TASK_PULL_THREAD] {} tasks in internal queue".format(self.total_pending_task_count))
                continue

            try:
                msg = Message.unpack(raw_msg)
                logger.debug("[TASK_PULL_THREAD] received Message/Heartbeat? on task queue")
            except Exception:
                logger.exception("Failed to unpack message")
                pass

            if msg == 'STOP':
                # TODO: Yadu. This should be replaced by a proper MessageType
                kill_event.set()
                break
            elif isinstance(msg, Heartbeat):
                logger.debug("Got heartbeat")
            else:
                logger.info("[TASK_PULL_THREAD] Received task:{}".format(msg))
                local_container = self.get_container(msg.container_id)
                msg.set_local_container(local_container)
                if local_container not in self.pending_task_queue:
                    self.pending_task_queue[local_container] = queue.Queue(maxsize=10 ** 6)

                # We pass the raw message along
                self.pending_task_queue[local_container].put({'task_id': msg.task_id,
                                                              'container_id': msg.container_id,
                                                              'local_container': local_container,
                                                              'raw_buffer': raw_msg})
                self.total_pending_task_count += 1
                self.task_status_deltas[msg.task_id] = TaskStatusCode.WAITING_FOR_NODES
                logger.debug(f"[TASK_PULL_THREAD] task {msg.task_id} is now WAITING_FOR_NODES")
                logger.debug("[TASK_PULL_THREAD] pending task count: {}".format(self.total_pending_task_count))
                task_counter += 1
                logger.debug("[TASK_PULL_THREAD] Fetched task:{}".format(task_counter))

    def get_container(self, container_uuid):
        """ Get the container image location if it is not known to the interchange"""
        if container_uuid not in self.containers:
            if container_uuid == 'RAW' or not container_uuid:
                self.containers[container_uuid] = 'RAW'
            else:
                try:
                    container = self.fxs.get_container(container_uuid, self.container_type)
                except Exception:
                    logger.exception("[FETCH_CONTAINER] Unable to resolve container location")
                    self.containers[container_uuid] = 'RAW'
                else:
                    logger.info("[FETCH_CONTAINER] Got container info: {}".format(container))
                    self.containers[container_uuid] = container.get('location', 'RAW')
        return self.containers[container_uuid]

    def get_total_tasks_outstanding(self):
        """ Get the outstanding tasks in total
        """
        outstanding = {}
        for task_type in self.pending_task_queue:
            outstanding[task_type] = outstanding.get(task_type, 0) + self.pending_task_queue[task_type].qsize()
        for manager in self._ready_manager_queue:
            for task_type in self._ready_manager_queue[manager]['tasks']:
                outstanding[task_type] = outstanding.get(task_type, 0) + len(self._ready_manager_queue[manager]['tasks'][task_type])
        return outstanding

    def get_total_live_workers(self):
        """ Get the total active workers
        """
        active = 0
        for manager in self._ready_manager_queue:
            if self._ready_manager_queue[manager]['active']:
                active += self._ready_manager_queue[manager]['max_worker_count']
        return active

    def get_outstanding_breakdown(self):
        """ Get outstanding breakdown per manager and in the interchange queues

        Returns
        -------
        List of status for online elements
        [ (element, tasks_pending, status) ... ]
        """

        pending_on_interchange = self.total_pending_task_count
        # Reporting pending on interchange is a deviation from Parsl
        reply = [('interchange', pending_on_interchange, True)]
        for manager in self._ready_manager_queue:
            resp = (manager.decode('utf-8'),
                    sum([len(tids) for tids in self._ready_manager_queue[manager]['tasks'].values()]),
                    self._ready_manager_queue[manager]['active'])
            reply.append(resp)
        return reply

    def _hold_block(self, block_id):
        """ Sends hold command to all managers which are in a specific block

        Parameters
        ----------
        block_id : str
             Block identifier of the block to be put on hold
        """
        for manager in self._ready_manager_queue:
            if self._ready_manager_queue[manager]['active'] and \
               self._ready_manager_queue[manager]['block_id'] == block_id:
                logger.debug("[HOLD_BLOCK]: Sending hold to manager: {}".format(manager))
                self.hold_manager(manager)

    def hold_manager(self, manager):
        """ Put manager on hold
        Parameters
        ----------

        manager : str
          Manager id to be put on hold while being killed
        """
        if manager in self._ready_manager_queue:
            self._ready_manager_queue[manager]['active'] = False

    def _status_report_loop(self, kill_event, status_report_queue: queue.Queue):
        logger.debug("[STATUS] Status reporting loop starting")

        while not kill_event.is_set():
            logger.info(f"Endpoint id : {self.endpoint_id}, {type(self.endpoint_id)}")
            msg = EPStatusReport(
                self.endpoint_id,
                self.get_status_report(),
                self.task_status_deltas
            )
            logger.debug("[STATUS] Sending status report to forwarder, and clearing task deltas.")
            status_report_queue.put(msg.pack())
            self.task_status_deltas.clear()
            time.sleep(self.heartbeat_period)

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
                buffer = self.command_channel.recv()
                logger.debug(f"[COMMAND] Received command request {buffer}")
                command = Message.unpack(buffer)
                if command.type not in COMMAND_TYPES:
                    logger.error("Received incorrect message type on command channel")
                    self.command_channel.send(bytes())
                    continue

                if command.type is MessageType.HEARTBEAT_REQ:
                    logger.info("[COMMAND] Received synchonous HEARTBEAT_REQ from hub")
                    logger.info(f"[COMMAND] Replying with Heartbeat({self.endpoint_id})")
                    reply = Heartbeat(self.endpoint_id)

                logger.debug("[COMMAND] Reply: {}".format(reply))
                self.command_channel.send(reply.pack())

            except zmq.Again:
                logger.debug("[COMMAND] is alive")
                continue

    def stop(self):
        """Prepare the interchange for shutdown"""
        self._kill_event.set()

        self._task_puller_thread.join()
        self._command_thread.join()

    def start(self, poll_period=None):
        """ Start the Interchange

        Parameters:
        ----------
        poll_period : int
           poll_period in milliseconds
        """
        logger.info("Incoming ports bound")

        if poll_period is None:
            poll_period = self.poll_period

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

        try:
            logger.info("Starting strategy.")
            self.strategy.start(self)
        except RuntimeError:
            # This is raised when re-registering an endpoint as strategy already exists
            logger.exception("Failed to start strategy.")

        poller = zmq.Poller()
        # poller.register(self.task_incoming, zmq.POLLIN)
        poller.register(self.task_outgoing, zmq.POLLIN)
        poller.register(self.results_incoming, zmq.POLLIN)

        # These are managers which we should examine in an iteration
        # for scheduling a job (or maybe any other attention?).
        # Anything altering the state of the manager should add it
        # onto this list.
        interesting_managers = set()

        while not self._kill_event.is_set():
            self.socks = dict(poller.poll(timeout=poll_period))

            # Listen for requests for work
            if self.task_outgoing in self.socks and self.socks[self.task_outgoing] == zmq.POLLIN:
                logger.debug("[MAIN] starting task_outgoing section")
                message = self.task_outgoing.recv_multipart()
                manager = message[0]

                if manager not in self._ready_manager_queue:
                    reg_flag = False

                    try:
                        msg = json.loads(message[1].decode('utf-8'))
                        reg_flag = True
                    except Exception:
                        logger.warning("[MAIN] Got a non-json registration message from manager:{}".format(
                            manager))
                        logger.debug("[MAIN] Message :\n{}\n".format(message))

                    # By default we set up to ignore bad nodes/registration messages.
                    self._ready_manager_queue[manager] = {'last': time.time(),
                                                          'reg_time': time.time(),
                                                          'free_capacity': {'total_workers': 0},
                                                          'max_worker_count': 0,
                                                          'active': True,
                                                          'tasks': collections.defaultdict(set),
                                                          'total_tasks': 0}
                    if reg_flag is True:
                        interesting_managers.add(manager)
                        logger.info("[MAIN] Adding manager: {} to ready queue".format(manager))
                        self._ready_manager_queue[manager].update(msg)
                        logger.info("[MAIN] Registration info for manager {}: {}".format(manager, msg))

                        if (msg['python_v'].rsplit(".", 1)[0] != self.current_platform['python_v'].rsplit(".", 1)[0] or
                            msg['parsl_v'] != self.current_platform['parsl_v']):
                            logger.warn("[MAIN] Manager {} has incompatible version info with the interchange".format(manager))

                            if self.suppress_failure is False:
                                logger.debug("Setting kill event")
                                self._kill_event.set()
                                e = ManagerLost(manager)
                                result_package = {'task_id': -1,
                                                  'exception': self.serializer.serialize(e)}
                                pkl_package = pickle.dumps(result_package)
                                self.results_outgoing.send(pickle.dumps([pkl_package]))
                                logger.warning("[MAIN] Sent failure reports, unregistering manager")
                            else:
                                logger.debug("[MAIN] Suppressing shutdown due to version incompatibility")

                    else:
                        # Registration has failed.
                        if self.suppress_failure is False:
                            logger.debug("Setting kill event for bad manager")
                            self._kill_event.set()
                            e = BadRegistration(manager, critical=True)
                            result_package = {'task_id': -1,
                                              'exception': self.serializer.serialize(e)}
                            pkl_package = pickle.dumps(result_package)
                            self.results_outgoing.send(pickle.dumps([pkl_package]))
                        else:
                            logger.debug("[MAIN] Suppressing bad registration from manager:{}".format(
                                manager))

                else:
                    self._ready_manager_queue[manager]['last'] = time.time()
                    if message[1] == b'HEARTBEAT':
                        logger.debug("[MAIN] Manager {} sends heartbeat".format(manager))
                        self.task_outgoing.send_multipart([manager, b'', PKL_HEARTBEAT_CODE])
                    else:
                        manager_adv = pickle.loads(message[1])
                        logger.debug("[MAIN] Manager {} requested {}".format(manager, manager_adv))
                        self._ready_manager_queue[manager]['free_capacity'].update(manager_adv)
                        self._ready_manager_queue[manager]['free_capacity']['total_workers'] = sum(manager_adv.values())
                        interesting_managers.add(manager)

            # If we had received any requests, check if there are tasks that could be passed

            logger.debug("[MAIN] Managers count (total/interesting): {}/{}".format(
                len(self._ready_manager_queue),
                len(interesting_managers)))

            task_dispatch, dispatched_task = naive_interchange_task_dispatch(interesting_managers,
                                                                             self.pending_task_queue,
                                                                             self._ready_manager_queue,
                                                                             scheduler_mode=self.scheduler_mode)
            self.total_pending_task_count -= dispatched_task

            for manager in task_dispatch:
                tasks = task_dispatch[manager]
                if tasks:
                    logger.info("[MAIN] Sending task message {} to manager {}".format(tasks, manager))
                    serializd_raw_tasks_buffer = pickle.dumps(tasks)
                    # self.task_outgoing.send_multipart([manager, b'', pickle.dumps(tasks)])
                    self.task_outgoing.send_multipart([manager, b'', serializd_raw_tasks_buffer])

                    for task in tasks:
                        task_id = task["task_id"]
                        logger.debug(f"[MAIN] Task {task_id} is now WAITING_FOR_LAUNCH")
                        self.task_status_deltas[task_id] = TaskStatusCode.WAITING_FOR_LAUNCH

            # Receive any results and forward to client
            if self.results_incoming in self.socks and self.socks[self.results_incoming] == zmq.POLLIN:
                logger.debug("[MAIN] entering results_incoming section")
                manager, *b_messages = self.results_incoming.recv_multipart()
                if manager not in self._ready_manager_queue:
                    logger.warning("[MAIN] Received a result from a un-registered manager: {}".format(manager))
                else:
                    # We expect the batch of messages to be (optionally) a task status update message
                    # followed by 0 or more task results
                    try:
                        logger.debug("[MAIN] Trying to unpack ")
                        manager_report = Message.unpack(b_messages[0])
                        if manager_report.task_statuses:
                            logger.info(f"[MAIN] Got manager status report: {manager_report.task_statuses}")
                        self.task_status_deltas.update(manager_report.task_statuses)
                        self.task_outgoing.send_multipart([manager, b'', PKL_HEARTBEAT_CODE])
                        b_messages = b_messages[1:]
                        self._ready_manager_queue[manager]['last'] = time.time()
                    except Exception:
                        pass
                    if len(b_messages):
                        logger.info("[MAIN] Got {} result items in batch".format(len(b_messages)))
                    for b_message in b_messages:
                        r = pickle.loads(b_message)
                        logger.debug("[MAIN] Received result for task {} from {}".format(r, manager))
                        task_type = self.containers[r['container_id']]
                        if r['task_id'] in self.task_status_deltas:
                            del self.task_status_deltas[r['task_id']]
                        self._ready_manager_queue[manager]['tasks'][task_type].remove(r['task_id'])
                    self._ready_manager_queue[manager]['total_tasks'] -= len(b_messages)

                    # TODO: handle this with a Task message or something?
                    # previously used this; switched to mono-message, self.results_outgoing.send_multipart(b_messages)
                    self.results_outgoing.send(pickle.dumps(b_messages))
                    logger.debug("[MAIN] Current tasks: {}".format(self._ready_manager_queue[manager]['tasks']))
                logger.debug("[MAIN] leaving results_incoming section")

            # Send status reports from this main thread to avoid thread-safety on zmq sockets
            try:
                packed_status_report = status_report_queue.get(block=False)
                logger.debug(f"[MAIN] forwarding status report queue: {packed_status_report}")
                self.results_outgoing.send(packed_status_report)
            except queue.Empty:
                pass

            # logger.debug("[MAIN] entering bad_managers section")
            bad_managers = [manager for manager in self._ready_manager_queue if
                            time.time() - self._ready_manager_queue[manager]['last'] > self.heartbeat_threshold]
            bad_manager_msgs = []
            for manager in bad_managers:
                logger.debug("[MAIN] Last: {} Current: {}".format(self._ready_manager_queue[manager]['last'], time.time()))
                logger.warning("[MAIN] Too many heartbeats missed for manager {}".format(manager))
                e = ManagerLost(manager)
                for task_type in self._ready_manager_queue[manager]['tasks']:
                    for tid in self._ready_manager_queue[manager]['tasks'][task_type]:
                        try:
                            raise ManagerLost(manager)
                        except Exception:
                            result_package = {'task_id': tid, 'exception': self.serializer.serialize(RemoteExceptionWrapper(*sys.exc_info()))}
                            pkl_package = pickle.dumps(result_package)
                            bad_manager_msgs.append(pkl_package)
                logger.warning("[MAIN] Sent failure reports, unregistering manager {}".format(manager))
                self._ready_manager_queue.pop(manager, 'None')
                if manager in interesting_managers:
                    interesting_managers.remove(manager)
            if bad_manager_msgs:
                self.results_outgoing.send(pickle.dumps(bad_manager_msgs))
            logger.debug("[MAIN] ending one main loop iteration")

            if self._status_request.is_set():
                logger.info("status request response")
                result_package = self.get_status_report()
                pkl_package = pickle.dumps(result_package)
                self.results_outgoing.send(pkl_package)
                logger.info("[MAIN] Sent info response")
                self._status_request.clear()

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
                                   'worker_mode': self.worker_mode,
                                   'scheduler_mode': self.scheduler_mode,
                                   'scaling_enabled': self.scaling_enabled,
                                   'mem_per_worker': self.mem_per_worker,
                                   'cores_per_worker': self.cores_per_worker,
                                   'prefetch_capacity': self.prefetch_capacity,
                                   'max_blocks': self.provider.max_blocks,
                                   'min_blocks': self.provider.min_blocks,
                                   'max_workers_per_node': self.max_workers_per_node,
                                   'nodes_per_block': self.provider.nodes_per_block
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
            if self.provider:
                self._block_counter += 1
                external_block_id = str(self._block_counter)
                if not task_type and self.scheduler_mode == 'hard':
                    launch_cmd = self.launch_cmd.format(block_id=external_block_id, worker_type='RAW')
                else:
                    launch_cmd = self.launch_cmd.format(block_id=external_block_id, worker_type=task_type)
                if not task_type:
                    internal_block = self.provider.submit(launch_cmd, 1)
                else:
                    internal_block = self.provider.submit(launch_cmd, 1, task_type)
                logger.debug("Launched block {}->{}".format(external_block_id, internal_block))
                if not internal_block:
                    raise(ScalingFailed(self.config.provider.label,
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
            if self.scaling_enabled and self.provider:
                to_kill, r = self.provider.cancel(blocks, task_type)
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

        if self.scaling_enabled and self.provider:
            r = self.provider.cancel(to_kill)

        return r

    def provider_status(self):
        """ Get status of all blocks from the provider
        """
        status = []
        if self.provider:
            logger.debug("[MAIN] Getting the status of {} blocks.".format(list(self.blocks.values())))
            status = self.provider.status(list(self.blocks.values()))
            logger.debug("[MAIN] The status is {}".format(status))

        return status


def starter(comm_q, *args, **kwargs):
    """Start the interchange process

    The executor is expected to call this function. The args, kwargs match that of the Interchange.__init__
    """
    # logger = multiprocessing.get_logger()
    ic = Interchange(*args, **kwargs)
    comm_q.put((ic.worker_task_port,
                ic.worker_result_port))
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
    parser.add_argument("-p", "--poll_period",
                        help="REQUIRED: poll period used for main thread")
    parser.add_argument("--worker_ports", default=None,
                        help="OPTIONAL, pair of workers ports to listen on, eg --worker_ports=50001,50005")
    parser.add_argument("--suppress_failure", action='store_true',
                        help="Enables suppression of failures")
    parser.add_argument("--endpoint_id", default=None,
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
    optionals['config'] = args.config

    if args.debug:
        optionals['logging_level'] = logging.DEBUG
    if args.worker_ports:
        optionals['worker_ports'] = [int(i) for i in args.worker_ports.split(',')]
    if args.worker_port_range:
        optionals['worker_port_range'] = [int(i) for i in args.worker_port_range.split(',')]

    with daemon.DaemonContext():
        ic = Interchange(**optionals)
        ic.start()
