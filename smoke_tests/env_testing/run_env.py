from __future__ import annotations

import os
import sys
import time
from random import random
from uuid import UUID

from globus_compute_sdk import Client, Executor
from globus_sdk.services.auth.errors import AuthAPIError

TUTORIAL_EP_UUID = "4b116d3c-1703-4f8f-9f6f-39921e5864df"
WAIT_CLIENT_SEC = 8


def show_help(err_msg: str = None):
    if err_msg:
        print(err_msg)
    print(
        "Usage:\n  python3 run_env.py "
        + "(prod|staging|preview|sandbox|test|integration) [optional_func_uuid]\n"
        + "The environment keyword can be abbreviated as long as the first "
        "chars uniquely identify it.  ie. pro for production, sa for sandbox.\n\n"
        "If you specify an optional function UUID, that will be used.  Otherwise "
        "a new function that takes one argument and echos it as output is "
        "registered and used to test.\n\n"
        "Note providing existing FN_UUID yields KeyError TBD Fix"
    )
    sys.exit(1)


def echo_arg_tutorial_ep_testing(s):
    return f"Hello Compute user, your argument is {s}"


ENV_UNIQUE = {
    "preview": "pre",
    "production": "pro",
    "sandbox": "sa",
    "test": "t",
    "staging": "st",
    "integration": "i",
}


def get_env(arg: str) -> str | None:
    if arg:
        arg = arg.lower()
        for k, v in ENV_UNIQUE.items():
            if arg.startswith(v) and k.startswith(arg):
                return k
    return None


if __name__ == "__main__":
    fn_id = None
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        show_help()
    elif len(sys.argv) == 3:
        try:
            fn_id = UUID(sys.argv[2])
        except Exception:
            show_help(f"Invalid function UUID: {sys.argv[2]}")

    env_id = get_env(sys.argv[1])
    if env_id is None:
        show_help(f"Invalid/non-unique environment: {env_id}")

    os.environ["GLOBUS_SDK_ENVIRONMENT"] = env_id
    print(f"Testing on GLOBUS_SDK_ENVIRONMENT: ({env_id})")
    try:
        gcc = Client(environment=env_id)
    except AuthAPIError as e:
        print(
            "Encountered AuthAPIError, you probably need to delete/refresh"
            " your token for this environment (delete storage.db is quickest)"
        )
        print(e)
        sys.exit(1)

    if fn_id is None:
        print("Registering echo function...", end="")
        fn_id = gcc.register_function(echo_arg_tutorial_ep_testing)
        print(f"successfully with FN_UUID: {fn_id}")
    else:
        print(f"Using provided FN_UUID {fn_id}")

    echo_arg = str(random())
    print("Submitting task via Client...", end="")
    try:
        task_id = gcc.run(echo_arg, endpoint_id=TUTORIAL_EP_UUID, function_id=fn_id)
    except KeyError:
        print("TBD investigate KeyError")
        raise
    print(f"successfully with TASK_UUID: {task_id}, waiting {WAIT_CLIENT_SEC}s...")
    time.sleep(WAIT_CLIENT_SEC)
    result = gcc.get_result(task_id)
    if echo_arg in result:
        print(f"Client run result nominal: {result}")
    else:
        print(f"ERROR: Client run result was invalid: {result} ")
        sys.exit(1)

    print("Submitting task via Executor...", end="")
    with Executor(client=gcc, endpoint_id=TUTORIAL_EP_UUID) as gce:
        fut = gce.submit_to_registered_function(fn_id, args=(echo_arg,))
        result = fut.result()
        if echo_arg in result:
            print(f"result nominal: {result}")
        else:
            print(f"ERROR: submit result was invalid: {result} ")
            sys.exit(1)

    print(f"Test finished successfully on ({env_id})!")
