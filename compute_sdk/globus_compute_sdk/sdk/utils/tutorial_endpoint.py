import time
from datetime import datetime as dt

from globus_compute_sdk import Client, Executor
from globus_compute_sdk.sdk._environments import TUTORIAL_EP_UUID

# No arguments, just returns "Hello World"
EXISTING_PUBLIC_FUID = "b0a5d1a0-2b22-4381-b899-ba73321e41e0"


def sdk_tutorial_sample_function():
    return f"Hello world at {dt.now().isoformat()}"


class TutorialEndpointTester:
    def __init__(self):
        self.gcc = Client()
        self.fuid = None

    def register_sample_function(self) -> str:
        self.fuid = self.gcc.register_function(sdk_tutorial_sample_function)
        return self.fuid

    def run_func_with_client(self, max_wait_s: int = 15, fuid=None):
        resp = self.gcc.run(endpoint_id=TUTORIAL_EP_UUID, function_id=fuid)
        e_msg = None

        # Check for results until the time limit allowed
        while max_wait_s >= 0:
            time.sleep(5)
            try:
                print(self.gcc.get_result(resp))
                return
            except Exception as e:
                e_msg = f"{str(e)}"
                if "pending due to" not in e_msg:
                    print(
                        "Unexpected error submitting to Globus Compute Client:\n"
                        f"{e_msg}"
                    )
            max_wait_s -= 5
        if e_msg is not None:
            print(e_msg)
        else:
            print(f"No result from Client task submission to {TUTORIAL_EP_UUID}")

    def run_new_func_with_executor(self):
        with Executor(funcx_client=self.gcc, endpoint_id=TUTORIAL_EP_UUID) as gce:
            try:
                fut = gce.submit(sdk_tutorial_sample_function)
                print(fut.result(timeout=5))
            except Exception as e:
                print(
                    "Unexpected error submitting to Globus Compute Executor:\n" f"{e}"
                )

    def run_existing_func_with_Client(self):
        self.run_func_with_client(fuid=EXISTING_PUBLIC_FUID)

    def run_new_func_with_Client(self):
        if self.fuid is None:
            self.register_sample_function()
        self.run_func_with_client(fuid=self.fuid)
