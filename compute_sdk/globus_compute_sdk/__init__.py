""" Globus Compute : Fast function serving for clouds, clusters and supercomputers.

"""

from globus_compute_sdk.version import __version__ as _version

__author__ = "The Globus Compute Team"
__version__ = _version

from globus_compute_sdk.sdk.bash_function import BashFunction, BashResult
from globus_compute_sdk.sdk.client import Client
from globus_compute_sdk.sdk.container_spec import ContainerSpec
from globus_compute_sdk.sdk.executor import Executor
from globus_compute_sdk.sdk.mpi_function import MPIFunction

__all__ = (
    "Executor",
    "Client",
    "ContainerSpec",
    "BashFunction",
    "MPIFunction",
    "BashResult",
)
