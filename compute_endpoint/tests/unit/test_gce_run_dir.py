import os.path
import uuid
from unittest import mock

from globus_compute_endpoint.engines.globus_compute import GlobusComputeEngine
from parsl.executors import HighThroughputExecutor


def test_default_working_dir(tmp_path):
    """Verify that working dir is set to tasks dir in the run_dir by default"""
    gce = GlobusComputeEngine(
        address="127.0.0.1",
        working_dir=None,
    )
    gce.executor.start = mock.MagicMock(spec=HighThroughputExecutor)
    gce.start(endpoint_id=uuid.uuid4(), run_dir=tmp_path)
    assert gce.executor.run_dir == tmp_path
    assert gce.working_dir == os.path.join(tmp_path, "TASKS")


def test_relative_working_dir(tmp_path):
    """Test working_dir relative to run_dir"""
    gce = GlobusComputeEngine(
        address="127.0.0.1",
        working_dir="relative_path",
    )
    gce.executor.start = mock.MagicMock(spec=HighThroughputExecutor)
    gce.start(endpoint_id=uuid.uuid4(), run_dir=tmp_path)
    assert gce.run_dir == gce.executor.run_dir
    assert gce.working_dir == os.path.join(tmp_path, "relative_path")


def test_absolute_working_dir(tmp_path):
    """Test absolute path for working_dir"""
    gce = GlobusComputeEngine(
        address="127.0.0.1",
        working_dir="/absolute/path",
    )
    gce.executor.start = mock.MagicMock(spec=HighThroughputExecutor)
    gce.start(endpoint_id=uuid.uuid4(), run_dir=tmp_path)
    assert gce.run_dir == gce.executor.run_dir
    assert gce.working_dir == "/absolute/path"


def test_submit_pass(tmp_path):
    """Test absolute path for working_dir"""
    gce = GlobusComputeEngine(
        address="127.0.0.1",
        working_dir=None,
    )
    gce.executor.start = mock.Mock(spec=HighThroughputExecutor)
    gce.executor.submit = mock.Mock(spec=HighThroughputExecutor.start)
    gce.start(endpoint_id=uuid.uuid4(), run_dir=tmp_path)

    gce.submit(
        task_id=uuid.uuid4(),
        packed_task=b"PACKED_TASK",
        resource_specification={},
    )

    gce.executor.submit.assert_called()
    flag = False
    for call in gce.executor.submit.call_args_list:
        args, kwargs = call
        assert "run_dir" in kwargs
        assert kwargs["run_dir"] == os.path.join(tmp_path, "TASKS")
        flag = True
    assert flag, "Call args parsing failed, did not find run_dir"
