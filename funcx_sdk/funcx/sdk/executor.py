import time
import threading
import os
import uuid
import sys
from concurrent.futures import Future
import concurrent
import logging
import asyncio
import websockets
import json
import dill
from websockets.exceptions import InvalidHandshake
import multiprocessing as mp
import atexit

from funcx.sdk.asynchronous.ws_polling_task import WebSocketPollingTask

logger = logging.getLogger(__name__)


class FuncXExecutor(concurrent.futures.Executor):
    """ An executor
    """

    def __init__(self, funcx_client,
                 results_ws_uri: str = 'ws://localhost:6000',
                 label: str = 'FuncXExecutor'):
        """
        Parameters
        ==========

        funcx_client : client object
            Instance of FuncXClient to be used by the executor

        results_ws_uri : str
            Web sockets URI for the results

        label : str
            Optional string label to name the executor.
            Default: 'FuncXExecutor'
        """

        self.funcx_client = funcx_client
        self.results_ws_uri = results_ws_uri
        self.label = label
        self._tasks = {}
        self._function_registry = {}
        self._function_future_map = {}
        self.task_group_id = self.funcx_client.session_task_group_id # we need to associate all batch launches with this id

        self.poller_thread = None
        atexit.register(self.shutdown)

    def submit(self, function, *args, endpoint_id=None, container_uuid=None, **kwargs):
        """Initiate an invocation

        Parameters
        ----------
        function : Function/Callable
            Function / Callable to execute

        *args : Any
            Args as specified by the function signature

        endpoint_id : uuid str
            Endpoint UUID string. Required

        **kwargs : Any
            Arbitrary kwargs

        Returns
        -------
        Future : concurrent.futures.Future
            A future object
        """

        if function not in self._function_registry:
            # Please note that this is a partial implementation, not all function registration
            # options are fleshed out here.
            logger.debug("Function:{function} is not registered. Registering")
            function_uuid = self.funcx_client.register_function(function,
                                                                function_name=function.__name__,
                                                                container_uuid=container_uuid)
            self._function_registry[function] = function_uuid
            logger.debug(f"Function registered with id:{function_uuid}")
        assert endpoint_id is not None, "endpoint_id key-word argument must be set"

        batch = self.funcx_client.create_batch(task_group_id=self.task_group_id)
        batch.add(*args,
                  endpoint_id=endpoint_id,
                  function_id=self._function_registry[function],
                  **kwargs)
        r = self.funcx_client.batch_run(batch)
        logger.debug(f"Batch submitted to task_group: {self.task_group_id}")

        task_uuid = r[0]
        logger.debug(f'Waiting on results for task ID: {task_uuid}')
        # There's a potential for a race-condition here where the result reaches
        # the poller before the future is added to the future_map
        self._function_future_map[task_uuid] = Future()

        res = self._function_future_map[task_uuid]

        if not self.poller_thread or self.poller_thread.ws_handler.closed:
            self.poller_thread = ExecutorPollerThread(self.funcx_client, self._function_future_map, self.results_ws_uri, self.task_group_id)
        return res

    def shutdown(self):
        if self.poller_thread:
            self.poller_thread.shutdown()
        logger.debug(f"Executor:{self.label} shutting down")


class ExecutorPollerThread():
    """ An executor
    """

    def __init__(self, funcx_client, _function_future_map, results_ws_uri, task_group_id):
        """
        Parameters
        ==========

        funcx_client : client object
            Instance of FuncXClient to be used by the executor

        results_ws_uri : str
            Web sockets URI for the results
        """

        self.funcx_client = funcx_client
        self.results_ws_uri = results_ws_uri
        self._function_future_map = _function_future_map
        self.task_group_id = task_group_id

        self.start()

    def start(self):
        """ Start the result polling thread
        """

        # Currently we need to put the batch id's we launch into this queue
        # to tell the web_socket_poller to listen on them. Later we'll associate

        eventloop = asyncio.new_event_loop()
        self.eventloop = eventloop
        self.ws_handler = WebSocketPollingTask(self.funcx_client, eventloop, self.task_group_id,
                                               self.results_ws_uri, auto_start=False)
        self.thread = threading.Thread(target=self.event_loop_thread,
                                       args=(eventloop, ))
        self.thread.start()

        logger.debug("Started web_socket_poller thread")

    def event_loop_thread(self, eventloop):
        asyncio.set_event_loop(eventloop)
        eventloop.run_until_complete(self.web_socket_poller())

    @asyncio.coroutine
    async def web_socket_poller(self):
        await self.ws_handler.init_ws(start_message_handlers=False)
        await self.ws_handler.handle_incoming(self._function_future_map, auto_close=True)

    def shutdown(self):
        ws = self.ws_handler.ws
        if ws:
            ws_close_future = asyncio.run_coroutine_threadsafe(ws.close(), self.eventloop)
            ws_close_future.result()


def double(x):
    return x * 2


if __name__ == '__main__':

    import argparse
    from funcx import FuncXClient
    from funcx import set_stream_logger
    import time

    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--service_url", default='http://localhost:5000/v2',
                        help="URL at which the funcx-web-service is hosted")
    parser.add_argument("-e", "--endpoint_id", required=True,
                        help="Target endpoint to send functions to")
    parser.add_argument("-d", "--debug", action='store_true',
                        help="Count of apps to launch")
    args = parser.parse_args()

    endpoint_id = args.endpoint_id

    # set_stream_logger()
    fx = FuncXExecutor(FuncXClient(funcx_service_address=args.service_url))

    print("In main")
    endpoint_id = args.endpoint_id
    future = fx.submit(double, 5, endpoint_id=endpoint_id)
    print("Got future back : ", future)

    for i in range(5):
        time.sleep(0.2)
        # Non-blocking check whether future is done
        print("Is the future done? :", future.done())

    print("Blocking for result")
    x = future.result()     # <--- This is a blocking call
    print("Result : ", x)

    # fx.shutdown()
