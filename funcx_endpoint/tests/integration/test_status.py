import argparse
import time

import funcx
from funcx import set_stream_logger
from funcx.sdk.client import FuncXClient
from funcx.serialize import FuncXSerializer

fxs = FuncXSerializer()

set_stream_logger()


def sum_yadu_new01(event):
    return sum(event)


"""
@funcx.register(description="...")
def sum_yadu_new01(event):
    return sum(event)
"""


def test(fxc, ep_id):
    fn_uuid = fxc.register_function(sum_yadu_new01,
                                    description="New sum function defined without string spec")
    print("FN_UUID : ", fn_uuid)

    task_id = fxc.run([1, 2, 3, 9001], endpoint_id=ep_id, function_id=fn_uuid)
    r = fxc.get_task_status(task_id)
    print("Got from status :", r)


def platinfo():
    import platform
    return platform.uname()


def div_by_zero(x):
    return x / 0


def test2(fxc, ep_id):
    fn_uuid = fxc.register_function(platinfo,
                                    description="Get platform info")
    print("FN_UUID : ", fn_uuid)
    task_id = fxc.run(endpoint_id=ep_id, function_id=fn_uuid)

    time.sleep(2)
    r = fxc.get_task_status(task_id)
    if 'details' in r:
        s_buf = r['details']['result']
        print("Result : ", fxs.deserialize(s_buf))
    else:
        print("Got from status :", r)


def test3(fxc, ep_id):
    fn_uuid = fxc.register_function(div_by_zero,
                                    description="Div by zero")
    print("FN_UUID : ", fn_uuid)
    task_id = fxc.run(1099, endpoint_id=ep_id, function_id=fn_uuid)

    time.sleep(1)
    _ = fxc.get_task_status(task_id)

    print("Trying get_result")
    _ = fxc.get_result(task_id)


def test4(fxc, ep_id):
    fn_uuid = fxc.register_function(platinfo,
                                    description="Get platform info")
    print("FN_UUID : ", fn_uuid)
    task_id = fxc.run(endpoint_id=ep_id, function_id=fn_uuid)

    time.sleep(2)
    r = fxc.get_result(task_id)
    print("Got result : ", r)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("-e", "--endpoint", required=True)
    args = parser.parse_args()

    print("FuncX version : ", funcx.sdk.__version__)
    fxc = FuncXClient()
    # test(fxc, args.endpoint)
    # test2(fxc, args.endpoint)
    test3(fxc, args.endpoint)
    test4(fxc, args.endpoint)
