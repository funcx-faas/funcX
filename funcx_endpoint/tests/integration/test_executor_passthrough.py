from funcx_endpoint.executors.high_throughput.executor import HighThroughputExecutor
import logging
from funcx import set_file_logger
import uuid
from funcx.serialize import FuncXSerializer
from funcx_endpoint.executors.high_throughput.messages import Message, Task
import time
import pickle
from multiprocessing import Queue


def double(x):
    return x * 2


if __name__ == "__main__":

    results_queue = Queue()
    #    set_file_logger('executor.log', name='funcx_endpoint', level=logging.DEBUG)
    htex = HighThroughputExecutor(interchange_local=True,
                                  passthrough=True)

    htex.start(results_passthrough=results_queue)
    htex._start_remote_interchange_process()
    fx_serializer = FuncXSerializer()

    for i in range(10):
        task_id = str(uuid.uuid4())
        args = (i,)
        kwargs = {}

        fn_code = fx_serializer.serialize(double)
        ser_code = fx_serializer.pack_buffers([fn_code])
        ser_params = fx_serializer.pack_buffers([fx_serializer.serialize(args),
                                                 fx_serializer.serialize(kwargs)])

        payload = Task(task_id,
                       'RAW',
                       ser_code + ser_params)
        f = htex.submit_raw(payload.pack())
        time.sleep(0.5)

    for i in range(10):
        result_package = results_queue.get()
        # print("Result package : ", result_package)
        r = pickle.loads(result_package)
        result = fx_serializer.deserialize(r['result'])
        print(f"Result:{i}: {result}")

    print("All done")
