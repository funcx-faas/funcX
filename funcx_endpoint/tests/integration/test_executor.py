from funcx_endpoint.executors.high_throughput.executor import HighThroughputExecutor
import logging
from funcx import set_file_logger


def double(x):
    return x * 2


if __name__ == "__main__":

    #    set_file_logger('executor.log', name='funcx_endpoint', level=logging.DEBUG)
    htex = HighThroughputExecutor(interchange_local=True)
    htex.start()
    htex._start_remote_interchange_process()
    f = htex.submit(double, 2)
    print(f)
    print("Result : ", f.result())
