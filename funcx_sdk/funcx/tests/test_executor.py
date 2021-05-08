from funcx import FuncXClient
from funcx.sdk.executor import FuncXExecutor
from funcx import set_stream_logger
import time
import argparse
import random


def double(x):
    return x * 2


def test_simple(fx, endpoint_id):

    x = random.randint(0, 100)
    fut = fx.submit(double, x, endpoint_id=endpoint_id)

    assert fut.result() == x * 2, "Got wrong answer"


def test_loop(fx, endpoint_id, count=10):

    futures = []
    for i in range(count):
        future = fx.submit(double, i, endpoint_id=endpoint_id)
        futures.append(future)

    for fu in futures:
        print(fu.result())


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--service_url", default='http://localhost:5000/v2',
                        help="URL at which the funcx-web-service is hosted")
    parser.add_argument("-e", "--endpoint_id", required=True,
                        help="Target endpoint to send functions to")
    parser.add_argument("-c", "--count", default="10",
                        help="Number of tasks to launch")
    parser.add_argument("-d", "--debug", action='store_true',
                        help="Count of apps to launch")
    args = parser.parse_args()

    # set_stream_logger()
    fx = FuncXExecutor(FuncXClient(funcx_service_address=args.service_url))

    print("Running simple test")
    test_simple(fx, args.endpoint_id)
    print("Complete")

    print(f"Running a test with a for loop of {args.count} tasks")
    test_loop(fx, args.endpoint_id, count=int(args.count))
