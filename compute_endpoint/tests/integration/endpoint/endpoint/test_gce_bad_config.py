import uuid
from unittest import mock

import pytest
from globus_compute_endpoint.engines.globus_compute import GlobusComputeEngine
from parsl.executors.errors import BadStateException
from parsl.providers import SlurmProvider
from tests.utils import double


def test_provider_fail_at_init(tmp_path):
    """Test to confirm engine reports error when wrong provider is used

    init_blocks = 1
    """

    with mock.patch(
        "parsl.providers.SlurmProvider.status_polling_interval",
        new_callable=mock.PropertyMock,
    ) as mock_prop:
        mock_prop.return_value = 1
        gce = GlobusComputeEngine(
            provider=SlurmProvider(init_blocks=1),
            job_status_kwargs={"max_idletime": 0.1, "strategy_period": 0.1},
        )
        gce.start(endpoint_id=uuid.uuid4(), run_dir=tmp_path)

    assert gce.bad_state_is_set is False, "Executor should be clean at test-start"

    def double(x):
        return x * 2

    future = gce.executor.submit(double, {}, 5)

    with pytest.raises(BadStateException):
        future.result()

    assert gce.bad_state_is_set is True, "Executor should be in failed state"
    exception_str = str(future.exception())
    # There are Mac/Linux variations in the exception_str
    # that the following tests work around
    assert "127" in exception_str
    assert "sbatch" in exception_str
    assert "Could not read job ID from submit command standard output" in exception_str
    assert "not found" in exception_str
    gce.shutdown()


def test_provider_fail_at_scaling(tmp_path):
    """Test to confirm engine reports error when wrong provider is used
    init_blocks = 0
    This is a slow test because the pollers run at 5s intervals
    """

    with mock.patch(
        "parsl.providers.SlurmProvider.status_polling_interval",
        new_callable=mock.PropertyMock,
    ) as mock_prop:
        mock_prop.return_value = 1
        gce = GlobusComputeEngine(
            provider=SlurmProvider(init_blocks=0),
            job_status_kwargs={"max_idletime": 0.1, "strategy_period": 0.1},
        )
        gce.start(endpoint_id=uuid.uuid4(), run_dir=tmp_path)

    assert gce.bad_state_is_set is False

    future = gce.executor.submit(double, {}, 5)

    with pytest.raises(BadStateException):
        future.result(timeout=30)
    exception_str = str(future.exception())
    assert "127" in exception_str
    assert "sbatch"
    assert "Could not read job ID from submit command standard output" in exception_str
    assert "not found" in exception_str
    gce.shutdown()
