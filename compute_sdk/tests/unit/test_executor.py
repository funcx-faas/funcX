from __future__ import annotations

import platform
import random
import threading
import typing as t
import uuid
from unittest import mock

import pika
import pytest
from globus_compute_common import messagepack
from globus_compute_common.messagepack.message_types import Result, ResultErrorDetails
from globus_compute_sdk import Client, Executor, __version__
from globus_compute_sdk.errors import TaskExecutionFailed
from globus_compute_sdk.sdk.asynchronous.compute_future import ComputeFuture
from globus_compute_sdk.sdk.executor import _ResultWatcher, _TaskSubmissionInfo
from globus_compute_sdk.sdk.utils import chunk_by
from globus_compute_sdk.sdk.utils.uuid_like import as_optional_uuid, as_uuid
from globus_compute_sdk.sdk.web_client import WebClient
from globus_compute_sdk.serialize.facade import ComputeSerializer
from pytest_mock import MockerFixture
from tests.utils import try_assert, try_for_timeout

_MOCK_BASE = "globus_compute_sdk.sdk.executor."
_chunk_size = 1024


def _is_stopped(thread: threading.Thread | None) -> t.Callable[..., bool]:
    def _wrapped():
        return not (thread and thread.is_alive())

    return _wrapped


def noop():
    return 1


class MockedExecutor(Executor):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault(
            "client",
            mock.Mock(
                spec=Client,
                web_client=mock.Mock(spec=WebClient),
                fx_serializer=mock.Mock(spec=ComputeSerializer),
            ),
        )
        super().__init__(*args, **kwargs)
        self._test_task_submitter_exception: t.Type[Exception] | None = None
        self._test_task_submitter_done = False

    def _task_submitter_impl(self):
        try:
            super()._task_submitter_impl()
        except Exception as exc:
            self._test_task_submitter_exception = exc
        finally:
            self._test_task_submitter_done = True


class MockedResultWatcher(_ResultWatcher):
    def __init__(self, *args, **kwargs):
        if not args:
            args = (MockedExecutor(),)
        super().__init__(*args, **kwargs)
        self._time_to_stop_mock = threading.Event()

    def start(self) -> None:
        super().start()
        try_assert(lambda: self._connection is not None)

    def run(self):
        self._connection = mock.MagicMock()
        self._channel = mock.MagicMock()
        self._time_to_stop_mock.wait()

    def shutdown(self, *args, **kwargs):
        super().shutdown(*args, **kwargs)
        self._time_to_stop_mock.set()

    def join(self, timeout: float | None = None) -> None:
        if self._time_to_stop:  # important to identify bugs
            self._time_to_stop_mock.set()
        super().join(timeout=timeout)


@pytest.fixture
def gc_executor(mocker):
    gce = MockedExecutor()
    watcher = mocker.patch(f"{_MOCK_BASE}_ResultWatcher", autospec=True)

    def create_mock_watcher(*args, **kwargs):
        return MockedResultWatcher(gce)

    watcher.side_effect = create_mock_watcher

    yield gce.client, gce

    gce.shutdown(wait=False, cancel_futures=True)
    try_for_timeout(_is_stopped(gce._task_submitter))
    try_for_timeout(_is_stopped(gce._result_watcher))

    if not _is_stopped(gce._task_submitter)():
        trepr = repr(gce._task_submitter)
        raise RuntimeError(
            "Executor still running: _task_submitter thread alive: %s" % trepr
        )
    if not _is_stopped(gce._result_watcher)():
        trepr = repr(gce._result_watcher)
        raise RuntimeError(
            "Executor still running: _result_watcher thread alive: %r" % trepr
        )


@pytest.fixture
def mock_log(mocker):
    yield mocker.patch(f"{_MOCK_BASE}log", autospec=True)


@pytest.mark.parametrize(
    "tg_id, fn_id, ep_id, uep_config",
    (
        (uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), {"heartbeat": 10}),
        (str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), None),
    ),
)
def test_task_submission_info_stringification(tg_id, fn_id, ep_id, uep_config):
    fut_id = 10

    info = _TaskSubmissionInfo(
        task_num=fut_id,
        task_group_id=tg_id,
        function_id=fn_id,
        endpoint_id=ep_id,
        user_endpoint_config=uep_config,
        args=(),
        kwargs={},
    )
    as_str = str(info)
    assert as_str.startswith("_TaskSubmissionInfo(")
    assert as_str.endswith("args=[0]; kwargs=[0])")
    assert "task_num=10" in as_str
    assert f"task_group_uuid='{tg_id}'" in as_str
    assert f"function_uuid='{fn_id}'" in as_str
    assert f"endpoint_uuid='{ep_id}'" in as_str
    uep_config_len = len(uep_config) if uep_config else 0
    assert f"user_endpoint_config={{{uep_config_len}}};"


@pytest.mark.parametrize("argname", ("batch_interval", "batch_enabled"))
def test_deprecated_args_warned(argname, mocker):
    mock_warn = mocker.patch(f"{_MOCK_BASE}warnings")
    gcc = mock.Mock(spec=Client)
    Executor(client=gcc).shutdown()
    mock_warn.warn.assert_not_called()

    Executor(client=gcc, **{argname: 1}).shutdown()
    mock_warn.warn.assert_called()


def test_invalid_args_raise(randomstring):
    invalid_arg = f"abc_{randomstring()}"
    with pytest.raises(TypeError) as wrapped_err:
        Executor(**{invalid_arg: 1}).shutdown()

    err = wrapped_err.value
    assert "invalid argument" in str(err)
    assert f"'{invalid_arg}'" in str(err)


def test_creates_default_client_if_none_provided(mocker):
    mock_gcc_klass = mocker.patch(f"{_MOCK_BASE}Client")
    Executor().shutdown()

    mock_gcc_klass.assert_called()


def test_executor_shutdown(gc_executor):
    _, gce = gc_executor
    gce.shutdown()

    try_assert(_is_stopped(gce._task_submitter))
    try_assert(_is_stopped(gce._result_watcher))


def test_executor_context_manager(gc_executor):
    _, gce = gc_executor
    with gce:
        pass
    assert _is_stopped(gce._task_submitter)
    assert _is_stopped(gce._result_watcher)


def test_executor_stuck_submitter_doesnt_hold_shutdown(gc_executor, mock_log):
    _, gce = gc_executor
    gce._task_submitter = mock.Mock(spec=threading.Thread)
    gce._task_submitter.join.side_effect = lambda *a, **kw: None
    gce._task_submitter.is_alive.return_value = True

    assert not gce._task_submitter.join.called, "Verify test setup"
    gce.shutdown()
    assert gce._task_submitter.join.called, "Verify test setup"

    gce._task_submitter.is_alive.return_value = False  # allow test cleanup

    warn_log, _ = mock_log.warning.call_args
    assert "did not stop in a timely fashion" in warn_log[0]


def test_multiple_register_function_fails(gc_executor):
    gcc, gce = gc_executor

    gcc.register_function.return_value = "abc"

    gce.register_function(noop)

    with pytest.raises(ValueError):
        gce.register_function(noop)

    try_assert(lambda: gce._stopped)

    with pytest.raises(RuntimeError):
        gce.register_function(noop)


def test_shortcut_register_function(gc_executor):
    gcc, gce = gc_executor

    fn_id = str(uuid.uuid4())
    gce.register_function(noop, function_id=fn_id)

    with pytest.raises(ValueError) as pyt_exc:
        gce.register_function(noop, function_id=fn_id)

    assert "Function already registered" in str(pyt_exc.value)
    gcc.register_function.assert_not_called()


def test_failed_registration_shuts_down_executor(gc_executor, randomstring):
    gcc, gce = gc_executor

    exc = RuntimeError(randomstring())
    gcc.register_function.side_effect = exc

    with pytest.raises(Exception) as pyt_exc:
        gce.register_function(noop)

    assert pyt_exc.value is exc, "Expected raw exception raised"
    try_assert(lambda: gce._stopped)


@pytest.mark.parametrize("container_id", (None, uuid.uuid4(), str(uuid.uuid4())))
def test_container_id_as_id(gc_executor, container_id):
    _, gce = gc_executor
    assert gce.container_id is None, "Default value is None"
    gce.container_id = container_id
    if container_id is None:
        assert gce.container_id is None
    else:
        expected_container_id = as_uuid(container_id)
        assert gce.container_id == expected_container_id


@pytest.mark.parametrize("container_id", (None, uuid.uuid4(), str(uuid.uuid4())))
def test_register_function_sends_container_id(gc_executor, container_id):
    gcc, gce = gc_executor

    gce.container_id = container_id
    gce.register_function(noop)
    assert gcc.register_function.called
    a, k = gcc.register_function.call_args

    expected_container_id = as_optional_uuid(container_id)
    assert k["container_uuid"] == expected_container_id


def test_register_function_sends_metadata(gc_executor):
    gcc, gce = gc_executor

    gce.register_function(noop)

    assert gcc.register_function.called
    a, k = gcc.register_function.call_args
    assert k["metadata"].get("python_version") == platform.python_version()
    assert k["metadata"].get("sdk_version") == __version__


@pytest.mark.parametrize(
    "is_valid,uep_config",
    (
        (True, None),
        (True, {"foo": "bar"}),
        (False, {"foo": str}),
        (False, "{'foo': 'bar'}"),
        (False, "config"),
        (False, 1),
    ),
)
def test_user_endpoint_config(gc_executor, is_valid: bool, uep_config):
    _, gce = gc_executor

    assert gce.user_endpoint_config is None, "Default value is None"

    if is_valid:
        gce.user_endpoint_config = uep_config
        assert gce.user_endpoint_config == uep_config
    else:
        with pytest.raises(TypeError) as pyt_e:
            gce.user_endpoint_config = uep_config
        if isinstance(uep_config, dict):
            # Ensure we retain original exception
            assert isinstance(pyt_e.value.__cause__, TypeError)
        assert gce.user_endpoint_config is None


def test_user_endpoint_config_added_to_batch(gc_executor):
    gcc, gce = gc_executor

    gce.endpoint_id = uuid.uuid4()
    gce.user_endpoint_config = {"foo": "bar"}
    gcc.register_function.return_value = uuid.uuid4()

    gce.submit(noop)

    try_assert(lambda: gcc.batch_run.called)
    args, _ = gcc.create_batch.call_args
    assert gce.user_endpoint_config in args


def test_submit_raises_if_thread_stopped(gc_executor):
    _, gce = gc_executor
    gce.shutdown()

    try_assert(_is_stopped(gce._task_submitter), "Test prerequisite")

    with pytest.raises(RuntimeError) as wrapped_exc:
        gce.submit(noop)

    err = wrapped_exc.value
    assert " is shutdown;" in str(err)


def test_submit_auto_registers_function(gc_executor):
    gcc, gce = gc_executor

    fn_id = uuid.uuid4()
    gcc.register_function.return_value = str(fn_id)
    gcc.batch_run.return_value = {
        "task_group_id": str(uuid.uuid4()),
        "tasks": {str(fn_id): [str(uuid.uuid4())]},
    }
    gce.endpoint_id = uuid.uuid4()
    gce.submit(noop)

    assert gcc.register_function.called


def test_submit_value_error_if_no_endpoint(gc_executor):
    _, gce = gc_executor

    with pytest.raises(ValueError) as pytest_exc:
        gce.submit(noop)

    err = pytest_exc.value
    assert "No endpoint_id set" in str(err)
    assert "    gce = Executor(endpoint_id=" in str(err), "Expected hint"
    try_assert(_is_stopped(gce._task_submitter), "Expected graceful shutdown on error")


def test_same_function_different_containers_allowed(gc_executor):
    _, gce = gc_executor
    c1_id, c2_id = str(uuid.uuid4()), str(uuid.uuid4())

    gce.container_id = c1_id
    gce.register_function(noop)
    gce.container_id = c2_id
    gce.register_function(noop)
    with pytest.raises(ValueError, match="already registered"):
        gce.register_function(noop)


def test_map_raises(gc_executor):
    _, gce = gc_executor

    with pytest.raises(NotImplementedError):
        gce.map(noop)


def test_reload_tasks_requires_task_group_id(gc_executor):
    _, gce = gc_executor

    assert gce.task_group_id is None, "verify test setup"
    with pytest.raises(Exception) as e:
        gce.reload_tasks()

    assert "must specify a task_group_id in order to reload tasks" in str(e.value)


def test_reload_tasks_sets_passed_task_group_id(gc_executor):
    gcc, gce = gc_executor

    # for less mocking:
    gcc.web_client.get_taskgroup_tasks.side_effect = RuntimeError("bailing out early")

    tg_id = uuid.uuid4()
    with pytest.raises(RuntimeError) as e:
        gce.reload_tasks(tg_id)

    assert "bailing out early" in str(e.value)
    assert gce.task_group_id == tg_id


@pytest.mark.parametrize("num_tasks", [0, 1, 2, 10])
def test_reload_tasks_none_completed(gc_executor, mock_log, num_tasks):
    gcc, gce = gc_executor

    gce.task_group_id = uuid.uuid4()
    mock_data = {
        "taskgroup_id": str(gce.task_group_id),
        "tasks": [{"id": uuid.uuid4()} for _ in range(num_tasks)],
    }
    mock_batch_result = {t["id"]: t for t in mock_data["tasks"]}
    mock_batch_result = mock.MagicMock(data={"results": mock_batch_result})

    gcc.web_client.get_taskgroup_tasks.return_value = mock_data
    gcc.web_client.get_batch_status.return_value = mock_batch_result

    client_futures = list(gce.reload_tasks())
    if num_tasks == 0:
        log_args, log_kwargs = mock_log.warning.call_args
        assert "Received no tasks" in log_args[0]
        assert str(gce.task_group_id) in log_args[0]
    else:
        assert not mock_log.warning.called

    assert len(client_futures) == num_tasks
    assert not any(fut.done() for fut in client_futures)


@pytest.mark.parametrize("num_tasks", [1, 2, 10])
def test_reload_tasks_some_completed(gc_executor, mock_log, num_tasks):
    gcc, gce = gc_executor

    gce.task_group_id = uuid.uuid4()
    mock_data = {
        "taskgroup_id": str(gce.task_group_id),
        "tasks": [{"id": uuid.uuid4()} for _ in range(num_tasks)],
    }
    num_completed = random.randint(1, num_tasks)
    num_i = 0

    serialize = ComputeSerializer().serialize
    mock_batch_result = {t["id"]: t for t in mock_data["tasks"]}
    for t_id in mock_batch_result:
        if num_i >= num_completed:
            break
        num_i += 1
        mock_batch_result[t_id]["completion_t"] = "0"
        mock_batch_result[t_id]["status"] = "success"
        mock_batch_result[t_id]["result"] = serialize("abc")
    mock_batch_result = mock.MagicMock(data={"results": mock_batch_result})

    gcc.web_client.get_taskgroup_tasks.return_value = mock_data
    gcc.web_client.get_batch_status.return_value = mock_batch_result

    client_futures = list(gce.reload_tasks())
    if num_tasks == 0:
        log_args, log_kwargs = mock_log.warning.call_args
        assert "Received no tasks" in log_args[0]
        assert gce.task_group_id in log_args[0]
    else:
        assert not mock_log.warning.called

    assert len(client_futures) == num_tasks
    assert sum(1 for fut in client_futures if fut.done()) == num_completed


def test_reload_tasks_all_completed(gc_executor):
    gcc, gce = gc_executor

    serialize = ComputeSerializer().serialize
    num_tasks = 5

    gce.task_group_id = uuid.uuid4()
    mock_data = {
        "taskgroup_id": str(gce.task_group_id),
        "tasks": [
            {
                "id": uuid.uuid4(),
                "completion_t": 25,
                "status": "success",
                "result": serialize("abc"),
            }
            for _ in range(num_tasks)
        ],
    }

    mock_batch_result = {t["id"]: t for t in mock_data["tasks"]}
    mock_batch_result = mock.MagicMock(data={"results": mock_batch_result})

    gcc.web_client.get_taskgroup_tasks.return_value = mock_data
    gcc.web_client.get_batch_status.return_value = mock_batch_result

    client_futures = list(gce.reload_tasks())

    assert len(client_futures) == num_tasks
    assert sum(1 for fut in client_futures if fut.done()) == num_tasks
    assert gce._result_watcher is None, "Should NOT start watcher: all tasks done!"


@pytest.mark.parametrize(
    "num_tasks",
    (
        0,
        1,
        3,
        500,
        1 * _chunk_size + -1,
        1 * _chunk_size + 0,
        1 * _chunk_size + 1,
        2 * _chunk_size + -1,
        2 * _chunk_size + 0,
        2 * _chunk_size + 1,
        3 * _chunk_size + -1,
        3 * _chunk_size + 0,
        3 * _chunk_size + 1,
    ),
)
def test_reload_chunks_tasks_requested(mock_log, gc_executor, num_tasks):
    gcc, gce = gc_executor

    exp_num_chunks = num_tasks // _chunk_size
    exp_num_chunks += num_tasks % _chunk_size > 0

    gce.task_group_id = uuid.uuid4()
    mock_data = {
        "taskgroup_id": str(gce.task_group_id),
        "tasks": [
            {
                "id": uuid.uuid4(),
                "completion_t": 25,
                "status": "success",
                "result": "abc",
            }
            for _ in range(num_tasks)
        ],
    }

    mock_batch_result = mock.Mock(data={"results": {}})

    gbs: mock.Mock = gcc.web_client.get_batch_status  # convenience
    gcc.web_client.get_taskgroup_tasks.return_value = mock_data
    gbs.return_value = mock_batch_result

    gce.reload_tasks()

    assert gbs.call_count == exp_num_chunks
    expected_chunks = chunk_by(mock_data["tasks"], _chunk_size)
    for expected_chunk, (a, _k) in zip(expected_chunks, gbs.call_args_list):
        assert tuple(_t["id"] for _t in expected_chunk) == a[0]

    if exp_num_chunks > 1:
        dbgs = [
            a
            for a, _ in mock_log.debug.call_args_list
            if a[0].startswith("Large task group")
        ]
        assert len(dbgs) == exp_num_chunks
        assert all(a[1] == num_tasks for a in dbgs)
        assert all(a[2] == exp_num_chunks for a in dbgs)
        assert all(a[3] == chunk_num for chunk_num, a in enumerate(dbgs, start=1))


def test_reload_starts_new_watcher(gc_executor):
    gcc, gce = gc_executor

    num_tasks = 3

    gce.task_group_id = uuid.uuid4()
    mock_data = {
        "taskgroup_id": str(gce.task_group_id),
        "tasks": [{"id": uuid.uuid4()} for _ in range(num_tasks)],
    }
    mock_batch_result = {t["id"]: t for t in mock_data["tasks"]}
    mock_batch_result = mock.MagicMock(data={"results": mock_batch_result})

    gcc.web_client.get_taskgroup_tasks.return_value = mock_data
    gcc.web_client.get_batch_status.return_value = mock_batch_result

    client_futures = list(gce.reload_tasks())

    assert len(client_futures) == num_tasks
    try_assert(lambda: gce._result_watcher.is_alive())
    watcher_1 = gce._result_watcher

    gce.reload_tasks()
    try_assert(lambda: gce._result_watcher.is_alive())
    watcher_2 = gce._result_watcher

    assert watcher_1 is not watcher_2


def test_reload_tasks_cancels_existing_futures(gc_executor, randomstring):
    gcc, gce = gc_executor

    gce.task_group_id = uuid.uuid4()

    def mock_data():
        return {
            "taskgroup_id": str(gce.task_group_id),
            "tasks": [{"id": uuid.uuid4()} for _ in range(random.randint(1, 20))],
        }

    md = mock_data()
    bs = {_t["id"]: {} for _t in md["tasks"]}
    gcc.web_client.get_taskgroup_tasks.return_value = md
    gcc.web_client.get_batch_status.return_value = mock.Mock(data={"results": bs})

    client_futures_1 = list(gce.reload_tasks())
    assert any(not fut.done() for fut in client_futures_1), "Verify test setup"

    gcc.web_client.get_taskgroup_tasks.return_value = mock_data()
    client_futures_2 = list(gce.reload_tasks())

    assert all(fut.cancelled() for fut in client_futures_1)
    assert not any(fut.done() for fut in client_futures_2)


def test_reload_client_taskgroup_tasks_fails_gracefully(gc_executor):
    gcc, gce = gc_executor

    gce.task_group_id = uuid.uuid4()
    mock_datum = (
        (KeyError, {"mispeleed": str(gce.task_group_id)}),
        (ValueError, {"taskgroup_id": "abcd"}),
        (None, {"taskgroup_id": str(gce.task_group_id)}),
    )

    for expected_exc_class, md in mock_datum:
        gcc.web_client.get_taskgroup_tasks.return_value = md
        if expected_exc_class:
            with pytest.raises(expected_exc_class):
                gce.reload_tasks()
        else:
            gce.reload_tasks()


def test_reload_sets_failed_tasks(gc_executor):
    gcc, gce = gc_executor

    gce.task_group_id = uuid.uuid4()
    mock_data = {
        "taskgroup_id": str(gce.task_group_id),
        "tasks": [
            {"id": uuid.uuid4(), "completion_t": 1, "exception": "doh!"}
            for i in range(random.randint(0, 10))
        ],
    }

    mock_batch_result = {t["id"]: t for t in mock_data["tasks"]}
    mock_batch_result = mock.MagicMock(data={"results": mock_batch_result})

    gcc.web_client.get_taskgroup_tasks.return_value = mock_data
    gcc.web_client.get_batch_status.return_value = mock_batch_result

    futs = list(gce.reload_tasks())

    assert all("doh!" in str(fut.exception()) for fut in futs)


def test_reload_handles_deseralization_error_gracefully(gc_executor):
    gcc, gce = gc_executor
    gcc.fx_serializer = ComputeSerializer()

    gce.task_group_id = uuid.uuid4()
    mock_data = {
        "taskgroup_id": str(gce.task_group_id),
        "tasks": [
            {"id": uuid.uuid4(), "completion_t": 1, "result": "a", "status": "success"}
            for i in range(random.randint(0, 10))
        ],
    }

    mock_batch_result = {t["id"]: t for t in mock_data["tasks"]}
    mock_batch_result = mock.MagicMock(data={"results": mock_batch_result})

    gcc.web_client.get_taskgroup_tasks.return_value = mock_data
    gcc.web_client.get_batch_status.return_value = mock_batch_result

    futs = list(gce.reload_tasks())

    assert all("Failed to set " in str(fut.exception()) for fut in futs)


@pytest.mark.parametrize("batch_size", tuple(range(1, 11)))
def test_task_submitter_respects_batch_size(gc_executor, batch_size: int):
    gcc, gce = gc_executor

    # make a new MagicMock every time create_batch is called
    gcc.create_batch.side_effect = lambda *_args, **_kwargs: mock.MagicMock()

    fn_id = str(uuid.uuid4())
    gcc.register_function.return_value = fn_id
    gcc.batch_run.return_value = {
        "tasks": {fn_id: [str(uuid.uuid4()) for _ in range(batch_size)]},
        "task_group_id": uuid.uuid4(),
    }
    num_batches = 50

    gce.endpoint_id = uuid.uuid4()
    gce.batch_size = batch_size
    for _ in range(num_batches * batch_size):
        gce.submit(noop)

    # force the batches to be populated by flushing the queues. more consistent than
    # waiting for the queues to flush themselves automatically due to slowdowns
    # introduced by `coverage`.
    gce.shutdown(cancel_futures=True)

    assert gcc.batch_run.call_count >= num_batches
    for args, _kwargs in gcc.batch_run.call_args_list:
        *_, batch = args
        assert 0 < batch.add.call_count <= batch_size


def test_task_submitter_stops_executor_on_exception(gc_executor):
    _, gce = gc_executor
    gce._tasks_to_send.put(("too", "much", "destructuring", "!!"))

    try_assert(lambda: gce._stopped)
    try_assert(lambda: isinstance(gce._test_task_submitter_exception, ValueError))


def test_task_submitter_stops_executor_on_upstream_error_response(
    gc_executor, randomstring
):
    _, gce = gc_executor

    upstream_error = Exception(f"Upstream error {randomstring}!!")
    gce.client.batch_run.side_effect = upstream_error
    gce.task_group_id = uuid.uuid4()
    tsi = _TaskSubmissionInfo(
        task_num=12345,
        task_group_id=uuid.uuid4(),
        endpoint_id=uuid.uuid4(),
        function_id=uuid.uuid4(),
        user_endpoint_config=None,
        args=(),
        kwargs={},
    )
    cf = ComputeFuture()
    gce._tasks_to_send.put((cf, tsi))

    try_assert(lambda: gce._stopped)
    try_assert(lambda: gce._test_task_submitter_done, "Expect graceful shutdown")
    assert cf.exception() is upstream_error
    assert gce._test_task_submitter_exception is None, "handled by future"


def test_sc25897_task_submit_correctly_handles_multiple_tg_ids(mocker, gc_executor):
    gcc, gce = gc_executor
    gce.endpoint_id = uuid.uuid4()
    gcc.register_function.return_value = uuid.uuid4()

    can_continue = threading.Event()

    def _mock_max(*a, **k):
        can_continue.wait()
        return max(*a, **k)

    mocker.patch(f"{_MOCK_BASE}max", side_effect=_mock_max)
    func_id = gce.register_function(noop)

    tg_id_1 = uuid.uuid4()
    tg_id_2 = uuid.uuid4()
    gcc.batch_run.side_effect = (
        ({"task_group_id": str(tg_id_1), "tasks": {func_id: [str(uuid.uuid4())]}}),
        ({"task_group_id": str(tg_id_2), "tasks": {func_id: [str(uuid.uuid4())]}}),
    )
    gce.task_group_id = tg_id_1
    gce.submit(noop)
    gce.task_group_id = tg_id_2
    gce.submit(noop)
    assert not gcc.create_batch.called, "Verify test setup"
    can_continue.set()

    try_assert(lambda: gcc.batch_run.call_count == 2)

    for expected, (a, _k) in zip((tg_id_1, tg_id_2), gcc.create_batch.call_args_list):
        found_tg_uuid = a[0]
        assert found_tg_uuid == expected


def test_task_submit_handles_multiple_user_endpoint_configs(
    mocker: MockerFixture, gc_executor
):
    gcc, gce = gc_executor
    gce.endpoint_id = uuid.uuid4()

    func_uuid_str = str(uuid.uuid4())
    tg_uuid_str = str(uuid.uuid4())
    gcc.register_function.return_value = func_uuid_str
    gcc.batch_run.side_effect = (
        ({"task_group_id": tg_uuid_str, "tasks": {func_uuid_str: [str(uuid.uuid4())]}}),
        ({"task_group_id": tg_uuid_str, "tasks": {func_uuid_str: [str(uuid.uuid4())]}}),
    )

    # Temporarily block the task submitter loop
    can_continue = threading.Event()

    def _mock_max(*a, **k):
        can_continue.wait()
        return max(*a, **k)

    mocker.patch(f"{_MOCK_BASE}max", side_effect=_mock_max)

    uep_config_1 = {"heartbeat": 10}
    uep_config_2 = {"heartbeat": 20}
    gce.user_endpoint_config = uep_config_1
    gce.submit(noop)
    gce.user_endpoint_config = uep_config_2
    gce.submit(noop)

    assert not gcc.create_batch.called, "Verify test setup"
    can_continue.set()

    try_assert(lambda: gcc.batch_run.call_count == 2)
    for expected, (a, _k) in zip(
        (uep_config_1, uep_config_2), gcc.create_batch.call_args_list
    ):
        found_uep_config = a[1]
        assert found_uep_config == expected


def test_task_submitter_handles_stale_result_watcher_gracefully(gc_executor):
    gcc, gce = gc_executor
    gcc.register_function.return_value = uuid.uuid4()
    gce.endpoint_id = uuid.uuid4()

    fn_id = str(uuid.uuid4())
    gce._function_registry[gce._fn_cache_key(noop)] = fn_id
    task_id = str(uuid.uuid4())
    gcc.batch_run.return_value = {
        "tasks": {fn_id: [task_id]},
        "task_group_id": str(uuid.uuid4()),
    }
    gce.submit(noop)
    try_assert(lambda: bool(gce._result_watcher), "Test prerequisite")
    try_assert(lambda: bool(gce._result_watcher._open_futures), "Test prerequisite")
    watcher_1 = gce._result_watcher
    watcher_1._closed = True  # simulate shutting down, but not yet stopped
    watcher_1._time_to_stop = True

    gce.submit(noop)
    try_assert(lambda: gce._result_watcher is not watcher_1, "Test prerequisite")


def test_task_submitter_sets_future_task_ids(gc_executor):
    gcc, gce = gc_executor

    num_tasks = random.randint(2, 20)
    futs = [ComputeFuture() for _ in range(num_tasks)]
    tasks = [
        mock.MagicMock(function_uuid="fn_id", args=[], kwargs={})
        for _ in range(num_tasks)
    ]
    batch_ids = [uuid.uuid4() for _ in range(num_tasks)]

    gcc.batch_run.return_value = {
        "request_id": "rq_id",
        "task_group_id": str(uuid.uuid4()),
        "endpoint_id": "ep_id",
        "tasks": {"fn_id": batch_ids},
    }
    gce._submit_tasks("tg_id", "ep_id", None, futs, tasks)

    assert all(f.task_id == task_id for f, task_id in zip(futs, batch_ids))


@pytest.mark.parametrize("batch_response", [{"tasks": "foo"}, {"task_group_id": "foo"}])
def test_submit_tasks_stops_futures_on_bad_response(gc_executor, batch_response):
    gcc, gce = gc_executor

    gcc.batch_run.return_value = batch_response

    num_tasks = random.randint(2, 20)
    futs = [ComputeFuture() for _ in range(num_tasks)]
    tasks = [
        mock.MagicMock(function_uuid="fn_id", args=[], kwargs={})
        for _ in range(num_tasks)
    ]

    with pytest.raises(KeyError) as pyt_exc:
        gce._submit_tasks("tg_id", "ep_id", None, futs, tasks)

    for fut in futs:
        assert fut.exception() is pyt_exc.value


def test_resultwatcher_stops_if_unable_to_connect(mocker):
    mock_time = mocker.patch(f"{_MOCK_BASE}time")
    rw = _ResultWatcher(MockedExecutor())
    rw._connect = mock.Mock(return_value=mock.Mock(spec=pika.SelectConnection))

    rw.run()
    assert rw._connection_tries >= rw.connect_attempt_limit
    assert mock_time.sleep.call_count == rw._connection_tries - 1, "Should wait between"


def test_resultwatcher_ignores_invalid_tasks():
    rw = _ResultWatcher(MockedExecutor())
    rw._connect = mock.Mock(return_value=mock.Mock(spec=pika.SelectConnection))

    futs = [ComputeFuture() for i in range(random.randint(1, 10))]
    futs[0].task_id = uuid.uuid4()
    num_added = rw.watch_for_task_results(futs)
    assert 1 == num_added


def test_resultwatcher_cancels_futures_on_unexpected_stop(mocker):
    mocker.patch(f"{_MOCK_BASE}time")
    rw = _ResultWatcher(MockedExecutor())
    rw._connect = mock.Mock(return_value=mock.Mock(spec=pika.SelectConnection))

    fut = ComputeFuture(task_id=str(uuid.uuid4()))
    rw.watch_for_task_results([fut])
    rw.run()

    assert "thread quit" in str(fut.exception())


def test_resultwatcher_gracefully_handles_unexpected_exception(mocker, mock_log):
    mocker.patch(f"{_MOCK_BASE}time")
    rw = _ResultWatcher(MockedExecutor())
    rw._connect = mock.Mock(return_value=mock.Mock(spec=pika.SelectConnection))
    rw._event_watcher = mock.Mock(side_effect=Exception)

    rw.run()

    assert mock_log.exception.call_count > 2
    args, _kwargs = mock_log.exception.call_args
    assert "shutting down" in args[0]


def test_resultwatcher_blocks_until_tasks_done():
    fut = ComputeFuture(task_id=str(uuid.uuid4()))
    mrw = MockedResultWatcher()
    mrw.watch_for_task_results([fut])
    mrw.start()

    res = Result(task_id=fut.task_id, data="abc123")
    mrw._received_results[fut.task_id] = (None, res)

    mrw.shutdown(wait=False)
    try_assert(lambda: not mrw._time_to_stop, timeout_ms=1000)
    mrw._match_results_to_futures()
    try_assert(lambda: mrw._time_to_stop)


def test_resultwatcher_does_not_check_if_no_results():
    fut = ComputeFuture(task_id=str(uuid.uuid4()))
    mrw = MockedResultWatcher()
    mrw._match_results_to_futures = mock.Mock()
    mrw.watch_for_task_results([fut])
    mrw.start()
    mrw._event_watcher()

    mrw._match_results_to_futures.assert_not_called()
    mrw.shutdown(cancel_futures=True)


def test_resultwatcher_checks_match_if_results():
    fut = ComputeFuture(task_id=str(uuid.uuid4()))
    res = Result(task_id=fut.task_id, data="abc123")

    mrw = MockedResultWatcher()
    mrw._received_results[fut.task_id] = (None, res)

    mrw.watch_for_task_results([fut])
    mrw.start()
    mrw._event_watcher()

    assert fut.done() and not fut.cancelled()
    assert fut.result() is not None
    mrw.shutdown(cancel_futures=True)


def test_resultwatcher_repr():
    mrw = MockedResultWatcher()
    assert "<✗;" in repr(mrw)
    mrw._consumer_tag = "asdf"
    assert "<✓;" in repr(mrw)
    mrw._consumer_tag = None
    assert "<✗;" in repr(mrw)

    for i in range(10):
        assert f"fut={i};" in repr(mrw)
        mrw._open_futures[f"a{i}"] = 1
    mrw._open_futures.update({f"b{i}": 1 for i in range(1000)})
    assert "fut=1,010; " in repr(mrw), "includes separator"

    for i in range(10):
        assert f"res={i};" in repr(mrw)
        mrw._received_results[f"a{i}"] = 1
    mrw._received_results.update({f"b{i}": 1 for i in range(1000)})
    assert "res=1,010; " in repr(mrw), "includes separator"

    assert "; qp=-" in repr(mrw), "blank queue prefix shown with dash (-)"
    mrw._queue_prefix = "abc"
    assert f"; qp={mrw._queue_prefix}" in repr(mrw)


def test_resultwatcher_match_sets_exception(randomstring):
    payload = randomstring()
    fxs = ComputeSerializer()
    fut = ComputeFuture(task_id=str(uuid.uuid4()))
    err_details = ResultErrorDetails(code="1234", user_message="some_user_message")
    res = Result(task_id=fut.task_id, error_details=err_details, data=payload)

    mrw = MockedResultWatcher()
    mrw.funcx_executor.client.fx_serializer.deserialize = fxs.deserialize
    mrw._received_results[fut.task_id] = (mock.Mock(timestamp=5), res)
    mrw.watch_for_task_results([fut])
    mrw.start()
    mrw._event_watcher()

    assert payload in str(fut.exception())
    assert isinstance(fut.exception(), TaskExecutionFailed)
    mrw.shutdown()


def test_resultwatcher_match_sets_result(randomstring):
    payload = randomstring()
    fxs = ComputeSerializer()
    fut = ComputeFuture(task_id=str(uuid.uuid4()))
    res = Result(task_id=fut.task_id, data=fxs.serialize(payload))

    mrw = MockedResultWatcher()
    mrw.funcx_executor.client.fx_serializer.deserialize = fxs.deserialize
    mrw._received_results[fut.task_id] = (None, res)
    mrw.watch_for_task_results([fut])
    mrw.start()
    mrw._event_watcher()

    assert fut.result() == payload
    mrw.shutdown()


def test_resultwatcher_match_handles_deserialization_error():
    invalid_payload = "invalidly serialized"
    fxs = ComputeSerializer()
    fut = ComputeFuture(task_id=str(uuid.uuid4()))
    res = Result(task_id=fut.task_id, data=invalid_payload)

    mrw = MockedResultWatcher()
    mrw.funcx_executor.client.fx_serializer.deserialize = fxs.deserialize
    mrw._received_results[fut.task_id] = (None, res)
    mrw.watch_for_task_results([fut])
    mrw.start()
    mrw._event_watcher()

    exc = fut.exception()
    assert "Malformed or unexpected data structure" in str(exc)
    assert invalid_payload in str(exc)
    mrw.shutdown()


def test_resultwatcher_match_calls_log_version_mismatch(randomstring):
    payload = randomstring()
    fxs = ComputeSerializer()
    fut = ComputeFuture(task_id=str(uuid.uuid4()))
    res = Result(task_id=fut.task_id, data=fxs.serialize(payload))

    mrw = MockedResultWatcher()
    mrw.funcx_executor.client.fx_serializer.deserialize = fxs.deserialize
    mrw._received_results[fut.task_id] = (None, res)
    mrw.watch_for_task_results([fut])
    mrw.start()
    mrw._event_watcher()

    assert fut.result() == payload
    assert mrw.funcx_executor.client._log_version_mismatch.called
    mrw.shutdown()


@pytest.mark.parametrize("unpacked", ("not_a_Result", Exception))
def test_resultwatcher_onmessage_verifies_result_type(mocker, unpacked):
    mock_unpack = mocker.patch(f"{_MOCK_BASE}messagepack.unpack")

    mock_unpack.side_effect = unpacked
    mock_channel = mock.Mock()
    mock_deliver = mock.Mock()
    mock_props = mock.Mock()
    mrw = MockedResultWatcher()
    mrw._on_message(mock_channel, mock_deliver, mock_props, b"some_bytes")
    mock_channel.basic_nack.assert_called()
    assert not mrw._received_results


def test_resultwatcher_onmessage_sets_check_results_flag():
    res = Result(task_id=uuid.uuid4(), data="abc")

    mock_channel = mock.Mock()
    mock_deliver = mock.Mock()
    mock_props = mock.Mock()
    mrw = MockedResultWatcher()
    mrw._on_message(mock_channel, mock_deliver, mock_props, messagepack.pack(res))
    mock_channel.basic_nack.assert_not_called()
    assert mrw._received_results
    assert mrw._time_to_check_results.is_set()


@pytest.mark.parametrize("exc", (MemoryError("some description"), "some description"))
def test_resultwatcher_stops_loop_on_open_failure(mock_log, exc):
    mrw = MockedResultWatcher()
    mrw.start()
    assert not mrw._connection.ioloop.stop.called, "Test setup verification"

    while not mrw._cancellation_reason:
        mrw._connection_tries += 1

        assert not mrw._cancellation_reason, "Test setup verification"
        assert not mock_log.warning.called
        mrw._connection.ioloop.stop.reset_mock()
        mock_log.debug.reset_mock()

        mrw._on_open_failed(mock.Mock(), exc)  # kernel of test

        assert mrw._connection.ioloop.stop.called
        assert mock_log.debug.called
        log_args, *_ = mock_log.debug.call_args
        assert "Failed to open connection" in log_args[0]

    assert mrw._connection_tries == mrw.connect_attempt_limit
    assert mock_log.warning.called, "Expected warning only on watcher quit"
    assert "some description" in str(mrw._cancellation_reason)
    mrw.shutdown()


def test_resultwatcher_connection_closed_stops_loop():
    exc = MemoryError("some description")
    mrw = MockedResultWatcher()
    mrw.start()
    mrw._connection.ioloop.stop.assert_not_called()
    mrw._on_connection_closed(mock.Mock(), exc)
    mrw._connection.ioloop.stop.assert_called()
    mrw.shutdown()


def test_resultwatcher_channel_closed_retries_then_shuts_down():
    exc = Exception("some pika reason")
    mrw = MockedResultWatcher()
    mrw.start()
    for i in range(1, mrw.channel_close_window_limit):
        mrw._connection.ioloop.call_later.reset_mock()
        mrw._on_channel_closed(mock.Mock(), exc)
        assert len(mrw._channel_closes) == i
    assert not mrw._closed
    mrw._on_channel_closed(mock.Mock(), exc)
    assert mrw._closed

    # and finally, no error if we call "too many" times
    mrw._on_channel_closed(mock.Mock(), exc)


def test_resultwatcher_connection_opened_resets_fail_counter():
    mrw = MockedResultWatcher()
    mrw.start()
    mrw._connection_tries = 57
    mrw._on_connection_open(None)
    assert mrw._connection_tries == 0
    mrw.shutdown()


def test_resultwatcher_channel_opened_starts_consuming():
    mock_channel = mock.Mock()
    mrw = MockedResultWatcher()
    mrw.start()
    assert mrw._consumer_tag is None
    mrw._on_channel_open(mock_channel)
    assert mock_channel is mrw._channel
    assert mrw._consumer_tag is not None
    mrw.shutdown()


def test_resultwatcher_amqp_acks_in_bulk():
    mrw = MockedResultWatcher()
    mrw.start()
    mrw._to_ack.extend(range(200))
    assert mrw._channel.basic_ack.call_count == 0
    mrw._event_watcher()
    assert not mrw._to_ack
    assert mrw._channel.basic_ack.call_count == 1
    mrw.shutdown()


def test_result_queue_watcher_custom_port(mocker, gc_executor):
    gcc, gce = gc_executor
    custom_port_no = random.randint(1, 65536)
    rw = _ResultWatcher(gce, port=custom_port_no)
    gcc.get_result_amqp_url.return_value = {
        "queue_prefix": "",
        "connection_url": "amqp://some.address:1111",
    }
    connect = mocker.patch(f"{_MOCK_BASE}pika.SelectConnection")

    rw._connect()

    assert connect.call_args[0][0].port == custom_port_no
