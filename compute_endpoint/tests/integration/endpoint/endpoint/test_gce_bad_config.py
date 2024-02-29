import uuid
from unittest import mock

import pytest
from globus_compute_endpoint.engines.globus_compute import GlobusComputeEngine
from parsl.executors.errors import BadStateException
from parsl.providers import SlurmProvider


def test_provider_fail_at_init(tmp_path):
    """Test to confirm engine reports error when wrong provider is used

    init_blocks = 1
    """

    gce = None
    with mock.patch(
        "parsl.providers.SlurmProvider.status_polling_interval",
        new_callable=mock.PropertyMock,
    ) as mock_prop:
        mock_prop.return_value = 1
        gce = GlobusComputeEngine(provider=SlurmProvider(init_blocks=1))
        gce.start(endpoint_id=uuid.uuid4(), run_dir=tmp_path)

    assert gce.bad_state_is_set is False

    def double(x):
        return x * 2

    future = gce.executor.submit(double, {}, 5)

    with pytest.raises(BadStateException):
        future.result()
    exception_str = str(future.exception())
    assert "sbatch: command not found" in exception_str
    gce.shutdown()


def test_provider_fail_at_scaling(tmp_path):
    """Test to confirm engine reports error when wrong provider is used
    init_blocks = 0
    This is a slow test because the pollers run at 5s intervals
    """

    gce = None
    with mock.patch(
        "parsl.providers.SlurmProvider.status_polling_interval",
        new_callable=mock.PropertyMock,
    ) as mock_prop:
        mock_prop.return_value = 1
        gce = GlobusComputeEngine(provider=SlurmProvider(init_blocks=0))
        gce.start(endpoint_id=uuid.uuid4(), run_dir=tmp_path)

    assert gce.bad_state_is_set is False

    def double(x):
        return x * 2

    future = gce.executor.submit(double, {}, 5)

    with pytest.raises(BadStateException):
        future.result(timeout=30)
    exception_str = str(future.exception())
    assert "sbatch: command not found" in exception_str
    gce.shutdown()
