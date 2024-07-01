import os
import subprocess
import sys
import textwrap
from unittest import mock

import pytest
from globus_compute_sdk import Client
from globus_compute_sdk.sdk.executor import Executor, _ResultWatcher
from tests.utils import try_assert


@pytest.mark.skipif(
    not os.getenv("COMPUTE_INTEGRATION_TEST_WEB_URL"), reason="no integration web url"
)
def test_resultwatcher_graceful_shutdown():
    service_url = os.environ["COMPUTE_INTEGRATION_TEST_WEB_URL"]
    gcc = Client()
    gcc.web_service_address = service_url
    gcc.web_client = gcc.login_manager.get_web_client(service_url)
    gce = Executor(client=gcc)
    rw = _ResultWatcher(gce)
    rw._start_consuming = mock.Mock()
    rw.start()

    try_assert(lambda: rw._start_consuming.called)
    rw.shutdown()

    try_assert(lambda: rw._channel is None)
    try_assert(lambda: not rw._connection or rw._connection.is_closed)
    try_assert(lambda: not rw.is_alive())
    gce.shutdown()


def test_executor_atexit_handler_catches_all_instances(tmp_path):
    test_script = tmp_path / "test.py"
    script_content = textwrap.dedent(
        """
        import random
        from globus_compute_sdk import Executor
        from globus_compute_sdk.sdk.executor import _REGISTERED_EXECUTORS

        gcc = " a fake client"
        num_executors = random.randrange(1, 10)
        for i in range(num_executors):
            Executor(client=gcc)  # start N threads, none shutdown
        gce = Executor(client=gcc)  # intentionally overwritten
        gce = Executor(client=gcc)

        num_executors += 2
        assert len(_REGISTERED_EXECUTORS) == num_executors, (
            f"Verify test setup: {len(_REGISTERED_EXECUTORS)} != {num_executors}"
        )
        gce.shutdown()  # only shutting down _last_ instance.  Should still exit cleanly
        """
    )
    test_script.write_text(script_content)
    res = subprocess.run([sys.executable, str(test_script)], timeout=5)
    assert res.returncode == 0
