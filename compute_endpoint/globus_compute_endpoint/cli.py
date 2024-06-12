from __future__ import annotations

import difflib
import json
import logging
import pathlib
import re
import shutil
import sys
import textwrap
import uuid

import click
from click import ClickException
from click_option_group import optgroup
from globus_compute_endpoint.boot_persistence import disable_on_boot, enable_on_boot
from globus_compute_endpoint.endpoint.config import Config
from globus_compute_endpoint.endpoint.config.utils import get_config, load_config_yaml
from globus_compute_endpoint.endpoint.endpoint import Endpoint
from globus_compute_endpoint.endpoint.utils import (
    send_endpoint_startup_failure_to_amqp,
    user_input_select,
)
from globus_compute_endpoint.exception_handling import handle_auth_errors
from globus_compute_endpoint.logging_config import setup_logging
from globus_compute_sdk.sdk.login_manager import LoginManager
from globus_compute_sdk.sdk.login_manager.client_login import is_client_login
from globus_compute_sdk.sdk.login_manager.tokenstore import ensure_compute_dir
from globus_compute_sdk.sdk.login_manager.whoami import print_whoami_info
from globus_sdk import MISSING, AuthClient, GlobusAPIError, MissingType

try:
    from globus_compute_endpoint.endpoint.endpoint_manager import EndpointManager
except ImportError as _e:
    _has_multi_user = False
    if "--debug" in sys.argv and sys.stderr.isatty():
        # We haven't set up logging yet so manually print for now
        print(f"(DEBUG) Failed to import from file: {__file__}", file=sys.stderr)
        print(f"(DEBUG) [{type(_e).__name__}] {_e}", file=sys.stderr)
else:
    _has_multi_user = True

log = logging.getLogger(__name__)


_AUTH_POLICY_DEFAULT_NAME = "Globus Compute Authentication Policy"
_AUTH_POLICY_DEFAULT_DESC = "This policy was created automatically by Globus Compute."


class CommandState:
    def __init__(self):
        self.endpoint_config_dir: pathlib.Path = init_config_dir()
        self.debug = False
        self.no_color = False
        self.log_to_console = False
        self.endpoint_uuid = None
        self.die_with_parent = False

    @classmethod
    def ensure(cls) -> CommandState:
        return click.get_current_context().ensure_object(CommandState)


def init_config_dir() -> pathlib.Path:
    try:
        return ensure_compute_dir()
    except (FileExistsError, PermissionError) as e:
        raise ClickException(str(e))


def get_config_dir() -> pathlib.Path:
    state = CommandState.ensure()
    return state.endpoint_config_dir


def get_cli_endpoint(conf: Config) -> Endpoint:
    # this getter creates an Endpoint object from the CommandState
    # it takes its various configurable values from the current CommandState
    # as a result, any number of CLI options may be used to tweak the CommandState
    # via callbacks, and the Endpoint will only be constructed within commands which
    # access the Endpoint via this getter
    state = CommandState.ensure()
    endpoint = Endpoint(debug=state.debug or conf.debug)

    return endpoint


def set_param_to_config(ctx, param, value):
    state = CommandState.ensure()
    setattr(state, param.name, value)
    return state


def log_flag_callback(ctx, param, value):
    if not value or ctx.resilient_parsing:
        return

    state = set_param_to_config(ctx, param, value)
    setup_logging(debug=state.debug, no_color=state.no_color)


def common_options(f):
    f = click.option(
        "--debug",
        is_flag=True,
        expose_value=False,
        callback=log_flag_callback,
        is_eager=True,
        help="Emit extra information generally only helpful for developers",
    )(f)
    f = click.option(
        "--log-to-console",
        is_flag=True,
        expose_value=False,
        is_eager=True,
        callback=log_flag_callback,
        help="Emit log lines to console as well as log",
    )(f)
    f = click.option(
        "--no-color",
        is_flag=True,
        expose_value=False,
        is_eager=True,
        callback=log_flag_callback,
        help="Do not colorize the (console) log lines",
    )(f)

    f = click.help_option("-h", "--help")(f)
    return f


def start_options(f):
    f = click.option(
        "--endpoint-uuid",
        default=None,
        callback=set_param_to_config,
        help="The UUID to register with the Globus Compute services",
    )(f)
    f = click.option(
        "--die-with-parent",
        is_flag=True,
        hidden=True,
        callback=set_param_to_config,
        help="Shutdown if parent process goes away",
    )(f)
    return f


def get_ep_dir_by_name_or_uuid(ctx, param, value, require_local: bool = True):
    """
    :param require_local:  Whether a local directory needs to be present,
                             specified by name or found via provided UUID.
                             If this is False, an unrecognized UUID is ok,
                             presumably used to interact with the web-service.
    """
    conf_dir = get_config_dir()
    try:
        uuid.UUID(value)
    except ValueError:
        # value is a name
        path = conf_dir / value
        uuid_provided = False
    else:
        uuid_provided = True
        path = Endpoint.get_endpoint_dir_by_uuid(conf_dir, value)

        # pass the UUID value as a param to be used later, but only if
        # the endpoint isn't necessarily local.  This require_local check
        # also avoids adding the `ep_uuid` as a param to some endpoint commands
        if not require_local:
            ctx.params["ep_uuid"] = value

    if (path and not path.exists()) or (path is None and require_local):
        # If a EP name is given but dir doesn't exist, or if a UUID
        # is given but a local dir is required but missing, error out.
        # (Otherwise, proceed with at least one of ep_dir/ep_uuid set)
        if uuid_provided:
            ep_info = f"with ID {value}"
            ep_name = "<endpoint_name>"
        else:
            ep_info = f"at {path}"
            ep_name = value

        msg = textwrap.dedent(
            f"""
            There is no endpoint configuration on this machine {ep_info}

            1. Please create a configuration template with:
                globus-compute-endpoint configure {ep_name}
            2. Update the configuration
            3. Start the endpoint
            """
        )
        raise ClickException(msg)

    ctx.params["ep_dir"] = path


def get_ep_dir_uuid_only_ok(ctx, param, value):
    # A version of get_ep_dir that allows a UUID without a local dir present
    get_ep_dir_by_name_or_uuid(ctx, param, value, require_local=False)


def verify_not_uuid(ctx, param, value):
    try:
        uuid.UUID(value)
        raise click.BadParameter(
            f"'{ctx.command_path}' requires an endpoint name that"
            f" is not a UUID (got: '{value}')"
        )
    except ValueError:
        return value


def name_or_uuid_arg(f):
    return click.argument(
        "name_or_uuid",
        required=False,
        callback=get_ep_dir_by_name_or_uuid,
        default="default",
        expose_value=False,  # callback supplies ep_dir to command functions
    )(f)


def uuid_arg_or_local_dir(f):
    """
    A version of the decorator that allows UUIDs that do not have a local presence
    """
    return click.argument(
        "name_or_uuid",
        required=False,
        callback=get_ep_dir_uuid_only_ok,
        default="default",
        expose_value=False,  # callback supplies ep_dir/ep_uuid to command functions
    )(f)


def name_arg(f):
    return click.argument(
        "name", required=False, callback=verify_not_uuid, default="default"
    )(f)


def config_dir_callback(ctx, param, value):
    if value is None or ctx.resilient_parsing:
        return
    state = CommandState.ensure()
    state.endpoint_config_dir = pathlib.Path(value)


@click.group("globus-compute-endpoint")
@click.option(
    "-c",
    "--config-dir",
    default=None,
    help="override default config dir",
    callback=config_dir_callback,
    expose_value=False,
)
def app():
    # the main command group body runs on every command, so the block below will always
    # execute
    setup_logging()  # Until we parse the CLI flags, just setup default logging


@app.command("version")
@common_options
def version_command():
    """Show the version of globus-compute-endpoint"""
    import globus_compute_endpoint as gce

    click.echo(f"Globus Compute endpoint version: {gce.__version__}")


@app.command(name="configure", help="Configure an endpoint")
@click.option("--endpoint-config", default=None, help="a template config to use")
@click.option(
    "--multi-user",
    is_flag=True,
    default=False,
    help="Configure endpoint as multi-user capable",
)
@click.option(
    "--display-name",
    help="A human readable display name for the endpoint, if desired",
)
@click.option(
    "--auth-policy",
    help=(
        "Endpoint users are evaluated against this Globus authentication policy. "
        "For more information on Globus authentication policies, visit "
        "https://docs.globus.org/api/auth/developer-guide/#authentication-policies."
    ),
)
@optgroup.group(
    "Authentication Policy Creation",
    help=(
        "Options for creating an auth policy. If any of these options are specified, "
        "the endpoint will be associated with an auth policy configured per those "
        "options and users will be evaluated against that policy."
    ),
)
@optgroup.option(
    "--auth-policy-project-id",
    help=(
        "The Globus Auth Project this policy should belong to "
        "(will prompt to create one if not given)"
    ),
    default=None,
)
@optgroup.option(
    "--auth-policy-display-name",
    default=_AUTH_POLICY_DEFAULT_NAME,
    show_default=True,
)
@optgroup.option(
    "--auth-policy-description",
    default=_AUTH_POLICY_DEFAULT_DESC,
    show_default=True,
)
@optgroup.option(
    "--allowed-domains",
    help="A comma separated list of domains to include (ex: globus.org,*.edu)",
    default=None,
)
@optgroup.option(
    "--excluded-domains",
    help="A comma separated list of domains to exclude (ex: globus.org,*.edu)",
    default=None,
)
@optgroup.option(
    "--auth-timeout",
    help=(
        "How old (in seconds) a login session can be and still be compliant. "
        "Makes the auth policy high assurance"
    ),
    type=click.IntRange(min=0),
    default=None,
)
@click.option("--subscription-id", help="Associate endpoint with a subscription")
@name_arg
@common_options
@handle_auth_errors
def configure_endpoint(
    *,
    name: str,
    endpoint_config: str | None,
    multi_user: bool,
    display_name: str | None,
    auth_policy: str | None,
    auth_policy_project_id: str | None,
    auth_policy_display_name: str,
    auth_policy_description: str,
    allowed_domains: str | None,
    excluded_domains: str | None,
    auth_timeout: int | None,
    subscription_id: str | None,
):
    """Configure an endpoint

    Drops a config.yaml template into the Globus Compute configs directory.
    The template usually goes to ~/.globus_compute/<ENDPOINT_NAME>/config.yaml
    """
    try:
        Endpoint.validate_endpoint_name(name)
    except ValueError as e:
        raise ClickException(str(e))

    if multi_user and not _has_multi_user:
        raise ClickException("multi-user endpoints are not supported on this system")

    if (
        auth_policy_project_id is not None
        or auth_policy_display_name != _AUTH_POLICY_DEFAULT_NAME
        or auth_policy_description != _AUTH_POLICY_DEFAULT_DESC
        or allowed_domains is not None
        or excluded_domains is not None
        or auth_timeout is not None
    ):
        if auth_policy:
            raise ClickException(
                "Cannot specify an existing auth policy and "
                "create a new one at the same time"
            )

        login_manager = LoginManager()
        login_manager.ensure_logged_in()
        ac = login_manager.get_auth_client()

        if not auth_policy_project_id:
            auth_policy_project_id = create_or_choose_auth_project(ac)

        # mypy is confused
        include_domain_list: list[str] | MissingType = (
            allowed_domains.split(",") if allowed_domains else MISSING
        )
        exclude_domain_list: list[str] | MissingType = (
            excluded_domains.split(",") if excluded_domains else MISSING
        )

        auth_policy = create_auth_policy(
            ac=ac,
            project_id=auth_policy_project_id,
            display_name=auth_policy_display_name,
            description=auth_policy_description,
            include_domains=include_domain_list,
            exclude_domains=exclude_domain_list,
            timeout=auth_timeout or MISSING,
        )

    compute_dir = get_config_dir()
    ep_dir = compute_dir / name
    Endpoint.configure_endpoint(
        ep_dir,
        endpoint_config,
        multi_user,
        display_name,
        auth_policy,
        subscription_id,
    )


@app.command(name="start", help="Start an endpoint")
@name_or_uuid_arg
@start_options
@common_options
@handle_auth_errors
def start_endpoint(*, ep_dir: pathlib.Path, **_kwargs):
    """Start an endpoint

    This function will do:
    1. Connect to the broker service, and register itself
    2. Get connection info from broker service
    3. Start the interchange as a daemon

    |                      Broker service       |
    |               -----2----> Forwarder       |
    |    /register <-----3----+   ^             |
    +-----^-----------------------+-------------+
          |     |                 |
          1     4                 6
          |     v                 |
    +-----+-----+-----+           v
    |      Start      |---5---> EndpointInterchange
    |     Endpoint    |         daemon
    +-----------------+
    """
    state = CommandState.ensure()
    _do_start_endpoint(
        ep_dir=ep_dir,
        endpoint_uuid=state.endpoint_uuid,
        die_with_parent=state.die_with_parent,
    )


@app.command(name="login", help="Manually log in with Globus")
@click.option(
    "--force",
    is_flag=True,
    help="Log in even if already logged in",
)
def login(force: bool):
    _do_login(force)


@app.command(name="logout", help="Logout from all endpoints")
@click.option(
    "--force",
    is_flag=True,
    help="Revokes tokens even with currently running endpoints",
)
def logout_endpoints(force: bool):
    success, msg = _do_logout_endpoints(force=force)
    if not success:
        # Raising ClickException is apparently the way to do sys.exit(1)
        #     and return a non-zero value to the command line
        # See https://click.palletsprojects.com/en/8.1.x/exceptions/
        if not isinstance(msg, str) or msg is None:
            # Generic unsuccessful if no reason was given
            msg = "Logout unsuccessful"
        raise ClickException(msg)


@app.command("whoami", help="Show the currently logged-in identity")
@click.option(
    "--linked-identities",
    is_flag=True,
    default=False,
    help="Also show identities linked to the currently logged-in primary identity.",
)
def whoami(linked_identities: bool) -> None:
    try:
        print_whoami_info(linked_identities)
    except ValueError as ve:
        raise ClickException(str(ve))


def _do_login(force: bool) -> None:
    try:
        assert not is_client_login()
    except Exception as e:
        if isinstance(e, AssertionError):
            log.info("Don't need to log in when client credentials are present")
            return
        else:
            raise ClickException(str(e))

    lm = LoginManager()
    if force or lm._token_storage.get_by_resource_server().keys() != lm.SCOPES.keys():
        # if not forced, only run login flow if any tokens are missing
        lm.run_login_flow()
    else:
        log.info("Already logged in. Use --force to run login flow anyway")


def _do_logout_endpoints(
    force: bool, running_endpoints: dict | None = None
) -> tuple[bool, str | None]:
    """
    Logout from all endpoints and remove cached authentication credentials

    Returns True, None if logout was successful and tokens were found and revoked
    Returns False, error_msg if token revocation was not done
    """
    if running_endpoints is None:
        compute_dir = get_config_dir()
        running_endpoints = Endpoint.get_running_endpoints(compute_dir)
    tokens_revoked = False
    error_msg = None
    if running_endpoints and not force:
        running_list = ", ".join(running_endpoints.keys())
        log.info(
            "The following endpoints are currently running: "
            + running_list
            + "\nPlease use logout --force to proceed"
        )
        error_msg = "Not logging out with running endpoints without --force"
    else:
        tokens_revoked = LoginManager().logout()
        if tokens_revoked:
            log.info("Logout succeeded and all cached credentials were revoked")
        else:
            error_msg = "No cached tokens were found, already logged out?"
            log.info(error_msg)
    return tokens_revoked, error_msg


FUNCX_COMPUTE_IMPORT_UPDATES = {
    "from funcx_endpoint.endpoint.utils.config": "from globus_compute_endpoint.endpoint.config",  # noqa E501
    "from funcx_endpoint.engines": "from globus_compute_endpoint.engines",  # noqa E501
    "from funcx_endpoint.executors": "from globus_compute_endpoint.executors",  # noqa E501
    "from funcx_endpoint.engines": "from globus_compute_endpoint.engines",  # noqa E501
}


def _upgrade_funcx_imports_in_config(ep_dir: pathlib.Path, force=False) -> str:
    """
    This only modifies unindented import lines, as are in the original
    config.py.  Indented matching lines are user created and would have to be
    updated manually by the user.

    The force flag will overwrite an existing (non-directory) config.py.bak

    This method uses a randomly generated intermediate output file in case
    there are any permission or unforeseen file system issues
    """
    name = ep_dir.name
    config_path = ep_dir / "config.py"
    config_backup = ep_dir / "config.py.bak"

    try:
        config_text = config_path.read_text()
        upd_config_text = config_text

        for pattern, repl in FUNCX_COMPUTE_IMPORT_UPDATES.items():
            upd_config_text = re.sub(f"^{pattern}", repl, upd_config_text, flags=re.M)

        if upd_config_text == config_text:
            return f"No funcX import statements found in config.py for {name}"

        change_diff = "".join(
            difflib.unified_diff(
                config_text.splitlines(keepends=True),
                upd_config_text.splitlines(keepends=True),
                n=3,  # Typical 3 lines of context
            )
        )

        if config_backup.exists() and not force:
            msg = (
                f"{config_backup} already exists.\n"
                "Rename it or use the --force flag to update config."
            )
            raise ClickException(msg)
        elif config_backup.is_dir():
            msg = (
                f"{config_backup} is a directory.\n"
                "Rename it before proceeding with config update."
            )
            raise ClickException(msg)

        # Write to temporary file in case of issues
        tmp_output_path = ep_dir / ("config.py." + uuid.uuid4().hex)
        tmp_output_path.write_text(upd_config_text)

        # Rename files last, as it's the least likely to err
        if sys.version_info < (3, 8):
            try:
                # Dropping support for Python 3.7 "real soon", so this branch will
                # soon go away
                config_backup.unlink()
            except FileNotFoundError:
                pass
        else:
            config_backup.unlink(missing_ok=True)
        shutil.move(config_path, config_backup)  # Preserve file timestamp
        shutil.move(tmp_output_path, config_path)

        return (
            f"Applied following diff for endpoint {name}:\n{change_diff}\n\n"
            f"  The previous config has been renamed to {config_backup}"
        )

    except FileNotFoundError as err:
        msg = f"No config.py was found for endpoint ({name}) in {ep_dir}"
        raise ClickException(msg) from err
    except ClickException:
        raise
    except Exception as err:
        msg = f"Unknown Exception {err} attempting to reformat config.py in {ep_dir}"
        raise ClickException(msg) from err


def _do_start_endpoint(
    *,
    ep_dir: pathlib.Path,
    endpoint_uuid: str | None,
    die_with_parent: bool = False,
):
    state = CommandState.ensure()
    if ep_dir.is_dir():
        setup_logging(
            logfile=ep_dir / "endpoint.log",
            debug=state.debug,
            console_enabled=state.log_to_console,
            no_color=state.no_color,
        )

    reg_info = {}
    config_str = None
    if sys.stdin and not (sys.stdin.closed or sys.stdin.isatty()):
        try:
            stdin_data = json.loads(sys.stdin.read())

            if not isinstance(stdin_data, dict):
                type_name = stdin_data.__class__.__name__
                raise ValueError(
                    "Expecting JSON dictionary with endpoint info; got"
                    f" {type_name} instead"
                )

            reg_info = stdin_data.get("amqp_creds", {})
            config_str = stdin_data.get("config", None)

        except Exception as e:
            exc_type = e.__class__.__name__
            log.debug("Invalid info on stdin -- (%s) %s", exc_type, e)

    try:
        try:
            if config_str is not None:
                ep_config = load_config_yaml(config_str)
            else:
                ep_config = get_config(ep_dir)

            if not state.debug and ep_config.debug:
                setup_logging(
                    logfile=ep_dir / "endpoint.log",
                    debug=ep_config.debug,
                    console_enabled=state.log_to_console,
                    no_color=state.no_color,
                )
                if config_str is not None:
                    num_lines = config_str.count("\n") + 1  # +1 == 0-based
                    _rendered_config = config_str.replace("\n", "\n  | ")

                    log.debug(
                        f"Begin Compute endpoint configuration ({num_lines:,} lines):"
                        f"\n  | {_rendered_config}"
                        f"\nEnd Compute endpoint configuration"
                    )
                    del _rendered_config
            del config_str

        except Exception as e:
            if isinstance(e, ClickException):
                raise

            # We've likely not exported to the log, so at least put _something_ in the
            # logs for the human to debug; motivated by SC-28607
            exc_type = type(e).__name__
            msg = (
                "Failed to find or parse endpoint configuration.  Endpoint will not"
                f" start. ({exc_type}) {e}"
            )
            log.critical(msg)
            raise

        if die_with_parent:
            # The endpoint cannot die with its parent if it
            # doesn't have one :)
            ep_config.detach_endpoint = False
            log.debug("The --die-with-parent flag has set detach_endpoint to False")

        if ep_config.multi_user:
            if not _has_multi_user:
                raise ClickException(
                    "multi-user endpoints are not supported on this system"
                )
            epm = EndpointManager(ep_dir, endpoint_uuid, ep_config, reg_info)
            epm.start()
        else:
            get_cli_endpoint(ep_config).start_endpoint(
                ep_dir,
                endpoint_uuid,
                ep_config,
                state.log_to_console,
                state.no_color,
                reg_info,
                die_with_parent,
            )

    except (SystemExit, Exception) as e:
        if isinstance(e, SystemExit):
            if e.code in (0, None):
                # normal, system exit
                raise

        if reg_info:
            # We're quitting anyway, so just let any exceptions bubble
            msg = f"Failed to start or unexpected error:\n  ({type(e).__name__}) {e}"
            send_endpoint_startup_failure_to_amqp(reg_info, msg=msg)

        raise


@app.command("stop")
@name_or_uuid_arg
@click.option(
    "--remote",
    is_flag=True,
    help="send stop signal to all endpoints with this UUID, local or elsewhere",
)
@common_options
@handle_auth_errors
def stop_endpoint(*, ep_dir: pathlib.Path, remote: bool):
    """Stops an endpoint using the pidfile"""
    _do_stop_endpoint(ep_dir=ep_dir, remote=remote)


@app.command("update_funcx_config")
@name_or_uuid_arg
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="update config and backup to config.py.bak even if it already exists",
)
def update_funcx_endpoint_config(*, ep_dir: pathlib.Path, force: bool):
    """
    Update imports file from funcx_endpoint.* to globus_compute_endpoint.*

    Either should raise ClickException or returns modification result message
    """
    print(_upgrade_funcx_imports_in_config(ep_dir=ep_dir, force=force))


def _do_stop_endpoint(*, ep_dir: pathlib.Path, remote: bool = False) -> None:
    Endpoint.stop_endpoint(ep_dir, get_config(ep_dir), remote=remote)


@app.command("restart")
@name_or_uuid_arg
@common_options
@start_options
def restart_endpoint(*, ep_dir: pathlib.Path, **_kwargs):
    """Restarts an endpoint"""
    state = CommandState.ensure()
    _do_stop_endpoint(ep_dir=ep_dir)
    _do_start_endpoint(
        ep_dir=ep_dir,
        endpoint_uuid=state.endpoint_uuid,
        die_with_parent=state.die_with_parent,
    )


@app.command("list")
@common_options
def list_endpoints():
    """List all available endpoints"""
    compute_dir = get_config_dir()
    Endpoint.print_endpoint_table(compute_dir)


@app.command("delete")
@uuid_arg_or_local_dir
@click.option(
    "--force",
    default=False,
    is_flag=True,
    help=(
        "Ignores any errors encountered while attempting to delete an "
        "endpoint locally and remotely."
    ),
)
@click.option(
    "--yes",
    default=False,
    is_flag=True,
    help="Do not ask for confirmation to delete endpoints.",
)
@common_options
@handle_auth_errors
def delete_endpoint(
    *,
    ep_dir: pathlib.Path | None,
    ep_uuid: str | None = None,
    force: bool,
    yes: bool,
):
    """Deletes an endpoint and its config."""

    ep_info = f"< {ep_dir.name} >" if ep_dir else ""
    if ep_uuid:
        ep_info += f" ( {ep_uuid} )"

    if not yes:
        click.confirm(
            f"Are you sure you want to delete endpoint {ep_info}?",
            abort=True,
        )

    ep_conf = None
    error_msg = None

    if ep_dir:
        try:
            ep_conf = get_config(ep_dir)
            if ep_uuid is None:
                ep_uuid = Endpoint.get_endpoint_id(ep_dir)
        except Exception:
            # Endpoint directory exists locally but error loading the config
            error_msg = f"Failed to read configuration from {ep_dir}/ "
    else:
        error_msg = f"No local endpoint configuration available for {ep_uuid}"

    if error_msg:
        if force:
            log.info(f"{error_msg}, proceeding to remote deletion by --force ...")
        else:
            raise ClickException(
                f"{error_msg}\n\n"
                "Please add the --force flag if you wish to proceed with "
                "attempting to delete the endpoint both locally and from the "
                "Globus Compute web services."
            )

    Endpoint.delete_endpoint(ep_dir, ep_conf, force=force, ep_uuid=ep_uuid)


@app.command("self-diagnostic")
def self_diagnostic():
    print(
        "The endpoint specific self-diagnostic command has been deprecated.\n"
        "Please use the `globus-compute-diagnostic` command instead."
    )


def create_or_choose_auth_project(ac: AuthClient) -> str:
    """
    Lets the user choose from one of their existing projects, or
    prompt for a description and creates one for them
    """
    projects = ac.get_projects()
    if any(projects):
        proj_selected = user_input_select(
            (
                "Please select a project to contain new auth policy "
                "(leave blank to create a new one):"
            ),
            [f'id: {proj["id"]}, name: {proj["display_name"]}' for proj in projects],
        )
        if proj_selected:
            return proj_selected.split()[1][:-1]

    print("Creating a new auth project.")

    try:
        while True:
            display_name = input("Enter a name for this project: ")
            contact_email = input("Enter the contact email for this project: ")

            admin_ids_input = input(
                "Enter a comma separated list of admin IDs for this project: "
            )
            admin_ids = admin_ids_input.split(",") if admin_ids_input else []

            admin_gps_input = input(
                "Enter a comma separated list of group IDs as project admins: "
            )
            admin_group_ids = admin_gps_input.split(",") if admin_gps_input else []

            cont = input(
                "Create this auth project? "
                "(Y/y to confirm, Q/q to quit, otherwise start over): "
            ).lower()
            if cont == "y":
                break
            elif cont == "q":
                raise KeyboardInterrupt
            else:
                print()
                continue
    except KeyboardInterrupt:
        print("\nCanceled.")
        sys.exit(0)

    try:
        print("Creating project...")
        resp = ac.create_project(
            display_name=display_name,
            contact_email=contact_email,
            admin_ids=admin_ids,
            admin_group_ids=admin_group_ids,
        )
        print("Project created.")
        return resp["project"]["id"]
    except GlobusAPIError as e:
        raise ClickException(f"Unable to create auth project: [{e.text}]")


def create_auth_policy(
    ac: AuthClient,
    project_id: str,
    display_name: str,
    description: str,
    include_domains: list[str] | MissingType,
    exclude_domains: list[str] | MissingType,
    timeout: int | MissingType,
) -> str:
    """
    Uses the Globus SDK to create an auth policy and returns
    the policy_id after creation
    """

    try:
        resp = ac.create_policy(
            project_id=project_id,
            display_name=display_name,
            description=description,
            domain_constraints_include=include_domains,
            domain_constraints_exclude=exclude_domains,
            high_assurance=bool(timeout),
            authentication_assurance_timeout=timeout,
        )
        return resp["policy"]["id"]
    except GlobusAPIError as e:
        raise ClickException(f"Unable to create authentication policy: [{e.text}]")


@app.command(
    "enable-on-boot",
    help="Enable on-boot persistence; restarts the endpoint when the machine restarts.",
)
@name_or_uuid_arg
def enable_on_boot_cmd(ep_dir: pathlib.Path):
    enable_on_boot(ep_dir)


@app.command(
    "disable-on-boot",
    help="Disable on-boot persistence.",
)
@name_or_uuid_arg
def disable_on_boot_cmd(ep_dir: pathlib.Path):
    disable_on_boot(ep_dir)


def cli_run():
    """Entry point for setuptools to point to"""
    app()


if __name__ == "__main__":
    app()
