from itertools import chain, combinations

import pytest
from globus_compute_sdk.sdk.login_manager import AuthorizerLoginManager, LoginManager
from globus_sdk.scopes import AuthScopes

CID_KEY = "GLOBUS_COMPUTE_CLIENT_ID"
CSC_KEY = "GLOBUS_COMPUTE_CLIENT_SECRET"
_MOCK_BASE = "globus_compute_sdk.sdk.login_manager."


@pytest.fixture
def logman(mocker, tmp_path):
    home = mocker.patch(f"{_MOCK_BASE}tokenstore._home")
    home.return_value = tmp_path
    return AuthorizerLoginManager({})


def test_auth_client_requires_authorizer_openid_scope(mocker):
    alm = AuthorizerLoginManager({})
    with pytest.raises(KeyError) as pyt_exc:
        alm.get_auth_client()
    assert AuthScopes.openid in str(pyt_exc)


def test_does_not_open_local_cred_storage(mocker, randomstring):
    test_authorizer = randomstring()
    mock_lm = mocker.patch(f"{_MOCK_BASE}authorizer_login_manager.LoginManager")
    mock_lm.SCOPES = {test_authorizer}
    with pytest.raises(LookupError):
        AuthorizerLoginManager({}).ensure_logged_in()

    alm = AuthorizerLoginManager({test_authorizer: "asdf"})
    assert alm.ensure_logged_in() is None, "Test setup: verified SCOPES is checked"
    assert not mock_lm.called, "Do not instantiate; do not create creds storage"


@pytest.mark.parametrize(
    "missing_keys",
    list(
        chain(
            combinations(LoginManager.SCOPES, 1),
            combinations(LoginManager.SCOPES, 2),
            [()],
        )
    ),
)
def test_ensure_logged_in(mocker, logman, missing_keys):
    _authorizers = dict(LoginManager.SCOPES)
    for k in missing_keys:
        _authorizers.pop(k, None)

    logman.authorizers = _authorizers

    if missing_keys:
        with pytest.raises(LookupError) as err:
            logman.ensure_logged_in()

        assert f"could not find authorizer for {missing_keys[0]}" in err.value.args[0]


def test_warns_upon_logout_attempts(mocker):
    mock_log = mocker.patch(f"{_MOCK_BASE}authorizer_login_manager.log")
    alm = AuthorizerLoginManager({})
    assert not mock_log.warning.called
    alm.logout()
    assert mock_log.warning.called
