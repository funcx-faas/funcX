import logging
import uuid
from queue import Queue

import pytest
from globus_compute_common import messagepack
from globus_compute_endpoint.engines import GlobusComputeEngine
from globus_compute_sdk.sdk.bash_function import BashFunction
from globus_compute_sdk.serialize import ComputeSerializer
from parsl.providers import LocalProvider
from tests.utils import ez_pack_function


@pytest.fixture
def gc_engine(tmp_path):
    ep_id = uuid.uuid4()
    engine = GlobusComputeEngine(
        address="127.0.0.1",
        max_workers=1,
        heartbeat_period=1,
        heartbeat_threshold=1,
        provider=LocalProvider(
            init_blocks=1,
            min_blocks=0,
            max_blocks=1,
        ),
    )
    engine._status_report_thread.reporting_period = 1
    queue = Queue()
    engine.start(endpoint_id=ep_id, run_dir=tmp_path, results_passthrough=queue)
    yield engine
    engine.shutdown()


@pytest.fixture
def gc_engine_with_sandbox(tmp_path):
    ep_id = uuid.uuid4()
    engine = GlobusComputeEngine(
        address="127.0.0.1",
        max_workers=1,
        heartbeat_period=1,
        heartbeat_threshold=1,
        working_dir=tmp_path,
        run_in_sandbox=True,
        provider=LocalProvider(
            init_blocks=1,
            min_blocks=0,
            max_blocks=1,
        ),
    )
    engine._status_report_thread.reporting_period = 1
    queue = Queue()
    engine.start(endpoint_id=ep_id, run_dir=tmp_path, results_passthrough=queue)
    yield engine
    engine.shutdown()


def test_bash_function(gc_engine, tmp_path):
    """Test running BashFunction with GCE: Happy path"""
    engine = gc_engine
    queue = engine.results_passthrough
    task_id = uuid.uuid1()
    serializer = ComputeSerializer()
    bash_func = BashFunction("pwd")
    task_body = ez_pack_function(serializer, bash_func, (), {})
    task_message = messagepack.pack(
        messagepack.message_types.Task(task_id=task_id, task_buffer=task_body)
    )
    engine.submit(task_id, task_message)

    flag = False
    for _i in range(20):
        q_msg = queue.get(timeout=10)
        assert isinstance(q_msg, dict)

        packed_result_q = q_msg["message"]
        result = messagepack.unpack(packed_result_q)
        if isinstance(result, messagepack.message_types.Result):
            assert result.task_id == task_id
            assert result.error_details is None
            result_obj = serializer.deserialize(result.data)

            assert "pwd" == result_obj.cmd
            assert result_obj.returncode == 0
            assert tmp_path.name in result_obj.stdout
            assert "pwd" in result_obj.stderr
            flag = True
            break

    assert flag, "Expected result packet, but none received"


@pytest.mark.parametrize(
    "cmd, error_str, returncode",
    [
        ("sleep 5", "", 124),
        ("cat /NONEXISTENT", "No such file or directory", 1),
        ("echo 'very bad' 1&>2; exit 3", "very bad", 3),
        ("fake_command", "command not found", 127),
        ("touch foo; ./foo", "Permission denied", 126),
    ],
)
def test_fail_bash_function(gc_engine, tmp_path, cmd, error_str, returncode):
    """Test running BashFunction with GCE: Happy path"""
    engine = gc_engine
    queue = engine.results_passthrough
    task_id = uuid.uuid1()
    serializer = ComputeSerializer()
    bash_func = BashFunction(cmd, walltime=0.1)
    task_body = ez_pack_function(serializer, bash_func, (), {})
    task_message = messagepack.pack(
        messagepack.message_types.Task(task_id=task_id, task_buffer=task_body)
    )
    engine.submit(task_id, task_message)

    flag = False
    for _i in range(20):
        q_msg = queue.get(timeout=10)
        assert isinstance(q_msg, dict)

        packed_result_q = q_msg["message"]
        result = messagepack.unpack(packed_result_q)
        if isinstance(result, messagepack.message_types.Result):
            logging.warning(f"Got {result.data=}")

            assert result.task_id == task_id
            assert not result.error_details

            result_obj = serializer.deserialize(result.data)

            logging.warning(f"{result_obj.stdout=}")
            logging.warning(f"{result_obj.stderr=}")
            logging.warning(f"{result_obj.returncode=}")

            assert error_str in result_obj.stderr
            assert result_obj.returncode == returncode

            flag = True
            break

    assert flag, "Expected result packet, but none received"
