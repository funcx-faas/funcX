import uuid

import pytest
from globus_compute_endpoint.engines import GlobusComputeEngine

_LAUNCH_CMD_PREFIX = (
    "globus-compute-endpoint python-exec"
    " parsl.executors.high_throughput.process_worker_pool"
)


def platinfo():
    import platform
    import sys

    return platform.uname(), sys.version_info


def test_docker(tmp_path):
    gce = GlobusComputeEngine(
        worker_debug=True,
        address="127.0.0.1",
        label="GCE_TEST",
        container_type="docker",
        container_uri="funcx/kube-endpoint:main-3.10",
        container_cmd_options="--FABRICATED",
    )
    gce.start(endpoint_id=uuid.uuid4(), run_dir="/tmp")
    container_launch_cmd = gce.executor.launch_cmd
    expected = (
        "docker run --FABRICATED -v /tmp:/tmp -t "
        f"funcx/kube-endpoint:main-3.10 {_LAUNCH_CMD_PREFIX} --debug"
    )
    assert container_launch_cmd.startswith(expected)

    gce.shutdown()


def test_apptainer(tmp_path):
    gce = GlobusComputeEngine(
        worker_debug=True,
        address="127.0.0.1",
        label="GCE_TEST",
        container_type="apptainer",
        container_uri="APPTAINER_PATH",
        container_cmd_options="--FABRICATED",
    )
    gce.start(endpoint_id=uuid.uuid4(), run_dir="/tmp")
    container_launch_cmd = gce.executor.launch_cmd
    expected = f"apptainer run --FABRICATED APPTAINER_PATH {_LAUNCH_CMD_PREFIX} --debug"
    assert container_launch_cmd.startswith(expected)

    gce.shutdown()


def test_singularity(tmp_path):
    gce = GlobusComputeEngine(
        worker_debug=True,
        address="127.0.0.1",
        max_workers=1,
        label="GCE_TEST",
        container_type="singularity",
        container_uri="/home/yadunand/kube-endpoint.py3.9.sif",
        container_cmd_options="",
    )
    gce.start(endpoint_id=uuid.uuid4(), run_dir="/tmp")
    container_launch_cmd = gce.executor.launch_cmd
    expected = (
        "singularity run /home/yadunand/kube-endpoint.py3.9.sif"
        f" {_LAUNCH_CMD_PREFIX} --debug"
    )
    assert container_launch_cmd.startswith(expected)

    gce.shutdown()


def test_custom_missing_options(tmp_path):
    gce = GlobusComputeEngine(
        address="127.0.0.1",
        max_workers=1,
        label="GCE_TEST",
        container_type="custom",
    )
    with pytest.raises(AssertionError):
        gce.start(endpoint_id=uuid.uuid4(), run_dir="/tmp")


def test_custom(tmp_path):
    gce = GlobusComputeEngine(
        address="127.0.0.1",
        max_workers=1,
        label="GCE_TEST",
        container_type="custom",
        container_cmd_options="FOO {EXECUTOR_RUNDIR} {EXECUTOR_LAUNCH_CMD}",
    )

    gce.start(endpoint_id=uuid.uuid4(), run_dir="/tmp")

    container_launch_cmd = gce.executor.launch_cmd
    expected = f"FOO {gce.run_dir} {_LAUNCH_CMD_PREFIX}"
    assert container_launch_cmd.startswith(expected)

    gce.shutdown()
