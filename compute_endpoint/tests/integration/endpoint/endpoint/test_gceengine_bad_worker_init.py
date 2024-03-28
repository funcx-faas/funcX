import logging
import uuid
from queue import Queue

import pytest
from globus_compute_common import messagepack
from globus_compute_endpoint.engines import GlobusComputeEngine
from globus_compute_sdk.serialize import ComputeSerializer
from parsl.executors.errors import BadStateException
from parsl.jobs.errors import TooManyJobFailuresError
from parsl.providers import LocalProvider
from tests.utils import double, ez_pack_function

logger = logging.getLogger(__name__)


@pytest.fixture
def gc_engine_bad_submit_command(tmp_path):
    """This config is setup to fail *SILENTLY* meaning
    the block will fail with exit_code:0 so the provider
    will not track it is a failure.
    """
    ep_id = uuid.uuid4()
    engine = GlobusComputeEngine(
        address="127.0.0.1",
        heartbeat_period=1,
        heartbeat_threshold=2,
        provider=LocalProvider(
            init_blocks=1,
            min_blocks=0,
            max_blocks=1,
            worker_init='echo "BAD SUBMIT COMMAND"; exit 0',
        ),
        job_status_kwargs={"max_idletime": 0, "strategy_period": 0.1},
        strategy="simple",
    )
    queue = Queue()
    engine.start(endpoint_id=ep_id, run_dir=str(tmp_path), results_passthrough=queue)

    yield engine
    engine.shutdown()


def test_bad_submit_command(gc_engine_bad_submit_command, caplog):
    engine = gc_engine_bad_submit_command
    queue = engine.results_passthrough
    caplog.set_level(logging.WARNING)

    task_id = uuid.uuid4()
    serializer = ComputeSerializer()
    task_body = ez_pack_function(
        serializer,
        double,
        (5,),
        {},
    )
    task_message = messagepack.pack(
        messagepack.message_types.Task(task_id=task_id, task_buffer=task_body)
    )
    future = engine.submit(task_id=task_id, packed_task=task_message)

    with pytest.raises(BadStateException):
        future.result(timeout=1)

    report = str(future.exception())
    assert "EXIT CODE: 0" in report
    assert "STDOUT: BAD SUBMIT COMMAND" in report

    flag = False
    for _i in range(10):
        q_msg = queue.get(timeout=5)
        assert isinstance(q_msg, dict)

        packed_result_q = q_msg["message"]
        result = messagepack.unpack(packed_result_q)
        if isinstance(result, messagepack.message_types.Result):
            assert result.task_id == task_id
            assert result.error_details
            assert result.data
            assert "MISSING" in result.data
            assert "BAD SUBMIT COMMAND" in result.data
            assert "EXIT CODE: 0" in result.data
            flag = True
            break

    assert flag, "Expected BadStateException in failed result.data, but none received"


def test_submit_to_broken_executor(gc_engine_bad_submit_command, caplog):
    """Submitting job to an executor in a bad state"""
    engine = gc_engine_bad_submit_command
    queue = engine.results_passthrough

    engine.executor.set_bad_state_and_fail_all(TooManyJobFailuresError())

    task_id = uuid.uuid4()
    serializer = ComputeSerializer()
    task_body = ez_pack_function(
        serializer,
        double,
        (5,),
        {},
    )
    task_message = messagepack.pack(
        messagepack.message_types.Task(task_id=task_id, task_buffer=task_body)
    )
    future = engine.submit(task_id=task_id, packed_task=task_message)

    with pytest.raises(TooManyJobFailuresError):
        future.result()

    flag = False
    for _i in range(10):
        q_msg = queue.get(timeout=1)
        assert isinstance(q_msg, dict)

        packed_result_q = q_msg["message"]
        result = messagepack.unpack(packed_result_q)
        if isinstance(result, messagepack.message_types.Result):
            assert result.error_details
            assert "TooManyJobFailures" in result.data
            flag = True
            break

    assert flag, "Expected result with exception body, none received"
