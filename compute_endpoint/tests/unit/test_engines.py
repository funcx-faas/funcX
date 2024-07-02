import concurrent.futures
import logging
import pathlib
import random
import time
import typing as t
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
from parsl import HighThroughputExecutor
from parsl.executors.high_throughput.interchange import ManagerLost
from parsl.providers import KubernetesProvider
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
        future = engine.submit(double, 10)
        future.result()
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
        "max_workers": 1,
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
    mock_executor.status_polling_interval = 5

    mocker.patch.object(GlobusComputeEngine, "_get_compute_launch_cmd")

    run_dir = tmp_path
    scripts_dir = str(tmp_path / "submit_scripts")
    engine = GlobusComputeEngine(executor=mock_executor)

    assert mock_executor.run_dir != run_dir
    assert mock_executor.provider.script_dir != scripts_dir

    engine.start(endpoint_id=uuid.uuid4(), run_dir=run_dir, results_passthrough=Queue())
    engine.shutdown()

    assert mock_executor.run_dir == run_dir
    assert mock_executor.provider.script_dir == scripts_dir


def test_gcengine_start_provider_without_channel(tmp_path: pathlib.Path):
    mock_executor = mock.Mock(spec=HighThroughputExecutor)
    mock_executor.status_polling_interval = 5
    mock_executor.provider = mock.Mock(spec=KubernetesProvider)

    assert not hasattr(mock_executor.provider, "channel"), "Verify test setup"

    engine = GlobusComputeEngine(executor=mock_executor)
    engine.start(
        endpoint_id=uuid.uuid4(), run_dir=tmp_path, results_passthrough=Queue()
    )
    engine.shutdown()


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


@pytest.mark.parametrize("max_workers_per_node", (1, 2, None))
@pytest.mark.parametrize("max_workers", (3, 4, 5))
def test_gcengine_max_workers_per_node(
    engine_runner, max_workers_per_node: t.Union[int, None], max_workers: int
):
    engine: GlobusComputeEngine = engine_runner(
        GlobusComputeEngine,
        max_workers_per_node=max_workers_per_node,
        max_workers=max_workers,
    )

    if max_workers_per_node:
        assert engine.executor.max_workers == max_workers_per_node
    else:
        assert engine.executor.max_workers == max_workers


def test_gcengine_new_executor_not_exceptional():
    gce = GlobusComputeEngine()
    assert gce.executor_exception is None, "Expect no exception from fresh Executor"


def test_gcengine_executor_exception_passthrough(randomstring):
    gce = GlobusComputeEngine()
    exc_text = randomstring()
    gce.executor.set_bad_state_and_fail_all(ZeroDivisionError(exc_text))
    assert isinstance(gce.executor_exception, ZeroDivisionError)
    assert exc_text in str(gce.executor_exception)


def test_gcengine_bad_state_futures_failed_immediately(randomstring):
    gce = GlobusComputeEngine()
    exc_text = randomstring()
    gce.executor.set_bad_state_and_fail_all(ZeroDivisionError(exc_text))

    taskb = b"some packed task bytes"
    futs = [gce.submit(task_id=str(uuid.uuid4()), packed_task=taskb) for _ in range(5)]

    assert all(f.done() for f in futs), "Expect immediate completion for failed state"
    assert all(exc_text in str(f.exception()) for f in futs)


def test_gcengine_exception_report_from_bad_state():
    gce = GlobusComputeEngine()
    gce.executor.set_bad_state_and_fail_all(ZeroDivisionError())

    task_id = uuid.uuid4()
    gce.submit(task_id=str(task_id), packed_task=b"MOCK_PACKED_TASK")

    result = None
    for _i in range(10):
        q_msg = gce.results_passthrough.get(timeout=5)
        packed_result_q = q_msg["message"]
        result = messagepack.unpack(packed_result_q)
        if isinstance(result, messagepack.message_types.Result):
            break

    assert result.task_id == task_id
    assert result.error_details.code == "RemoteExecutionError"
    assert "ZeroDivisionError" in result.data
