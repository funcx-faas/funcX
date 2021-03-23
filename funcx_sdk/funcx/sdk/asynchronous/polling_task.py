import asyncio
import logging
from asyncio import AbstractEventLoop, QueueEmpty

import dill
from retry import retry

from funcx.sdk.asynchronous.funcx_task import FuncXTask
from funcx.sdk.utils.throttling import MaxRequestsExceeded

logger = logging.getLogger(__name__)


class PollingTask:
    """
    Launches an asycnhio task for polling unresolved funcX tasks. Uses a work queue to
    record unresolved funcX Tasks. Pulls a group off and attempts to retrieve the result.
    If this attempt fails, put the values back on the queue for later attempt.
    """

    # This is the number of tasks to include in a batch status request. The larger it is,
    # the more efficient the polling operation is, but at some point the results
    # will be too large
    MAX_BATCH_SIZE = 100

    async def poll_funcx(self, queue: asyncio.Queue):
        """
        Asynchornous function to poll for results. Use batch requests to reduce overhead
        :param queue:
        :return:
        """
        while True:
            at_least_one_resolved = False

            batch_request = self.get_a_batch(queue)
            if batch_request:
                batch_result = self.fetch_batch(batch_request.keys())
                logger.debug("Batch result: %s", batch_result)

                for result_id in batch_result.keys():
                    if not batch_result[result_id]['pending']:
                        # Handle exception in called function
                        if batch_result[result_id]['status'] == 'failed':
                            eek = dill.loads(batch_result[result_id]['exception'].e_value)
                            batch_request[result_id].set_exception(eek)
                        else:
                            # Resolve the associated future
                            batch_request[result_id].set_result(batch_result[result_id]['result'])
                            at_least_one_resolved = True
                    else:
                        # Put back into the queue so we can try again
                        await queue.put(batch_request[result_id])

            if not at_least_one_resolved:
                await asyncio.sleep(10)

    def get_a_batch(self, queue: asyncio.Queue) -> dict:
        """
        Grab as many tasks from the queue as will fit into our batch request
        :param queue:
        :return:
        """
        batch_size = min(queue.qsize(), self.MAX_BATCH_SIZE)
        batch_request = {}
        for i in range(0, batch_size):
            try:
                t = queue.get_nowait()
                batch_request[t.task_id] = t
            except QueueEmpty:
                break
        return batch_request

    @retry(exceptions=MaxRequestsExceeded, tries=5, delay=5, logger=logger)
    def fetch_batch(self, id_list: list) -> dict:
        """
        Retry protected call to the client's batch status endpoint. If we fall foul
        of the API throttle back off and try again
        :param id_list:
        :return:
        """
        batch_result = self.fxc.get_batch_status([t for t in id_list])
        return batch_result

    def __init__(self, fxc, loop: AbstractEventLoop):
        """

        :param fxc: FuncXClient
            Client instance for requesting results
        :param loop: AbstractEventLoop
            Asynchio event loop to manage asynchronous calls
        """
        self.fxc = fxc
        self.loop = loop
        self.running_tasks = asyncio.Queue()
        self.poll_task = self.loop.create_task(self.poll_funcx(self.running_tasks))

    def put(self, task: FuncXTask):
        """
        Add a funcX task to the poller
        :param task: FuncXTask
            Task to be added
        """
        self.running_tasks.put_nowait(task)
