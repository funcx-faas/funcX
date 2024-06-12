import logging
from types import SimpleNamespace

import pytest
from globus_compute_sdk.sdk.hardware_report import _run_command, run_hardware_report

_MOCK_BASE = "globus_compute_sdk.sdk.hardware_report."
_NON_PORTABLE_COMMANDS = ["lscpu", "lshw", "nvidia-smi"]


@pytest.fixture(autouse=True)
def mock_mem_info(mocker):
    # mem_info depends on psutil which is not installed on sdk
    mocker.patch(f"{_MOCK_BASE}mem_info", lambda: None)


@pytest.mark.parametrize("missing_command", _NON_PORTABLE_COMMANDS)
def test_excludes_missing_commands(mocker, missing_command):
    mocker.patch(
        f"{_MOCK_BASE}_run_command", lambda s: None if missing_command in s else s
    )

    env = "test env"
    report = run_hardware_report(env)

    for command in _NON_PORTABLE_COMMANDS:
        if command == missing_command:
            assert command not in report
        else:
            assert command in report
        assert f"== {env} ==" in report


@pytest.mark.parametrize("missing_command", _NON_PORTABLE_COMMANDS)
def test_run_command_handles_missing_commands(mocker, caplog, missing_command):
    mocker.patch(f"{_MOCK_BASE}shutil.which", lambda s: s != missing_command)
    mocker.patch(
        f"{_MOCK_BASE}subprocess.run", lambda s, **_: SimpleNamespace(stdout=s[0])
    )

    for command in _NON_PORTABLE_COMMANDS:
        with caplog.at_level(logging.INFO):
            caplog.clear()
            output = _run_command(command)

        if command == missing_command:
            assert output is None
            assert f"{missing_command} was not found in the PATH" in caplog.text
        else:
            assert output == command
            assert f"{missing_command} was not found in the PATH" not in caplog.text
