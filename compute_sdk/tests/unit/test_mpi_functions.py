import os

import pytest
from globus_compute_sdk.sdk.mpi_function import MPIFunction


@pytest.fixture
def run_in_tmp_dir(tmp_path):
    os.chdir(tmp_path)
    return tmp_path


def test_mpi_function(run_in_tmp_dir):

    mpi_func = MPIFunction("hostname", resource_specification={"num_ranks": 2})
    bash_result = mpi_func(resource_specification={"num_ranks": 4})

    assert "$PARSL_MPI_PREFIX hostname" in bash_result.stderr


def test_missing_resource_spec(run_in_tmp_dir):

    mpi_func = MPIFunction("hostname")

    with pytest.raises(AssertionError):
        mpi_func()
