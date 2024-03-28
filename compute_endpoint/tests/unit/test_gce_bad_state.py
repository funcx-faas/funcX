import uuid

import pytest
from globus_compute_common import messagepack
from globus_compute_endpoint.engines.globus_compute import GlobusComputeEngine


def test_bad_state():
    gce = GlobusComputeEngine()
    assert gce.bad_state_is_set is False

    gce.executor.set_bad_state_and_fail_all(ZeroDivisionError())

    assert gce.bad_state_is_set is True

    with pytest.raises(ZeroDivisionError):
        raise gce.executor.executor_exception


def test_exception_report_from_bad_state():

    gce = GlobusComputeEngine()
    queue = gce.results_passthrough
    assert gce.bad_state_is_set is False
    gce.executor.set_bad_state_and_fail_all(ZeroDivisionError())
    assert gce.bad_state_is_set is True

    task_id = uuid.uuid4()
    future = gce.submit(task_id=task_id, packed_task=b"MOCK_PACKED_TASK")

    with pytest.raises(ZeroDivisionError):
        future.result()

    flag = False
    for _i in range(10):
        q_msg = queue.get(timeout=5)
        assert isinstance(q_msg, dict)

        packed_result_q = q_msg["message"]
        result = messagepack.unpack(packed_result_q)
        if isinstance(result, messagepack.message_types.Result):
            assert result.task_id == task_id
            assert result.error_details.code == "RemoteExecutionError"
            assert "ZeroDivisionError" in result.data
            flag = True
            break

    assert flag, "Expected result packet, but none received"
