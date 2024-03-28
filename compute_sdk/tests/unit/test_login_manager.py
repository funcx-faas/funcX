import os
import pathlib
import uuid
from contextlib import nullcontext
from itertools import chain, combinations
from unittest import mock

import globus_sdk
import pytest
import requests
from globus_compute_sdk.sdk._environments import _get_envname
from globus_compute_sdk.sdk.login_manager import LoginManager, requires_login
from globus_compute_sdk.sdk.login_manager.client_login import (
    _get_client_creds_from_env,
    get_client_login,
    is_client_login,
)
from globus_compute_sdk.sdk.login_manager.tokenstore import (
    _resolve_namespace,
    ensure_compute_dir,
)

CID_KEY = "GLOBUS_COMPUTE_CLIENT_ID"
CSC_KEY = "GLOBUS_COMPUTE_CLIENT_SECRET"
MOCK_BASE = "globus_compute_sdk.sdk.login_manager"


def _fake_http_response(*, status: int = 200, method: str = "GET") -> requests.Response:
    req = requests.Request(method, "https://funcx.example.org/")
    p_req = req.prepare()
    res = requests.Response()
    res.request = p_req
    res.status_code = status
    return res


@pytest.fixture
def logman(mocker, tmp_path):
    home = mocker.patch(f"{MOCK_BASE}.tokenstore._home")
    home.return_value = tmp_path
    return LoginManager()


def test_get_client_creds_from_env(randomstring):
    for expected_cid, expected_csc in (
        (randomstring(), randomstring()),
        ("", None),
        (None, ""),
        (None, None),
    ):
        env = {}
        if expected_cid is not None:
            env[CID_KEY] = expected_cid
        if expected_csc is not None:
            env[CSC_KEY] = expected_csc
        with mock.patch.dict(os.environ, env):
            found_cid, found_csc = _get_client_creds_from_env()

        assert expected_cid == found_cid
        assert expected_csc == found_csc


@pytest.mark.parametrize("funcx_id", ["foo", "", None])
@pytest.mark.parametrize("funcx_sec", ["foo", "", None])
@pytest.mark.parametrize("compute_id", ["foo", "", None])
@pytest.mark.parametrize("compute_sec", ["foo", "", None])
def test_get_client_creds_deprecation(funcx_id, funcx_sec, compute_id, compute_sec):
    funcx_id_key = CID_KEY.replace("GLOBUS_COMPUTE", "FUNCX_SDK")
    funcx_sc_key = CSC_KEY.replace("GLOBUS_COMPUTE", "FUNCX_SDK")
    env = {
        key: val
        for key, val in [
            (funcx_id_key, funcx_id),
            (funcx_sc_key, funcx_sec),
            (CID_KEY, compute_id),
            (CSC_KEY, compute_sec),
        ]
        if val is not None
    }
    context = (
        pytest.warns(UserWarning)
        if funcx_id is not None or funcx_sec is not None
        else nullcontext()
    )

    with mock.patch.dict(os.environ, env):
        with context as record:
            found_cid, found_csc = _get_client_creds_from_env()

    if compute_id is not None:
        assert found_cid == compute_id
    elif funcx_id is not None:
        assert found_cid == funcx_id
    else:
        assert found_cid is None

    if compute_sec is not None:
        assert found_csc == compute_sec
    elif funcx_sec is not None:
        assert found_csc == funcx_sec
    else:
        assert found_csc is None

    if funcx_id is not None:
        assert any(
            funcx_id_key in r.message.args[0] for r in record
        ), f"{funcx_id_key} was set so it should be warned about"

    if funcx_sec is not None:
        assert any(
            funcx_sc_key in r.message.args[0] for r in record
        ), f"{funcx_sc_key} was set so it should be warned about"


@pytest.mark.parametrize("legacy_dir_exists", [True, False])
@pytest.mark.parametrize("dir_exists", [True, False])
@pytest.mark.parametrize("user_dir", ["/my/dir", None, ""])
def test_ensure_compute_dir(fs, legacy_dir_exists, dir_exists, user_dir):
    home_dirname = pathlib.Path.home()
    legacy_dirname = home_dirname / ".funcx"
    dirname = home_dirname / ".globus_compute"

    if legacy_dir_exists:
        fs.create_dir(legacy_dirname)
    if dir_exists:
        fs.create_dir(dirname)

    if user_dir is not None:
        dirname = pathlib.Path(user_dir)
        with mock.patch.dict(os.environ, {"GLOBUS_COMPUTE_USER_DIR": str(dirname)}):
            compute_dirname = ensure_compute_dir()
    else:
        compute_dirname = ensure_compute_dir()

    assert compute_dirname.is_dir()
    assert compute_dirname == dirname
    if legacy_dir_exists and not dir_exists and user_dir is None:
        assert legacy_dirname.is_symlink()


@pytest.mark.parametrize("user_dir_defined", [True, False])
def test_conflicting_compute_file(fs, user_dir_defined):
    filename = pathlib.Path.home() / ".globus_compute"
    fs.create_file(filename)

    with pytest.raises(FileExistsError) as exc:
        if user_dir_defined:
            with mock.patch.dict(
                os.environ, {"GLOBUS_COMPUTE_USER_DIR": str(filename)}
            ):
                ensure_compute_dir()
        else:
            ensure_compute_dir()

    assert "Error creating directory" in str(exc)


def test_restricted_user_dir(fs):
    parent_dirname = pathlib.Path("/parent/dir/")
    config_dirname = parent_dirname / "config"

    fs.create_dir(parent_dirname)
    os.chmod(parent_dirname, 0o000)

    with pytest.raises(PermissionError) as exc:
        with mock.patch.dict(
            os.environ, {"GLOBUS_COMPUTE_USER_DIR": str(config_dirname)}
        ):
            ensure_compute_dir()

    assert "Permission denied" in str(exc)


def test_is_client_login():
    env = {CID_KEY: "some_id", CSC_KEY: "some_secret"}
    with mock.patch.dict(os.environ, env):
        assert is_client_login()

    for cid, csc in (("", ""), ("", None), (None, ""), (None, None)):
        env = {}
        if cid is not None:
            env[CID_KEY] = cid
        if csc is not None:
            env[CSC_KEY] = csc
        with mock.patch.dict(os.environ, env):
            assert not is_client_login()

    for cid, csc in (
        ("some_id", ""),
        ("some_id", None),
        ("", "some_secret"),
        (None, "some_secret"),
    ):
        env = {}
        if cid is not None:
            env[CID_KEY] = cid
        if csc is not None:
            env[CSC_KEY] = csc
        with mock.patch.dict(os.environ, env):
            with pytest.raises(ValueError) as err:
                is_client_login()

    assert "Both GLOBUS_COMPUTE_CLIENT_ID and GLOBUS_COMPUTE_CLIENT_SECRET" in str(err)


def test_get_client_login(caplog, randomstring):
    for cid, csc in (("", ""), ("", None), (None, ""), (None, None)):
        env = {}
        if cid is not None:
            env[CID_KEY] = cid
        if csc is not None:
            env[CSC_KEY] = csc
        with mock.patch.dict(os.environ, env):
            with pytest.raises(ValueError) as err:
                get_client_login()

    assert "No client is logged in" in str(err)

    env = {CID_KEY: str(uuid.uuid4()), CSC_KEY: "some_secret"}
    with mock.patch.dict(os.environ, env):
        rv = get_client_login()

    assert isinstance(rv, globus_sdk.ConfidentialAppAuthClient)
    assert "VERY LIKELY" not in caplog.text

    env = {CID_KEY: randomstring(), CSC_KEY: randomstring()}
    with mock.patch.dict(os.environ, env):
        rv = get_client_login()

    assert isinstance(rv, globus_sdk.ConfidentialAppAuthClient)
    assert "VERY LIKELY INVALID CLIENT ID" in caplog.text
    assert rv.client_id == env[CID_KEY]
    assert rv.authorizer.password == env[CSC_KEY]


def test_resolve_namespace(randomstring):
    client_id = str(uuid.uuid4())
    env = {CID_KEY: client_id, CSC_KEY: randomstring()}

    for ns_env in (randomstring, "", "123", None):
        ns = _resolve_namespace(ns_env)
        ns_env = _get_envname() if ns_env is None else ns_env
        assert ns == f"user/{ns_env}"

        with mock.patch.dict(os.environ, env):
            ns = _resolve_namespace(ns_env)
            assert ns == f"clientprofile/{ns_env}/{client_id}"


def test_link_login_flow_requires_stdin(mocker, logman):
    mocker.patch(f"{MOCK_BASE}.manager.do_link_auth_flow")
    mock_stdin = mocker.patch(f"{MOCK_BASE}.manager.sys.stdin")
    mock_stdin.isatty.return_value = False
    with pytest.raises(RuntimeError) as err:
        logman.run_login_flow()
    assert "stdin is closed" in err.value.args[0]
    assert "is not a TTY" in err.value.args[0]
    assert "native app" in err.value.args[0]

    mock_stdin.isatty.return_value = True
    mock_stdin.closed = False
    logman.run_login_flow()


def test_run_login_flow_ignored_if_client_login(mocker, logman):
    mock_laf = mocker.patch(f"{MOCK_BASE}.manager.do_link_auth_flow")
    mock_stdin = mocker.patch(f"{MOCK_BASE}.manager.sys.stdin")
    mock_stdin.isatty.return_value = True
    mock_stdin.closed = False
    env = {CID_KEY: str(uuid.uuid4()), CSC_KEY: "some_secret"}
    with mock.patch.dict(os.environ, env):
        logman.run_login_flow()
    mock_laf.assert_not_called()

    logman.run_login_flow()
    mock_laf.assert_called()


def test_get_authorizer(mocker, logman):
    mock_gsdk = mocker.patch(f"{MOCK_BASE}.manager.globus_sdk")
    env = {CID_KEY: str(uuid.uuid4()), CSC_KEY: "some_secret"}
    with mock.patch.dict(os.environ, env):
        logman._get_authorizer("some_resource_server")
    mock_gsdk.ClientCredentialsAuthorizer.assert_called()

    with pytest.raises(LookupError):
        logman._get_authorizer("some_resource_server")


@pytest.mark.parametrize(
    "missing_keys",
    set(
        chain(
            combinations(LoginManager.SCOPES, 1),
            combinations(LoginManager.SCOPES, 2),
            combinations(LoginManager.SCOPES, 3),
            [()],
        )
    ),
)
@pytest.mark.parametrize(
    "missing_scopes",
    set(
        chain(
            combinations(chain(*LoginManager.SCOPES.values()), 1),
            combinations(chain(*LoginManager.SCOPES.values()), 2),
            combinations(chain(*LoginManager.SCOPES.values()), 3),
            [()],
        )
    ),
)
def test_ensure_logged_in(mocker, logman, missing_keys, missing_scopes):
    needs_login = bool(missing_keys) or bool(missing_scopes)

    def _get_data():
        token_data = {}
        for key, scope_list in LoginManager.SCOPES.items():
            if key in missing_keys:
                continue
            scope_str = " ".join(s for s in scope_list if s not in missing_scopes)
            token_data[key] = {"scope": scope_str}
        return token_data

    logman._token_storage.get_by_resource_server = _get_data

    mock_run_login_flow = mocker.patch(
        f"{MOCK_BASE}.manager.LoginManager.run_login_flow"
    )

    logman.ensure_logged_in()

    assert needs_login == mock_run_login_flow.called


def test_requires_login_decorator(mocker, logman):
    mocked_run_login_flow = mocker.patch(
        f"{MOCK_BASE}.manager.LoginManager.run_login_flow"
    )
    mocked_get_web_client = mocker.patch(
        f"{MOCK_BASE}.manager.LoginManager.get_web_client"
    )

    expected = "expected result"
    mock_method = mock.Mock()
    mock_method.side_effect = [
        expected,
        globus_sdk.AuthAPIError(_fake_http_response(status=400, method="POST")),
        expected,
    ]
    mock_method.__name__ = "mock_method"

    class MockClient:
        login_manager = logman
        web_service_address = "127.0.0.1"
        upstream_call = requires_login(mock_method)

    mock_client = MockClient()

    res = mock_client.upstream_call(None)  # case: no need to reauth
    assert res == expected
    assert not mocked_run_login_flow.called
    assert not mocked_get_web_client.called

    res = mock_client.upstream_call(None)  # case: now must reauth
    assert res == expected
    assert mocked_run_login_flow.called
    assert mocked_get_web_client.called
