from __future__ import annotations

import logging
import os
import platform
import shlex
import shutil
import subprocess
import sys

from globus_compute_sdk.sdk.utils import display_name



@display_name("psutil.virtual_memory()")
def mem_info() -> str:
    import psutil  # import here bc psutil is installed with endpoint but not sdk

    svmem = psutil.virtual_memory()
    return "\n".join(f"{k}: {v}" for k, v in svmem._asdict().items())


@display_name("Python version")
def python_version() -> str:
    return str(sys.version)


@display_name("Python interpreter location")
def python_location() -> str:
    return str(sys.executable)


def cpu_info() -> str | None:
    if sys.platform == "linux":
        return "lscpu"
    elif sys.platform == "darwin":
        return "sysctl -a | grep machdep.cpu"
    elif sys.platform == "win32":
        return "wmic CPU get NAME"
    else:
        return None


def _run_command(cmd: str) -> str | None:
    logger = logging.getLogger(__name__)  # get logger at call site (ie, endpoint)

    cmd_list = shlex.split(cmd)
    arg0 = cmd_list[0]

    if not shutil.which(arg0):
        logger.info(f"{arg0} was not found in the PATH")
        return None

    try:
        res = subprocess.run(cmd_list, timeout=30, capture_output=True, text=True)
        if res.stdout:
            return str(res.stdout)
        if res.stderr:
            return str(res.stderr)
        return "Warning: command had no output on stdout or stderr"
    except subprocess.TimeoutExpired:
        logger.exception(f"Command `{cmd}` timed out")
        return (
            "Error: command timed out (took more than 30 seconds). "
            "See endpoint logs for more details."
        )
    except Exception as e:
        logger.exception(f"Command `{cmd}` failed")
        return (
            f"An error of type {type(e).__name__} occurred. "
            "See endpoint logs for more details."
        )


def hardware_commands_list() -> list:
    """
    Commands that can also be useful in globus-compute-diagnostic
    """
    return [
        platform.processor,
        os.cpu_count,
        cpu_info(),
        "lshw -C display",
        "nvidia-smi",
        # mem_info,
        # "df",
        platform.node,
        platform.platform,
    ]


def run_hardware_report(env: str) -> str:
    """
    :param env:    # The environment this is to be run on, will
                     be highlighted in the output headers
                     for example == Compute Endpoint Worker == ... ==
    :returns:      A line break separated output of all the
                     individual reports
    """
    commands = hardware_commands_list()

    # Separate out some commands that are duplicated in general
    # SDK diagnostics
    commands.extend(
        [
            python_version,
            python_location,
            "globus-compute-endpoint version",
        ]
    )

    outputs = []
    for cmd in commands:
        if callable(cmd):
            display_name = getattr(
                cmd, "display_name", f"{cmd.__module__}.{cmd.__name__}()"
            )
            display_name = f"python: {display_name}"
            output = cmd()
        else:
            display_name = f"shell: {cmd}"
            output = _run_command(cmd)

        if output:
            outputs.append(f"== {env} == {display_name} ==\n{output}")

    return "\n\n".join(outputs)
