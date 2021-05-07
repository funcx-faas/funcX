import logging
from functools import partial
import uuid
import os
import queue
from multiprocessing import Queue

from multiprocessing import Process
from funcx import set_file_logger


def double(x):
    return x * 2


def failer(x):
    return x / 0


class Forwarder(Process):
    """ Forwards tasks/results between the executor and the queues

        Tasks_Q  Results_Q
           |     ^
           |     |
           V     |
          Executors

    Todo : We need to clarify what constitutes a task that comes down
    the task pipe. Does it already have the code fragment? Or does that need to be sorted
    out from some DB ?
    """

    def __init__(self, task_q, result_q, executor, endpoint_id,
                 logdir="forwarder", logging_level=logging.INFO):
        """
        Params:
             task_q : A queue object
                Any queue object that has get primitives. This must be a thread-safe queue.

             result_q : A queue object
                Any queue object that has put primitives. This must be a thread-safe queue.

             executor: Executor object
                Executor to which tasks are to be forwarded

             endpoint_id: str
                Usually a uuid4 as string that identifies the executor

             logdir: str
                Path to logdir

             logging_level : int
                Logging level as defined in the logging module. Default: logging.INFO (20)

        """
        super().__init__()
        self.logdir = logdir
        os.makedirs(self.logdir, exist_ok=True)

        global logger
        logger = set_file_logger(os.path.join(self.logdir, "forwarder.{}.log".format(endpoint_id)),
                                 level=logging_level)

        logger.info("Initializing forwarder for endpoint:{}".format(endpoint_id))
        self.task_q = task_q
        self.result_q = result_q
        self.executor = executor
        self.endpoint_id = endpoint_id
        self.internal_q = Queue()
        self.client_ports = None

    def handle_app_update(self, task_id, future):
        """ Triggered when the executor sees a task complete.

        This can be further optimized at the executor level, where we trigger this
        or a similar function when we see a results item inbound from the interchange.
        """
        logger.debug("[RESULTS] Updating result")
        try:
            res = future.result()
            self.result_q.put(task_id, res)
        except Exception:
            logger.debug("Task:{} failed".format(task_id))
            # Todo : Since we caught an exception, we should wrap it here, and send it
            # back onto the results queue.
        else:
            logger.debug("Task:{} succeeded".format(task_id))

    def run(self):
        """ Process entry point.
        """
        logger.info("[TASKS] Loop starting")
        logger.info("[TASKS] Executor: {}".format(self.executor))

        try:
            self.task_q.connect()
            self.result_q.connect()
        except Exception:
            logger.exception("Connecting to the queues have failed")

        self.executor.start()
        conn_info = self.executor.connection_info
        self.internal_q.put(conn_info)
        logger.info("[TASKS] Endpoint connection info: {}".format(conn_info))

        while True:
            try:
                task = self.task_q.get(timeout=10)
                logger.debug("[TASKS] Not doing {}".format(task))
            except queue.Empty:
                # This exception catching isn't very general,
                # Essentially any timeout exception should be caught and ignored
                logger.debug("[TASKS] Waiting for tasks")
                pass
            else:
                # TODO: We are piping down a mock task. This needs to be fixed.
                task_id = str(uuid.uuid4())
                args = [5]
                kwargs = {}
                fu = self.executor.submit(double, *args, **kwargs)
                fu.add_done_callback(partial(self.handle_app_update, task_id))

        logger.info("[TASKS] Terminating self due to user requested kill")
        return

    @property
    def connection_info(self):
        """Get the client ports to which the interchange must connect to
        """

        if not self.client_ports:
            self.client_ports = self.internal_q.get()

        return self.client_ports


def spawn_forwarder(address,
                    executor=None,
                    task_q=None,
                    result_q=None,
                    endpoint_id=uuid.uuid4(),
                    logging_level=logging.INFO):
    """ Spawns a forwarder and returns the forwarder process for tracking.

    Parameters
    ----------

    address : str
       IP Address to which the endpoint must connect

    executor : Executor object. Optional
       Executor object to be instantiated.

    task_q : Queue object
       Queue object matching funcx.queues.base.FuncxQueue interface

    logging_level : int
       Logging level as defined in the logging module. Default: logging.INFO (20)

    endpoint_id : uuid string
       Endpoint id for which the forwarder is being spawned.

    Returns:
         A Forwarder object
    """
    from funcx_endpoint.queues import RedisQueue
    from funcx_endpoint.executors import HighThroughputExecutor as HTEX
    from parsl.providers import LocalProvider
    from parsl.channels import LocalChannel

    task_q = RedisQueue('task', '127.0.0.1')
    result_q = RedisQueue('result', '127.0.0.1')

    if not executor:
        executor = HTEX(label='htex',
                        provider=LocalProvider(
                            channel=LocalChannel),
                        address=address)

    fw = Forwarder(task_q, result_q, executor,
                   "Endpoint_{}".format(endpoint_id),
                   logging_level=logging_level)
    fw.start()
    return fw


if __name__ == "__main__":

    pass
    # test()
