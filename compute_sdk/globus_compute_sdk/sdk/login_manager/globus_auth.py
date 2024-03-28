import globus_sdk

from ..utils import get_env_var_with_deprecation


def internal_auth_client():
    """
    This is the client that represents the Globus Compute application itself
    """

    client_id = get_env_var_with_deprecation(
        "GLOBUS_COMPUTE_CLIENT_ID",
        "FUNCX_SDK_CLIENT_ID",
        "4cf29807-cf21-49ec-9443-ff9a3fb9f81c",
    )
    return globus_sdk.NativeAppAuthClient(
        client_id, app_name="Globus Compute (internal client)"
    )
