import os
import pathlib
import pickle
import queue
import random
import threading
import uuid

import pytest
from globus_compute_common.messagepack import pack, unpack
from globus_compute_common.messagepack.message_types import EPStatusReport, Result, Task
from globus_compute_endpoint import engines
from globus_compute_endpoint.cli import get_config
from globus_compute_endpoint.endpoint.endpoint import Endpoint
from globus_compute_endpoint.endpoint.interchange import EndpointInterchange, log
from globus_compute_endpoint.endpoint.rabbit_mq import ResultPublisher
from globus_compute_endpoint.endpoint.utils.config import Config
from tests.utils import try_assert

_MOCK_BASE = "globus_compute_endpoint.endpoint.interchange."


@pytest.fixture
def funcx_dir(tmp_path):
    fxdir = tmp_path / pathlib.Path("funcx")
    fxdir.mkdir()
    yield fxdir


@pytest.fixture(autouse=True)
def reset_signals_auto(reset_signals):
    yield


@pytest.fixture(autouse=True)
def mock_spt(mocker):
    yield mocker.patch(f"{_MOCK_BASE}setproctitle.setproctitle")


@pytest.fixture
def mock_quiesce(mocker):
    quiesce_mock_wait = False

    def mock_set():
        nonlocal quiesce_mock_wait
        quiesce_mock_wait = True

    def mock_is_set():
        nonlocal quiesce_mock_wait
        return quiesce_mock_wait

    def mock_wait(*a, **k):
        return quiesce_mock_wait

    m = mocker.Mock(spec=threading.Event)
    m.wait.side_effect = mock_wait
    m.set.side_effect = mock_set
    m.is_set.side_effect = mock_is_set
    yield m


def test_endpoint_id_conveyed_to_executor(funcx_dir):
    manager = Endpoint()
    config_dir = funcx_dir / "mock_endpoint"
    expected_ep_id = str(uuid.uuid1())

    manager.configure_endpoint(config_dir, None)

    endpoint_config = get_config(pathlib.Path(config_dir))
    endpoint_config.executors[0].passthrough = False

    ic = EndpointInterchange(
        endpoint_config,
        reg_info={"task_queue_info": {}, "result_queue_info": {}},
        endpoint_id=expected_ep_id,
    )
    ic.executor = engines.ThreadPoolEngine()  # test does not need a child process
    ic.start_engine()
    assert ic.executor.endpoint_id == expected_ep_id
    ic.executor.shutdown()


def test_start_requires_pre_registered(mocker, funcx_dir):
    with pytest.raises(TypeError):
        EndpointInterchange(
            config=Config(executors=[mocker.Mock()]),
            reg_info=None,
            endpoint_id="mock_endpoint_id",
        )


@pytest.mark.skip("EPInterchange is no longer unpacking a task")
def test_invalid_task_received(mocker, endpoint_uuid):
    reg_info = {"task_queue_info": {}, "result_queue_info": {}}
    conf = Config(executors=[mocker.Mock(endpoint_id=endpoint_uuid)])
    ei = EndpointInterchange(endpoint_id=endpoint_uuid, config=conf, reg_info=reg_info)

    mock_results = mocker.MagicMock()
    mocker.patch(f"{_MOCK_BASE}ResultQueuePublisher", return_value=mock_results)
    mocker.patch(f"{_MOCK_BASE}convert_to_internaltask", side_effect=Exception("BLAR"))
    task = Task(task_id=uuid.uuid4(), task_buffer="")
    ei.pending_task_queue.put([{}, pack(task)])
    t = threading.Thread(target=ei._main_loop, daemon=True)
    t.start()

    try_assert(lambda: mock_results.publish.called)
    ei.time_to_quit = True
    t.join()

    assert mock_results.publish.called
    msg = mock_results.publish.call_args_list[0][0][0]
    result: Result = unpack(msg)
    assert result.task_id == task.task_id
    assert "Failed to start task" in result.data


@pytest.mark.skip("EPInterchange no longer unpacks result body")
def test_invalid_result_received(mocker, endpoint_uuid):
    mock_rqp = mocker.Mock(spec=ResultPublisher)
    mocker.patch(f"{_MOCK_BASE}ResultQueuePublisher", return_value=mock_rqp)

    reg_info = {"task_queue_info": {}, "result_queue_info": {}}
    conf = Config(executors=[mocker.Mock(endpoint_id=endpoint_uuid)])
    ei = EndpointInterchange(endpoint_id=endpoint_uuid, config=conf, reg_info=reg_info)
    res = {
        "task_id": endpoint_uuid,
        "not_data_field": bytes(i for i in range(20)),  # invalid utf8; not a string
    }
    task_id = str(uuid.uuid4())
    result_bytes = pickle.dumps(res)
    ei.results_passthrough.put({"task_id": task_id, "message": result_bytes})
    t = threading.Thread(target=ei._main_loop, daemon=True)
    t.start()

    try_assert(lambda: mock_rqp.publish.call_count > 1)
    ei.time_to_quit = True
    t.join()

    a = next(a[0] for a, _ in mock_rqp.publish.call_args_list if b"Task ID:" in a[0])
    res = unpack(a)

    assert "(KeyError)" in res.data, "Expect user-facing data contains exception type"
    assert task_id in res.data, "Expect user-facing data contains task id"


def test_die_with_parent_refuses_to_start_if_not_parent(mocker):
    ei = EndpointInterchange(
        config=Config(executors=[mocker.Mock()]),
        reg_info={"task_queue_info": {}, "result_queue_info": {}},
        parent_pid=os.getpid(),  # _not_ ppid; that's the test.
    )
    mock_warn = mocker.patch.object(log, "warning")
    assert not ei.time_to_quit, "Verify test setup"
    ei.start()
    assert ei.time_to_quit

    warn_msg = str(list(a[0] for a, _ in mock_warn.call_args_list))
    assert "refusing to start" in warn_msg


def test_die_with_parent_goes_away_if_parent_dies(mocker):
    ppid = os.getppid()

    mocker.patch(f"{_MOCK_BASE}ResultPublisher")
    mocker.patch(f"{_MOCK_BASE}time.sleep")
    mock_ppid = mocker.patch(f"{_MOCK_BASE}os.getppid")
    mock_ppid.side_effect = (ppid, 1)
    ei = EndpointInterchange(
        config=Config(executors=[mocker.Mock()]),
        reg_info={"task_queue_info": {}, "result_queue_info": {}},
        parent_pid=ppid,
    )
    ei.executors = {"mock_executor": mocker.Mock()}
    mock_warn = mocker.patch.object(log, "warning")
    assert not ei.time_to_quit, "Verify test setup"
    ei.start()
    assert ei.time_to_quit

    warn_msg = str(list(a[0] for a, _ in mock_warn.call_args_list))
    assert "refusing to start" not in warn_msg
    assert f"Parent ({ppid}) has gone away" in warn_msg


def test_no_idle_if_not_configured(mocker, endpoint_uuid, mock_spt, mock_quiesce):
    mock_log = mocker.patch(f"{_MOCK_BASE}log")
    mocker.patch(f"{_MOCK_BASE}ResultPublisher")

    reg_info = {"task_queue_info": {}, "result_queue_info": {}}
    mock_ex = mocker.Mock(endpoint_id=endpoint_uuid)
    mock_ex.executor_exception = None
    conf = Config(
        executors=[mock_ex],
        heartbeat_period=1,
        idle_heartbeats_soft=0,
    )
    ei = EndpointInterchange(endpoint_id=endpoint_uuid, config=conf, reg_info=reg_info)
    ei.results_passthrough = mocker.Mock(spec=queue.Queue)
    ei.results_passthrough.get.side_effect = queue.Empty
    ei.pending_task_queue = mocker.Mock(spec=queue.Queue)
    ei.pending_task_queue.get.side_effect = queue.Empty
    ei._quiesce_event = mock_quiesce

    t = threading.Thread(target=ei._main_loop, daemon=True)
    t.start()

    try_assert(lambda: mock_log.debug.call_count > 500)
    ei.time_to_quit = True
    t.join()
    assert not mock_spt.called


@pytest.mark.parametrize("idle_limit", (random.randint(2, 100),))
def test_soft_idle_honored(mocker, endpoint_uuid, mock_spt, idle_limit, mock_quiesce):
    result = Result(task_id=uuid.uuid1(), data=b"TASK RESULT")
    msg = {"task_id": str(result.task_id), "message": pack(result)}

    mock_log = mocker.patch(f"{_MOCK_BASE}log")
    mocker.patch(f"{_MOCK_BASE}ResultPublisher")

    reg_info = {"task_queue_info": {}, "result_queue_info": {}}
    mock_ex = mocker.Mock(endpoint_id=endpoint_uuid)
    mock_ex.executor_exception = None
    conf = Config(executors=[mock_ex], idle_heartbeats_soft=idle_limit)
    ei = EndpointInterchange(endpoint_id=endpoint_uuid, config=conf, reg_info=reg_info)

    ei.results_passthrough = mocker.Mock(spec=queue.Queue)
    ei.results_passthrough.get.side_effect = (msg, queue.Empty)
    ei.pending_task_queue = mocker.Mock(spec=queue.Queue)
    ei.pending_task_queue.get.side_effect = queue.Empty

    ei._quiesce_event = mock_quiesce
    ei._main_loop()

    assert ei.time_to_quit is True

    log_args = [a[0] for a, _k in mock_log.info.call_args_list]
    transition_count = sum("In idle state" in m for m in log_args)
    assert transition_count == 1, f"expected logs not spammed -- {log_args}"

    shut_down_s = f"{(idle_limit - 1) * conf.heartbeat_period:,}"
    idle_msg = next(m for m in log_args if "In idle state" in m)
    assert "due to" in idle_msg, "expected to find reason"
    assert "idle_heartbeats_soft" in idle_msg, "expected to find setting name"
    assert f" shut down in {shut_down_s}" in idle_msg, "expected to find timeout time"

    idle_msg = next(m for m in log_args if "Idle heartbeats reached." in m)
    assert "Shutting down" in idle_msg, "expected to find action taken"

    num_updates = sum(
        m[0][0].startswith("[idle; shut down in ") for m in mock_spt.call_args_list
    )
    assert num_updates == idle_limit, "expect process title updated; reflects status"


@pytest.mark.parametrize("idle_limit", (random.randint(4, 100),))
def test_hard_idle_honored(mocker, endpoint_uuid, mock_spt, idle_limit, mock_quiesce):
    idle_soft_limit = random.randrange(2, idle_limit)

    mock_log = mocker.patch(f"{_MOCK_BASE}log")
    mocker.patch(f"{_MOCK_BASE}ResultPublisher")
    mocker.patch(f"{_MOCK_BASE}threading.Thread")

    reg_info = {"task_queue_info": {}, "result_queue_info": {}}
    mock_ex = mocker.Mock(endpoint_id=endpoint_uuid)
    mock_ex.executor_exception = None
    conf = Config(
        executors=[mock_ex],
        idle_heartbeats_soft=idle_soft_limit,
        idle_heartbeats_hard=idle_limit,
    )
    ei = EndpointInterchange(endpoint_id=endpoint_uuid, config=conf, reg_info=reg_info)
    ei._quiesce_event = mock_quiesce

    ei._main_loop()

    log_args = [m[0][0] for m in mock_log.info.call_args_list]
    transition_count = sum("Possibly idle" in m for m in log_args)
    assert transition_count == 1, "expected logs not spammed"

    shut_down_s = f"{(idle_limit - idle_soft_limit - 1) * conf.heartbeat_period:,}"
    idle_msg = next(m for m in log_args if "Possibly idle" in m)
    assert "idle_heartbeats_hard" in idle_msg, "expected to find setting name"
    assert f" shut down in {shut_down_s}" in idle_msg, "expected to find timeout time"

    idle_msg = mock_log.warning.call_args[0][0]
    assert "Shutting down" in idle_msg, "expected to find action taken"
    assert "HARD limit" in idle_msg

    num_updates = sum(
        m[0][0].startswith("[possibly idle; shut down in ")
        for m in mock_spt.call_args_list
    )
    assert (
        num_updates == idle_limit - idle_soft_limit
    ), "expect process title updated; reflects status"


def test_unidle_updates_proc_title(mocker, endpoint_uuid, mock_spt, mock_quiesce):
    mock_log = mocker.patch(f"{_MOCK_BASE}log")
    mocker.patch(f"{_MOCK_BASE}ResultPublisher")

    reg_info = {"task_queue_info": {}, "result_queue_info": {}}
    mock_ex = mocker.Mock(endpoint_id=endpoint_uuid)
    mock_ex.executor_exception = None
    conf = Config(
        executors=[mock_ex],
        heartbeat_period=1,
        idle_heartbeats_soft=1,
        idle_heartbeats_hard=3,
    )
    ei = EndpointInterchange(endpoint_id=endpoint_uuid, config=conf, reg_info=reg_info)
    ei._quiesce_event = mock_quiesce
    ei.results_passthrough = mocker.Mock(spec=queue.Queue)
    ei.results_passthrough.get.side_effect = queue.Empty
    ei.pending_task_queue = mocker.Mock(spec=queue.Queue)
    ei.pending_task_queue.get.side_effect = queue.Empty

    main_thread_may_continue = threading.Event()

    def return_msg_set_empty():
        result = Result(task_id=uuid.uuid1(), data=b"TASK RESULT")
        yield {"task_id": str(result.task_id), "message": pack(result)}
        main_thread_may_continue.set()
        ei.results_passthrough.get.side_effect = queue.Empty
        raise queue.Empty

    def insert_msg(*a, **k):
        ei.results_passthrough.get.side_effect = return_msg_set_empty()
        main_thread_may_continue.wait()

    mock_spt.side_effect = insert_msg

    ei._main_loop()

    msg = next(m[0][0] for m in mock_log.info.call_args_list if "Moved to" in m[0][0])
    assert msg.startswith("Moved to active state"), "expect why state changed"
    assert "due to " in msg

    first, *middle, last = (ca[0][0] for ca in mock_spt.call_args_list)
    assert first.startswith("[possibly idle; shut down in ")
    assert not any(
        "idle; " in m for m in middle
    ), "expected proc title set back when not idle"
    assert last.startswith("[idle; shut down in ")


def test_sends_final_status_message_on_shutdown(mocker, endpoint_uuid, mock_quiesce):
    mock_rqp = mocker.Mock(spec=ResultPublisher)
    mocker.patch(f"{_MOCK_BASE}ResultPublisher", return_value=mock_rqp)

    reg_info = {"task_queue_info": {}, "result_queue_info": {}}
    mock_ex = mocker.Mock(endpoint_id=endpoint_uuid)
    mock_ex.executor_exception = None
    conf = Config(
        executors=[mock_ex],
        idle_heartbeats_soft=1,
        idle_heartbeats_hard=2,
    )
    ei = EndpointInterchange(endpoint_id=endpoint_uuid, config=conf, reg_info=reg_info)
    ei.results_passthrough = mocker.Mock(spec=queue.Queue)
    ei.results_passthrough.get.side_effect = queue.Empty
    ei.pending_task_queue = mocker.Mock(spec=queue.Queue)
    ei.pending_task_queue.get.side_effect = queue.Empty
    ei._quiesce_event = mock_quiesce
    ei._main_loop()

    assert mock_rqp.publish.called
    packed_bytes = mock_rqp.publish.call_args[0][0]
    epsr = unpack(packed_bytes)
    assert isinstance(epsr, EPStatusReport)
    assert epsr.endpoint_id == uuid.UUID(endpoint_uuid)
    assert epsr.global_state["heartbeat_period"] == 0


def test_faithfully_handles_status_report_messages(
    mocker, endpoint_uuid, randomstring, mock_quiesce
):
    mock_rqp = mocker.Mock(spec=ResultPublisher)
    mocker.patch(f"{_MOCK_BASE}ResultPublisher", return_value=mock_rqp)

    status_report = EPStatusReport(
        endpoint_id=endpoint_uuid, global_state={"sentinel": "foo"}, task_statuses=[]
    )
    status_report_msg = {"message": pack(status_report)}
    reg_info = {"task_queue_info": {}, "result_queue_info": {}}
    mock_ex = mocker.Mock(endpoint_id=endpoint_uuid)
    mock_ex.executor_exception = None
    conf = Config(executors=[mock_ex])
    ei = EndpointInterchange(endpoint_id=endpoint_uuid, config=conf, reg_info=reg_info)
    ei.results_passthrough = mocker.Mock(spec=queue.Queue)
    ei.results_passthrough.get.side_effect = (status_report_msg, queue.Empty)
    ei.pending_task_queue = mocker.Mock(spec=queue.Queue)
    ei.pending_task_queue.get.side_effect = queue.Empty
    ei._quiesce_event = mock_quiesce

    t = threading.Thread(target=ei._main_loop, daemon=True)
    t.start()

    try_assert(lambda: mock_rqp.publish.called)
    ei.time_to_quit = True
    t.join()

    assert mock_rqp.publish.call_count > 1, "Test packet, then the final status report"
    packed_bytes = mock_rqp.publish.call_args_list[0][0][0]
    found_epsr = unpack(packed_bytes)
    assert isinstance(found_epsr, EPStatusReport)
    assert found_epsr.endpoint_id == uuid.UUID(endpoint_uuid)
    assert found_epsr.global_state["sentinel"] == status_report.global_state["sentinel"]
