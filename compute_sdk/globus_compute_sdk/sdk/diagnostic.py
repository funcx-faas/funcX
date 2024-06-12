from __future__ import annotations

import argparse
import contextlib
import glob
import gzip
import os
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
from datetime import datetime as dt

import colorama
import rich

# from globus_compute_sdk.sdk._environments import (
#     get_amqp_service_host,
#     get_web_service_url,
# )
from globus_compute_sdk.sdk.hardware_report import hardware_commands_list
from globus_compute_sdk.sdk.utils import display_name
from globus_compute_sdk.sdk.utils.tutorial_endpoint import TutorialEndpointTester
from globus_compute_sdk.sdk.web_client import WebClient

# from urllib.parse import urlparse


def cat(path: str, wildcard: bool = False, max_bytes: int = 0):
    max_bytes = max(0, max_bytes)

    full_path = os.path.expanduser(path)
    if wildcard:
        files = glob.glob(full_path)
    else:
        files = [full_path]

    @display_name(f"cat({path})")
    def kernel() -> None:
        for filename in files:
            cat_cmd = "cat " + filename
            hline = "=" * len(cat_cmd)
            print(cat_cmd + "\n" + hline)
            if os.path.exists(filename):
                with open(filename) as f:
                    if max_bytes:
                        file_size = os.fstat(f.fileno()).st_size
                        f.seek(max(file_size - max_bytes, 0))
                    content = f.read().replace("\n", "\n | ")
                print(" | " + content)
            else:
                print_warning(f"No file named {filename}\n")
            hline = "-" * len(cat_cmd)
            print(hline + "\n")

    return kernel



def test_conn(host: str, port: int, timeout: int = 5):
    def kernel():
        try:
            socket.create_connection((host, port), timeout=timeout)
            print(f"Connected successfully to {host} over port {port}!\n")
        except OSError as e:
            print_warning(f"Connection failed to {host} over port {port}: {e}\n")

    kernel.display_name = f"test_conn({host}, {port})"  # type: ignore
    return kernel


def test_ssl_conn(host: str, port: int, timeout: int = 5):
    def kernel() -> None:
        context = ssl.create_default_context()
        try:
            with socket.create_connection((host, port), timeout=timeout) as sock:
                with context.wrap_socket(sock, server_hostname=host) as ssock:
                    conn_data = ssock.cipher()
                    cipher, version, _ = conn_data if conn_data else (None, None, None)
            print(
                f"Successfully established SSL connection with {host}:{port}!\n"
                f"Version: {version}\n"
                f"Cipher:  {cipher}\n"
            )
        except OSError as e:
            print_warning(
                f"Failed to establish SSL connection with {host}:{port} - {e}\n",
            )

    kernel.display_name = f"test_ssl_conn({host}, {port})"  # type: ignore
    return kernel


def print_service_versions(base_url: str):
    def kernel():
        # Just adds a newline to the version info for better formatting
        version_info = WebClient(base_url=base_url).get_version(service="all")
        print(f"{version_info}\n")

    kernel.display_name = f"get_service_versions({base_url})"  # type: ignore
    return kernel


def ip_or_if_config_cmd():
    if sys.platform in ["win32"]:
        return "ipconfig"

    return "ifconfig"

@display_name("OpenSSL version")
def print_openssl_version():
    print(f"{ssl.OPENSSL_VERSION}\n")


def print_warning(s: str):
    """"""
    # Prints both to stderr (goes to console) and to stdout (captured into file)
    rich.print(f"[bold red]{s}[/bold red]", file=sys.stderr)
    print(s)


def print_highlight(s: str):
    rich.print(f"[bold yellow]{s}[/bold yellow]")


def os_info_cmd():
    if sys.platform == "linux":
        return cat("/etc/os-release")
    elif sys.platform == "darwin":
        return "sw_vers"
    elif sys.platform == "win32":
        return "systeminfo"
    else:
        return None


def if_supported_on_os(cmd: str):
    """
    Misc commands that may exist for one or more platforms, but not for others.
    A simpler 'supported or not' check that returns None if unavailable
    """
    BY_PLATFORM = {
        "ip": ["linux"],
    }

    return cmd if sys.platform == BY_PLATFORM.get(cmd.split()[0]) else None


def print_endpoint_install_dir() -> str:
    # Works for bare install or pyenv.  TBD pipx
    run_attempt = subprocess.run("globus-compute-endpoint", capture_output=True)
    if bool(run_attempt.stderr):
        return "globus-compute-endpoint is not installed"
    else:
        # `which` by itself may return a value even if gce is not installed.
        # It will return the shim in pyenv, which contains info for other PY versions
        ep_path = shutil.which("globus-compute-endopint")
        return f"globus-compute-endpoint is installed at {ep_path}"


def get_diagnostic_commands(log_bytes: int):
    # web_svc_url = get_web_service_url()
    # web_svc_host = urlparse(web_svc_url).netloc
    # amqp_svc_host = get_amqp_service_host()

    commands = hardware_commands_list()
    tutorial_endpoint_client = TutorialEndpointTester()

    commands.extend(
        [
            "uname -a",
            # os_info_cmd(),
            # "whoami",
            # "pip freeze",
            print_openssl_version,
            # test_conn(web_svc_host, 443),
            # test_conn(amqp_svc_host, 5671),
            # test_ssl_conn(web_svc_host, 443),
            # test_ssl_conn(amqp_svc_host, 5671),
            # print_service_versions(web_svc_url),
            # ip_or_if_config_cmd(),
            if_supported_on_os("ip addr"),
            if_supported_on_os("ip route"),
            tutorial_endpoint_client.run_existing_func_with_Client,
            tutorial_endpoint_client.run_new_func_with_Client,
            tutorial_endpoint_client.run_new_func_with_executor,
            # "netstat -r",
        ]
    )

    # Run endpoint related diagnostics when it is installed
    ep_install_dir = print_endpoint_install_dir()
    if "is not installed" not in ep_install_dir:
        commands.extend(
            [
                print(ep_install_dir),
                "globus-compute-endpoint whoami",
                # "globus-compute-endpoint list",
                # cat("~/.globus_compute/*/*.yaml", wildcard=True),
                # cat("~/.globus_compute/*/*.py", wildcard=True),
                # cat("~/.globus_compute/*/*.j2", wildcard=True),
                # cat("~/.globus_compute/*/*.json", wildcard=True),
                # cat("~/.globus_compute/*/*.log", wildcard=True, max_bytes=log_bytes),
            ]
        )

    return commands


def run_single_command(cmd: str):
    cmd_list = shlex.split(cmd)
    arg0 = cmd_list[0]

    if not shutil.which(arg0):
        print_warning(f"{arg0} was not found in the PATH\n")
        return

    try:
        res = subprocess.run(cmd_list, timeout=15, capture_output=True, text=True)
        if res.stdout:
            print(res.stdout)
        if res.stderr:
            print_warning(res.stderr)
    except subprocess.TimeoutExpired:
        print_warning("Command timed out\n")
    except Exception as e:
        print_warning(f"Command failed: {e}\n")


def run_all_commands_wrapper(print_to_console: bool, log_bytes: int):
    """
    This is the diagnostic wrapper that obtains the list of commands to be run
    (which depends on whether we are in the SDK or Endpoint, and filters based
    on current platform OS), then runs the individual diagnostic methods one by
    one, and collates them into a zip file upon completion.

    For each diagnostic, this method prints what is being run to stdout, then
    executes the diagnostic itself which may also print to stdout if the flag
    is specified.  All output is added to a file as tests are run, and the
    output file is compressed at the conclusion of the method.

    :param print_to_console:  Whether to print the diagnostic output to console
                                as they are being run.  If False, will only
                                print the name of each step as they are run
    :param log_bytes:         # of bytes to limit the individual endpoint log
                                extractions to
    """

    current_date = dt.now().strftime("%Y-%m-%d")
    log_filename = f"xyz_globus_compute_diagnostic_{current_date}.txt"
    cur_cmd_log = "abc_globus_compute_diagnostic_current.txt"
    zip_filename = f"{log_filename}.gz"

    # Initialize empty file
    open(log_filename, "w").close()

    for cmd in get_diagnostic_commands(log_bytes):
        if cmd is None:
            # None signifies incompatibility with current OS with no warning needed
            # Other commands may also be unavailable, but will emit a warning
            continue
        elif callable(cmd):
            display_name = getattr(cmd, "display_name", f"{cmd.__name__}()")
            display_name = f"python:{display_name}"
        else:
            display_name = str(cmd)

        # This displays the command being run to console for every command
        diagnostic_display = f"== Diagnostic: {display_name} =="
        print_highlight(diagnostic_display)

        with open(cur_cmd_log, "w") as f:
            with contextlib.redirect_stdout(f):
                cmd() if callable(cmd) else run_single_command(cmd)

        cur_logs = None
        with open(log_filename, "a") as log_summary:
            with open(cur_cmd_log) as f:
                # cur_logs = f.read().decode("utf-8")
                cur_logs = f.read()
                log_summary.write(f"\n{diagnostic_display}\n")
                log_summary.write(cur_logs)

        if print_to_console and cur_logs is not None:
            print(cur_logs)

    # REMOVE
    if 2 > 1:
        return
    # Creates a zipped version for easier transport back to Globus
    with open(log_filename, "rb") as f_in, gzip.open(zip_filename, "wb") as f_out:
        f_out.writelines(f_in)
    # Deletes the original to save space - users can unzip to view if necessary
    os.remove(log_filename)


def do_diagnostic():
    """
    This is the root diagnostic method that parses user arguments and then calls
    the wrapper that in turn executes the individual diagnostic methods
    """

    # Supposedly is needed to make sure windows console colors work, is a no-op
    #   on other platforms
    colorama.just_fix_windows_console()

    parser = argparse.ArgumentParser(description="Run diagnostics for Globus Compute")
    parser.add_argument(
        "-p",
        "--print",
        action="store_true",
        default=False,
        help=(
            "print the diagnostic output to console, in addition to saving it to "
            "a local Gzip-compressed file."
        ),
    )
    parser.add_argument(
        "-kb",
        "--log-kb",
        metavar="number",
        action="store",
        default=5120,
        type=int,
        help=(
            "Specify the number of kilobytes (KB) to read from log files."
            " Defaults to 5,120 KB (5 MB)."
        ),
    )
    args = parser.parse_args(sys.argv[1:])
    run_all_commands_wrapper(args.print, args.log_kb * 1024)


if __name__ == "__main__":
    do_diagnostic()
