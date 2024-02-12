import concurrent.futures
import logging
import pathlib
import random
import time
import uuid
from queue import Queue
from unittest import mock

import pytest
from globus_compute_common import messagepack
from globus_compute_common.messagepack.message_types import TaskTransition
from globus_compute_common.tasks import ActorName, TaskState
from globus_compute_endpoint.endpoint.config import Config
from globus_compute_endpoint.endpoint.config.utils import serialize_config
from globus_compute_endpoint.engines import (
    GlobusComputeEngine,
    HighThroughputEngine,
    ProcessPoolEngine,
    ThreadPoolEngine,
)
from globus_compute_endpoint.engines.base import GlobusComputeEngineBase
from globus_compute_sdk.serialize import ComputeSerializer
from parsl.executors.high_throughput.interchange import ManagerLost
from pytest_mock import MockFixture
from tests.utils import double, ez_pack_function, kill_manager

logger = logging.getLogger(__name__)


def test_result_message_packing():
    exec_start = TaskTransition(
        timestamp=time.time_ns(), state=TaskState.EXEC_START, actor=ActorName.WORKER
    )

    serializer = ComputeSerializer()
    task_id = uuid.uuid1()
    result = random.randint(0, 1000)

    exec_end = TaskTransition(
        timestamp=time.time_ns(), state=TaskState.EXEC_END, actor=ActorName.WORKER
    )
    result_message = dict(
        task_id=task_id,
        data=serializer.serialize(result),
        task_statuses=[exec_start, exec_end],
    )

    mResult = messagepack.message_types.Result(**result_message)
    assert isinstance(mResult, messagepack.message_types.Result)
    packed_result = messagepack.pack(mResult)
    assert isinstance(packed_result, bytes)

    unpacked = messagepack.unpack(packed_result)
    assert isinstance(unpacked, messagepack.message_types.Result)
    # assert unpacked.
    logger.warning(f"Type of unpacked : {unpacked}")
    assert unpacked.task_id == task_id
    assert serializer.deserialize(unpacked.data) == result


@pytest.mark.parametrize(
    "engine_type", (ProcessPoolEngine, ThreadPoolEngine, GlobusComputeEngine)
)
def test_engine_submit(engine_type: GlobusComputeEngineBase, engine_runner):
    """Test engine.submit with multiple engines"""
    engine = engine_runner(engine_type)

    param = random.randint(1, 100)
    future = engine._submit(double, param)
    assert isinstance(future, concurrent.futures.Future)

    # 5-seconds is nominally "overkill," but gc on CI appears to need (at least) >1s
    assert future.result(timeout=5) == param * 2


@pytest.mark.parametrize(
    "engine_type", (ProcessPoolEngine, ThreadPoolEngine, GlobusComputeEngine)
)
def test_engine_submit_internal(engine_type: GlobusComputeEngineBase, engine_runner):
    engine = engine_runner(engine_type)

    q = engine.results_passthrough
    task_id = uuid.uuid1()
    serializer = ComputeSerializer()
    task_body = ez_pack_function(serializer, double, (3,), {})
    task_message = messagepack.pack(
        messagepack.message_types.Task(
            task_id=task_id, container_id=uuid.uuid1(), task_buffer=task_body
        )
    )
    future = engine.submit(str(task_id), task_message)
    packed_result = future.result()

    # Confirm that the future got the right answer
    assert isinstance(packed_result, bytes)
    result = messagepack.unpack(packed_result)
    assert isinstance(result, messagepack.message_types.Result)
    assert result.task_id == task_id

    # Confirm that the same result got back though the queue
    for _i in range(3):
        q_msg = q.get(timeout=0.1)
        assert isinstance(q_msg, dict)

        packed_result_q = q_msg["message"]
        result = messagepack.unpack(packed_result_q)
        # Handle a sneaky EPStatusReport that popped in ahead of the result
        if isinstance(result, messagepack.message_types.EPStatusReport):
            continue

        passed_task_id = q_msg["task_id"]
        # At this point the message should be the result
        assert (
            packed_result == packed_result_q
        ), "Result from passthrough_q and future should match"

        assert passed_task_id == str(task_id)
        assert result.task_id == task_id
        final_result = serializer.deserialize(result.data)
        assert final_result == 6, f"Expected 6, but got: {final_result}"
        break


def test_proc_pool_engine_not_started():
    engine = ProcessPoolEngine(max_workers=2)

    with pytest.raises(AssertionError) as pyt_exc:
        engine.submit(double, 10)
    assert "engine has not been started" in str(pyt_exc)

    with pytest.raises(AssertionError):
        engine.get_status_report()
    assert "engine has not been started" in str(pyt_exc)


def test_gc_engine_system_failure(engine_runner):
    """Test behavior of engine failure killing task"""
    engine = engine_runner(GlobusComputeEngine)
    engine.max_retries_on_system_failure = 0

    task_id = uuid.uuid1()
    serializer = ComputeSerializer()
    task_body = ez_pack_function(serializer, kill_manager, (), {})
    task_message = messagepack.pack(
        messagepack.message_types.Task(
            task_id=task_id, container_id=uuid.uuid1(), task_buffer=task_body
        )
    )
    future = engine.submit(str(task_id), task_message)

    assert isinstance(future, concurrent.futures.Future)
    with pytest.raises(ManagerLost):
        future.result()


@pytest.mark.parametrize("engine_type", (GlobusComputeEngine, HighThroughputEngine))
def test_serialized_engine_config_has_provider(engine_type: GlobusComputeEngineBase):
    ep_config = Config(executors=[engine_type(address="127.0.0.1")])

    res = serialize_config(ep_config)
    executor = res["executors"][0].get("executor") or res["executors"][0]

    assert executor.get("provider")


def test_gcengine_pass_through_to_executor(mocker: MockFixture):
    mock_executor = mocker.patch(
        "globus_compute_endpoint.engines.globus_compute.HighThroughputExecutor"
    )

    args = ("arg1", 2)
    kwargs = {
        "label": "VroomEngine",
        "address": "127.0.0.1",
        "encrypted": False,
        "foo": "bar",
    }
    GlobusComputeEngine(*args, **kwargs)

    a, k = mock_executor.call_args
    assert a == args
    assert kwargs == k


def test_gcengine_start_pass_through_to_executor(
    mocker: MockFixture, tmp_path: pathlib.Path
):
    mock_executor = mocker.patch(
        "globus_compute_endpoint.engines.globus_compute.HighThroughputExecutor"
    )
    mock_executor.provider = mock.MagicMock()

    run_dir = tmp_path
    scripts_dir = str(tmp_path / "submit_scripts")
    engine = GlobusComputeEngine(executor=mock_executor)

    assert mock_executor.run_dir != run_dir
    assert mock_executor.provider.script_dir != scripts_dir

    engine.start(endpoint_id=uuid.uuid4(), run_dir=run_dir, results_passthrough=Queue())
    engine.shutdown()

    assert mock_executor.run_dir == run_dir
    assert mock_executor.provider.script_dir == scripts_dir


@pytest.mark.parametrize("encrypted", (True, False))
def test_gcengine_encrypted(encrypted: bool, engine_runner):
    engine: GlobusComputeEngine = engine_runner(
        GlobusComputeEngine, encrypted=encrypted
    )
    assert engine.encrypted is engine.executor.encrypted is encrypted

    with pytest.raises(AttributeError):
        engine.encrypted = not encrypted

    engine.executor.encrypted = not encrypted
    assert engine.encrypted is not encrypted
