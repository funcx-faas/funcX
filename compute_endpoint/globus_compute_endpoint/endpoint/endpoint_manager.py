from __future__ import annotations

import fcntl
import json
import logging
import os
import pathlib
import pwd
import queue
import re
import resource
import signal
import socket
import sys
import threading
import time
import typing as t
import uuid
from concurrent.futures import Future
from datetime import datetime
from http import HTTPStatus

import globus_compute_sdk as GC
from cachetools import TTLCache
from globus_compute_endpoint.endpoint.identity_mapper import PosixIdentityMapper

try:
    import pyprctl
except AttributeError as e:
    raise ImportError("pyprctl is not supported on this system") from e
import setproctitle
import yaml
from globus_compute_common.messagepack import pack
from globus_compute_common.messagepack.message_types import EPStatusReport
from globus_compute_common.pydantic_v1 import BaseModel
from globus_compute_endpoint import __version__
from globus_compute_endpoint.endpoint.config import Config
from globus_compute_endpoint.endpoint.config.utils import (
    load_user_config_template,
    render_config_user_template,
    serialize_config,
)
from globus_compute_endpoint.endpoint.endpoint import Endpoint
from globus_compute_endpoint.endpoint.rabbit_mq import (
    CommandQueueSubscriber,
    ResultPublisher,
)
from globus_compute_endpoint.endpoint.utils import (
    _redact_url_creds,
    is_privileged,
    send_endpoint_startup_failure_to_amqp,
    update_url_port,
)
from globus_sdk import GlobusAPIError, NetworkError

if t.TYPE_CHECKING:
    from pika.spec import BasicProperties


log = logging.getLogger(__name__)

_MINIMUM_HEARTBEAT: float = 5.0


class InvalidCommandError(Exception):
    pass


class InvalidUserError(Exception):
    pass


class UserEndpointRecord(BaseModel):
    ep_name: str
    local_user_info: t.Optional[pwd.struct_passwd]
    arguments: str

    @property
    def uid(self) -> int:
        return self.local_user_info.pw_uid if self.local_user_info else -1

    @property
    def gid(self) -> int:
        return self.local_user_info.pw_gid if self.local_user_info else -1

    @property
    def uname(self) -> str:
        return self.local_user_info.pw_name if self.local_user_info else ""


T_CMD_START_ARGS = t.Tuple[
    pwd.struct_passwd, t.Optional[t.List[str]], t.Optional[t.Dict]
]


class EndpointManager:
    def __init__(
        self,
        conf_dir: pathlib.Path,
        endpoint_uuid: str | None,
        config: Config,
        reg_info: dict | None = None,
    ):
        log.debug("Endpoint Manager initialization")

        self.conf_dir = conf_dir
        self._config = config
        self._reload_requested = False
        self._time_to_stop = False
        self._kill_event = threading.Event()

        self._heartbeat_period: float = max(_MINIMUM_HEARTBEAT, config.heartbeat_period)

        self._children: dict[int, UserEndpointRecord] = {}

        self._wait_for_child = False

        self._command_queue: queue.SimpleQueue[tuple[int, BasicProperties, bytes]] = (
            queue.SimpleQueue()
        )
        self._command_stop_event = threading.Event()

        self._cached_cmd_start_args: TTLCache[int, T_CMD_START_ARGS] = TTLCache(
            maxsize=32768, ttl=config.mu_child_ep_grace_period_s
        )

        endpoint_uuid = Endpoint.get_endpoint_id(conf_dir) or endpoint_uuid

        self._mu_user = pwd.getpwuid(os.getuid())
        privileged = is_privileged(self._mu_user)

        self._allow_same_user = not privileged
        if config.force_mu_allow_same_user:
            self._allow_same_user = True
            _warn_str = privileged and "privileged process" or "unprivileged process"
            msg = (
                "Configuration item `force_mu_allow_same_user` set to `true`; this is"
                " considered a very dangerous override -- please use with care,"
                " especially if allowing this endpoint to be utilized by multiple"
                " users."
                f"\n  Endpoint (UID, GID): ({os.getuid()}, {os.getgid()}) {_warn_str}"
            )
            log.warning(msg)
            if sys.stderr.isatty():
                print(f"\033[31;1;40m{msg}\033[0m")  # Red bold on black

        if not reg_info:
            try:
                gcc = GC.Client(
                    local_compute_services=config.local_compute_services,
                    environment=config.environment,
                )
                reg_info = gcc.register_endpoint(
                    name=conf_dir.name,
                    endpoint_id=endpoint_uuid,
                    metadata=EndpointManager.get_metadata(config, conf_dir),
                    multi_user=True,
                    display_name=config.display_name,
                    allowed_functions=config.allowed_functions,
                    auth_policy=config.authentication_policy,
                    subscription_id=config.subscription_id,
                    public=config.public,
                )

                # Mostly to appease mypy, but also a useful text if it ever
                # *does* happen
                assert reg_info is not None, "Empty response from Compute API"

            except GlobusAPIError as e:
                blocked_msg = f"Endpoint registration blocked.  [{e.text}]"
                log.warning(blocked_msg)
                print(blocked_msg)
                if e.http_status in (
                    HTTPStatus.CONFLICT,
                    HTTPStatus.LOCKED,
                    HTTPStatus.NOT_FOUND,
                ):
                    sys.exit(os.EX_UNAVAILABLE)
                elif e.http_status in (
                    HTTPStatus.BAD_REQUEST,
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                ):
                    sys.exit(os.EX_DATAERR)
                raise
            except NetworkError as e:
                log.exception("Network error while registering multi-user endpoint")
                log.critical(f"Network failure; unable to register endpoint: {e}")
                sys.exit(os.EX_TEMPFAIL)

        upstream_ep_uuid = reg_info.get("endpoint_id")
        if endpoint_uuid and upstream_ep_uuid != endpoint_uuid:
            log.error(
                "Unexpected response from server: mismatched endpoint id."
                f"\n  Expected: {endpoint_uuid}, received: {upstream_ep_uuid}"
            )
            sys.exit(os.EX_SOFTWARE)

        endpoint_uuid = str(upstream_ep_uuid)  # convenience, and satisfy mypy
        self._endpoint_uuid = uuid.UUID(endpoint_uuid)
        self._endpoint_uuid_str = endpoint_uuid

        self.identity_mapper: PosixIdentityMapper | None = None
        if not is_privileged(user_privs_only=True):
            # Test for uid-change privileges only because we don't want to enable
            # identity mapping unless the process UID has specifically these
            # privileges; else an unrelated permission (e.g., NET_BIND) would
            # allow identity mapping.
            if config.identity_mapping_config_path:
                msg = (
                    "`identity_mapping_config_path` specified, but process is not"
                    " privileged (e.g., not `root`) -- identity mapping configuration"
                    " will be ignored; only requests from identities that match the"
                    " identity that registered this endpoint will be honored."
                    f"\n    (ignored) '{config.identity_mapping_config_path}'"
                )
                log.warning(msg)
        else:
            if not config.identity_mapping_config_path:
                msg = (
                    "No identity mapping file specified; please specify"
                    " identity_mapping_config_path"
                )
                log.error(msg)
                print(msg, file=sys.stderr)
                sys.exit(os.EX_OSFILE)

            # Only map identities if possibility of *changing* uid; otherwise
            # we enforce that the identity of UEPs must match the
            # parent-process' authorization -- we do not want to allow an open
            # endpoint by a non-power user.
            try:
                self.identity_mapper = PosixIdentityMapper(
                    config.identity_mapping_config_path, self._endpoint_uuid_str
                )

            except PermissionError as e:
                msg = f"({type(e).__name__}) {e}"
                log.error(msg)
                print(msg, file=sys.stderr)
                sys.exit(os.EX_NOPERM)

            except Exception as e:
                msg = (
                    f"({type(e).__name__}) {e} -- Unable to read identity mapping"
                    f" configuration from: {config.identity_mapping_config_path}"
                )
                log.debug(msg, exc_info=e)
                log.error(msg)
                print(msg, file=sys.stderr)
                sys.exit(os.EX_CONFIG)

        try:
            cq_info = reg_info["command_queue_info"]
            _ = cq_info["connection_url"], cq_info["queue"]

            rq_info = reg_info["result_queue_info"]
            _ = rq_info["connection_url"], rq_info["queue"]
            _ = rq_info["queue_publish_kwargs"]
        except Exception as e:
            log_reg_info = _redact_url_creds(str(reg_info))
            log.debug("%s", log_reg_info)
            log.error(
                "Invalid or unexpected registration data structure:"
                f" ({e.__class__.__name__}) {e}"
            )
            sys.exit(os.EX_DATAERR)

        if config.amqp_port:
            cq_info["connection_url"] = update_url_port(
                cq_info["connection_url"], config.amqp_port
            )

        # sanitize passwords in logs
        log_reg_info = _redact_url_creds(repr(reg_info))
        log.debug(f"Registration information: {log_reg_info}")

        json_file = conf_dir / "endpoint.json"

        # `endpoint_id` key kept for backward compatibility when
        # globus-compute-endpoint list is called
        ep_info = {"endpoint_id": endpoint_uuid}
        json_file.write_text(json.dumps(ep_info))
        log.debug(f"Registration info written to {json_file}")

        # * == "multi-user"; not important until it is, so let it be subtle
        ptitle = f"Globus Compute Endpoint *({endpoint_uuid}, {conf_dir.name})"
        if config.environment:
            ptitle += f" - {config.environment}"
        ptitle += f" [{setproctitle.getproctitle()}]"
        setproctitle.setproctitle(ptitle)

        self._command = CommandQueueSubscriber(
            queue_info=cq_info,
            command_queue=self._command_queue,
            stop_event=self._command_stop_event,
            thread_name="CQS",
        )
        self._heartbeat_publisher = ResultPublisher(queue_info=rq_info)

    @staticmethod
    def get_metadata(config: Config, conf_dir: pathlib.Path) -> dict:
        # Piecemeal Config settings because for MU, most of the SU items are
        # unrelated -- the MU (aka EndpointManager) does not execute tasks
        user_config_template, user_config_schema = load_user_config_template(conf_dir)
        return {
            "endpoint_version": __version__,
            "hostname": socket.getfqdn(),
            "local_user": pwd.getpwuid(os.getuid()).pw_name,
            "config": serialize_config(config),
            "user_config_template": user_config_template,
            "user_config_schema": user_config_schema,
        }

    def request_shutdown(self, sig_num, curr_stack_frame):
        self._time_to_stop = True

    def set_child_died(self, sig_num, curr_stack_fframe):
        self._wait_for_child = True

    def wait_for_children(self):
        try:
            self._wait_for_child = False
            wait_flags = os.WNOHANG
            pid, exit_status_ind = os.waitpid(-1, wait_flags)
            while pid > 0:
                try:
                    rc = os.waitstatus_to_exitcode(exit_status_ind)
                except ValueError:
                    rc = -127  # invalid signal number

                uep_record = self._children.pop(pid, None)

                proc_args = f" [{uep_record.arguments}]" if uep_record else ""
                if not rc:
                    log.info(f"Command stopped normally ({pid}){proc_args}")
                    cmd_start_args = self._cached_cmd_start_args.pop(pid, None)
                    if not self._time_to_stop and cmd_start_args is not None:
                        self._revive_child(uep_record, cmd_start_args)
                elif rc > 0:
                    log.warning(f"Command return code: {rc} ({pid}){proc_args}")
                elif rc == -127:
                    log.warning(f"Command unknown return code: ({pid}){proc_args}")
                else:
                    log.warning(
                        f"Command terminated by signal: {-rc} ({pid}){proc_args}"
                    )

                pid, exit_status_ind = os.waitpid(-1, wait_flags)

        except ChildProcessError:
            pass
        except Exception as e:
            log.exception(f"Failed to wait for a child process: {e}")

    def _revive_child(
        self, uep_record: UserEndpointRecord | None, cmd_start_args: T_CMD_START_ARGS
    ):
        ep_name = uep_record.ep_name if uep_record else "<unknown>"
        log.info(
            "User EP stopped within grace period; using cached arguments "
            f"to start a new instance (name: {ep_name})"
        )

        try:
            cached_rec, args, kwargs = cmd_start_args
            updated_rec = pwd.getpwuid(cached_rec.pw_uid)
        except Exception as e:
            log.warning(
                "Unable to update local user information; user EP will not be revived."
                f"  ({e.__class__.__name__}) {e}"
            )
            return

        try:
            self.cmd_start_endpoint(updated_rec, args, kwargs)
        except Exception:
            log.exception(
                f"Unable to execute command: cmd_start_endpoint\n"
                f"    args: {args}\n"
                f"  kwargs: {kwargs}"
            )

    def _install_signal_handlers(self):
        signal.signal(signal.SIGTERM, self.request_shutdown)
        signal.signal(signal.SIGINT, self.request_shutdown)
        signal.signal(signal.SIGQUIT, self.request_shutdown)

        signal.signal(signal.SIGCHLD, self.set_child_died)

    def send_heartbeat(self, shutting_down=False) -> Future[None]:
        if not self._heartbeat_publisher.is_alive():
            _w = RuntimeWarning("Heartbeat requested, but publisher is not running")
            f: Future[None] = Future()
            f.set_exception(_w)
            return f

        def _heart_publish_done(pub_fut: Future):
            e = f.exception()
            if e:
                log.error(
                    f"Failed to send heartbeat to web-services"
                    f" -- ({type(e).__name__}) {e}"
                )

        global_state = {"heartbeat_period": self._heartbeat_period}
        if shutting_down:
            global_state["heartbeat_period"] = 0  # 0 == "shutting down now"

        message = EPStatusReport(
            endpoint_id=self._endpoint_uuid, global_state=global_state, task_statuses={}
        )
        f = self._heartbeat_publisher.publish(pack(message))
        f.add_done_callback(_heart_publish_done)
        return f

    def start(self):
        log.info(f"\n\n========== Endpoint Manager begins: {self._endpoint_uuid_str}")

        msg_out = None
        if sys.stdout.isatty():
            msg_out = sys.stdout
        elif sys.stderr.isatty():
            msg_out = sys.stderr

        if msg_out:
            # hide cursor, highlight color, reset
            hc, hl, r = "\033[?25l", "\033[104m", "\033[m"
            pld = f"{hl}{self._endpoint_uuid_str}{r}"
            print(f"{hc}        >>> Multi-User Endpoint ID: {pld} <<<", file=msg_out)

        self._install_signal_handlers()

        self._command.start()
        self._heartbeat_publisher.start()

        try:
            self._event_loop()
        except Exception:
            log.exception("Unhandled exception; shutting down endpoint master")

        ptitle = f"[shutdown in progress] {setproctitle.getproctitle()}"
        setproctitle.setproctitle(ptitle)
        self._command_stop_event.set()
        self._kill_event.set()

        if self.identity_mapper:
            self.identity_mapper.stop_watching()

        try:
            f = self.send_heartbeat(shutting_down=True)
            f.result(10)  # Ensure heartbeat sent prior to thread shutdown
        except Exception as e:
            log.warning(f"Unable to send final heartbeat -- ({type(e).__name__}) {e}")

        self._heartbeat_publisher.stop(block=False)
        os.killpg(os.getpgid(0), signal.SIGTERM)

        proc_uid, proc_gid = os.getuid(), os.getgid()
        for msg_prefix, signum in (
            ("Signaling shutdown", signal.SIGTERM),
            ("Forcibly killing", signal.SIGKILL),
        ):
            for pid, rec in self._children.items():
                uid, gid, uname, proc_args = rec.uid, rec.gid, rec.uname, rec.arguments
                proc_ident = f"PID: {pid}, UID: {uid}, GID: {gid}, User: {uname}"
                log.info(f"{msg_prefix} of user endpoint ({proc_ident}) [{proc_args}]")
                try:
                    os.setresgid(gid, gid, -1)
                    os.setresuid(uid, uid, -1)
                    os.killpg(os.getpgid(pid), signum)
                except Exception as e:
                    log.warning(
                        f"User endpoint signal failed: {e} ({proc_ident}) [{proc_args}]"
                    )
                finally:
                    os.setresuid(proc_uid, proc_uid, -1)
                    os.setresgid(proc_gid, proc_gid, -1)

            deadline = time.monotonic() + 10
            while self._children and time.monotonic() < deadline:
                time.sleep(0.5)
                self.wait_for_children()

        self._command.join(5)
        self._heartbeat_publisher.join(5)
        log.info(
            "Shutdown complete."
            f"\n---------- Endpoint Manager ends: {self._endpoint_uuid_str}\n\n"
        )
        if msg_out:
            # re-enable cursor visibility
            print("\033[?25h", end="", file=msg_out)

    def _event_loop(self):
        parent_identities = set()
        if not is_privileged():
            client_options = {
                "local_compute_services": self._config.local_compute_services,
                "environment": self._config.environment,
            }
            log.debug("Ascertaining user identity set (%s)", client_options)

            gcc = GC.Client(**client_options)
            try:
                userinfo = gcc.login_manager.get_auth_client().userinfo()
                ids = userinfo["identity_set"]
                parent_identities.update(ident["sub"] for ident in ids)
                log.debug(
                    "User-endpoint start requests are valid from identites: %s",
                    parent_identities,
                )
                del gcc, client_options, ids
                if not parent_identities:
                    # Not a privileged user -- we require at least one identity
                    # against which to match start endpoint requests.
                    raise LookupError("No authorized identities found")

            except Exception as exc:
                msg = "Failed to determine identity set; try `whoami` command?"
                log.error(f"({type(exc).__name__}) {exc}\n    {msg}")
                log.debug("Stopping; failed to determine identities", exc_info=exc)
                self._time_to_stop = True
                return

        last_heartbeat = 0
        valid_method_name_re = re.compile(r"^cmd_[A-Za-z][0-9A-Za-z_]{0,99}$")
        max_skew_s = 180  # 3 minutes; ignore commands with out-of-date timestamp
        while not self._time_to_stop:
            if self._wait_for_child:
                self.wait_for_children()

            if time.monotonic() - last_heartbeat >= self._heartbeat_period:
                self.send_heartbeat()
                last_heartbeat = time.monotonic()

            try:
                d_tag, props, body = self._command_queue.get(timeout=1.0)
                self._command.ack(d_tag)
                if props.headers and props.headers.get("debug", False):
                    body_log_b = _redact_url_creds(body, redact_user=False)
                    log.warning(
                        "Command debug requested:"
                        f"\n  Delivery Tag: {d_tag}"
                        f"\n  Properties: {props}"
                        f"\n  Body bytes: {body_log_b}"
                    )
            except queue.Empty:
                if self._command_stop_event.is_set():
                    self._time_to_stop = True
                if sys.stderr.isatty():
                    time_fmt = time.strftime("%c")
                    print(f"  ----> {time_fmt}\r", end="", flush=True, file=sys.stderr)
                continue

            try:
                server_cmd_ts = props.timestamp
                if props.content_type != "application/json":
                    raise ValueError("Invalid message type; expecting JSON")

                cmd_msg = json.loads(body)
                command = cmd_msg.get("command")
                command_args = cmd_msg.get("args", [])
                command_kwargs = cmd_msg.get("kwargs", {})
            except Exception as e:
                log.error(
                    "Unable to deserialize Globus Compute services command."
                    f"  ({e.__class__.__name__}) {e}"
                )
                continue

            now = round(time.time())
            if abs(now - server_cmd_ts) > max_skew_s:
                server_pp_ts = datetime.fromtimestamp(server_cmd_ts).strftime("%c")
                endp_pp_ts = datetime.fromtimestamp(now).strftime("%c")
                msg = (
                    "Ignoring command from server"
                    "\nCommand too old or skew between system clocks is too large."
                    f"\n  Command timestamp:  {server_cmd_ts} ({server_pp_ts})"
                    f"\n  Endpoint timestamp: {now} ({endp_pp_ts})"
                )
                log.warning(msg)
                self.send_failure_notice(command_kwargs, msg=msg)
                continue

            try:
                effective_identity = cmd_msg["globus_effective_identity"]
                identity_set = cmd_msg["globus_identity_set"]
                globus_username = cmd_msg["globus_username"]
            except Exception as e:
                msg = f"Invalid server command.  ({e.__class__.__name__}) {e}"
                log.error(msg)
                self.send_failure_notice(command_kwargs, msg=msg)
                continue

            identity_for_log = (
                f"\n  Globus effective identity: {effective_identity}"
                f"\n  Globus username: {globus_username}"
            )

            local_user_rec = None
            local_username = None
            if not self.identity_mapper or parent_identities:
                # we are not a privileged user, so *only* allow the identity (or
                # linked identities) of the parent process auth'd to run tasks

                try:
                    cmd_identities = {ident["sub"] for ident in identity_set}
                except Exception as e:
                    log.debug(
                        "Invalid identity set: %s [({%s}) %s]",
                        identity_set,
                        type(e).__name__,
                        e,
                    )
                    cmd_identities = set()

                if not parent_identities.intersection(cmd_identities):
                    msg = (
                        "Ignoring start request for untrusted identity."
                        f"{identity_for_log}"
                    )
                    log.error(msg)
                    self.send_failure_notice(
                        command_kwargs, msg=msg, user_ident=identity_for_log
                    )
                    continue
                local_user_rec = self._mu_user
                local_username = self._mu_user.pw_name

            else:
                try:
                    local_username = self.identity_mapper.map_identity(identity_set)
                    if not local_username:
                        raise LookupError()
                except LookupError as e:
                    msg = (
                        "Identity failed to map to a local user name."
                        f"  ({type(e).__name__}) {e}{identity_for_log}"
                    )
                    log.error(msg)
                    self.send_failure_notice(
                        command_kwargs, msg=msg, user_ident=identity_for_log
                    )
                    continue
                except Exception as e:
                    msg = "Unhandled error attempting to map to a local user name."
                    log.debug(f"{msg}{identity_for_log}", exc_info=e)
                    log.error(f"{msg}  ({type(e).__name__}) {e}{identity_for_log}")

                    fail_msg = f"{msg}{identity_for_log}"
                    self.send_failure_notice(
                        command_kwargs, msg=fail_msg, user_ident=identity_for_log
                    )
                    continue

                try:
                    local_user_rec = pwd.getpwnam(local_username)

                except Exception as e:
                    exc_type = type(e).__name__
                    msg = (
                        "  Identity mapped to a local user name, but local user does"
                        " not exist."
                        f"\n  Local user name: {local_username}{identity_for_log}"
                    )
                    log.error(f"({exc_type}) {e}\n{msg}")
                    fail_msg = f"({exc_type})\n{msg}"
                    self.send_failure_notice(
                        command_kwargs, msg=fail_msg, user_ident=identity_for_log
                    )
                    continue

            try:
                if not (command and valid_method_name_re.match(command)):
                    raise InvalidCommandError(f"Unknown or invalid command: {command}")

                command_func = getattr(self, command, None)
                if not command_func:
                    raise InvalidCommandError(f"Unknown or invalid command: {command}")

                command_func(local_user_rec, command_args, command_kwargs)
                log.info(
                    f"Command process successfully forked for '{local_username}'"
                    f" (Globus effective identity: {effective_identity})."
                )
            except (InvalidCommandError, InvalidUserError) as e:
                exc_type = type(e).__name__
                log.error(f"({exc_type}) {e}{identity_for_log}")
                msg = (
                    f"({exc_type}) unexpected error; this is due to either an endpoint"
                    " misconfiguration or a programming error.  If you are able to"
                    " recreate this error message at will, consider reaching out to the"
                    " endpoint administrator or the Globus Compute team."
                )
                self.send_failure_notice(
                    command_kwargs, msg=msg, user_ident=identity_for_log
                )

            except Exception:
                msg_a = _redact_url_creds(str(command_args), redact_user=False)
                msg_kw = _redact_url_creds(str(command_kwargs), redact_user=False)

                log.exception(
                    f"Unable to execute command: {command}\n"
                    f"    args: {msg_a}\n"
                    f"  kwargs: {msg_kw}{identity_for_log}"
                )
                self.send_failure_notice(command_kwargs, user_ident=identity_for_log)

    def send_failure_notice(
        self,
        kwargs: dict,
        msg: str | None = None,
        user_ident: str = "",
        fork: bool = True,
    ):
        """
        Given a set of AMQP credentials, send a message to the Compute web services
        that the given endpoint has failed to start up.

        This method conditionally forks (if ``fork == True``), but always exits.  The
        exit is always "clean" (exit code of 0) -- this is true even if there is an
        unhandled error as the assumption is that this is a last-ditch effort to be
        kind to the user (better UX).  If it fails, "oh well," and then it is time for
        the administrator to investigate the logs.

        :param kwargs: A structure containing an ``amqps_creds`` key.  This should
            match the structure as the web-service sends for a user-endpoint start
            command.  The credentials will be utilized to send a message to the AMQP
            service.

        :param msg: This parameter will be presented to the user via the SDK as the
            reason for failure (finishing any outstanding futures), so be mindful of
            values passed to this parameter (e.g., sensitive information).  If ``None``
            (as opposed to the empty string), then a default message will be sent.

        :param user_ident: utilized for logging purposes for the admin for when the
            forked process quits immediately after sending the message.
        """
        if fork:
            try:
                pid = os.fork()
            except Exception as e:
                log.error(f"Unable to fork child process: ({type(e).__name__}) {e}")
                raise

            if pid > 0:
                uep_info = [f"User endpoint name: {kwargs.get('name')}"]
                if user_ident:
                    uep_info.extend(i.strip() for i in user_ident.strip().split("\n"))
                info = "; ".join(uep_info)
                args = f"Temporary process to send failure message ({info})"
                self._children[pid] = UserEndpointRecord(
                    ep_name=f"{pid}", local_user_info=None, arguments=args
                )

                return

        try:
            send_endpoint_startup_failure_to_amqp(kwargs["amqp_creds"], msg=msg)
        except Exception:
            log.exception("Unable to send user endpoint start up failure")
        finally:
            sys.exit()

    def cmd_start_endpoint(
        self,
        user_record: pwd.struct_passwd,
        args: list[str] | None,
        kwargs: dict | None,
    ):
        if not args:
            args = []
        if not kwargs:
            kwargs = {}

        ep_name = kwargs.get("name", "")
        if not ep_name:
            raise InvalidCommandError("Missing endpoint name")

        for p, r in self._children.items():
            if r.ep_name == ep_name:
                log.info(
                    f"User endpoint {ep_name} is already running (pid: {p}); "
                    "caching arguments in case it's about to shut down"
                )
                self._cached_cmd_start_args[p] = (user_record, args, kwargs)
                return

        udir, uid, gid = user_record.pw_dir, user_record.pw_uid, user_record.pw_gid
        uname = user_record.pw_name

        if not self._allow_same_user:
            p_uname = self._mu_user.pw_name
            if uname == p_uname or uid == os.getuid():
                raise InvalidUserError(
                    "Requested UID is same as multi-user UID, but configuration"
                    " has not been marked to allow the multi-user UID to process"
                    " tasks.  To allow the multi-user UID to also run single-user"
                    " endpoints, consider using a non-root user or removing privileges"
                    " from the UID."
                    f"\n  MU Process UID: {self._mu_user.pw_uid} ({p_uname})"
                    f"\n  Requested UID:  {uid} ({uname})",
                )

        proc_args = [
            "globus-compute-endpoint",
            "start",
            ep_name,
            "--die-with-parent",
            *args,
        ]

        try:
            pid = os.fork()
        except Exception as e:
            log.error(f"Unable to fork child process: ({e.__class__.__name__}) {e}")
            raise

        if pid > 0:
            proc_args_s = f"({uname}, {ep_name}) {' '.join(proc_args)}"
            self._children[pid] = UserEndpointRecord(
                ep_name=ep_name, local_user_info=user_record, arguments=proc_args_s
            )
            log.info(f"Creating new user endpoint (pid: {pid}) [{proc_args_s}]")
            return

        # Reminder: from this point on, we are now the *child* process.
        pid = os.getpid()

        exit_code = 70
        try:
            # in the child process; no need to load this in MUEP space
            import shutil
            from multiprocessing.process import current_process

            # hack to work with logging module; distinguish fork()ed process
            # beyond subtle pid: MainProcess-12345 --> UserEnd...(PreExec)-23456
            current_process().name = "UserEndpointProcess_Bootstrap(PreExec)"

            from globus_compute_endpoint.logging_config import setup_logging

            # after dropping privileges, any log.* calls may not be able to access
            # the parent's logging file.  We'll rely on stderr in that case, and fall
            # back to the exit_code in the worst case.
            setup_logging(logfile=None, debug=log.getEffectiveLevel() <= logging.DEBUG)

            # load prior to dropping privileges
            template_str, user_config_schema = load_user_config_template(self.conf_dir)

            pybindir = pathlib.Path(sys.executable).parent
            default_path = ("/usr/local/bin", "/usr/bin", "/bin", pybindir)
            env: dict[str, str] = {"PATH": ":".join(map(str, default_path))}
            env_path = self.conf_dir / "user_environment.yaml"
            try:
                log.debug("Load default environment variables from: %s", env_path)
                env_text = env_path.read_text()
                if not env_text:
                    raise ValueError("empty file")
                env_data = yaml.safe_load(env_text)
                if env_data:
                    env.update({k: str(v) for k, v in env_data.items()})
            except FileNotFoundError:
                log.warning(
                    "No user environment variable file found at %s.  Using default: %s",
                    env_path,
                    env,
                )
            except ValueError:
                log.warning(
                    "User environment variable file at %s is empty.  Using default: %s",
                    env_path,
                    env,
                )
            except Exception as e:
                log.warning(
                    "Failed to parse user environment variables from %s.  Using "
                    "default: %s\n  --- Exception ---\n(%s) %s",
                    env_path,
                    env,
                    type(e).__name__,
                    e,
                )
            user_home = {"HOME": udir, "USER": uname}
            env.update(user_home)
            os.environ.update(user_home)

            if not os.path.isdir(udir):
                udir = "/"

            wd = env.get("PWD", udir)

            os.chdir("/")  # always succeeds, so start from known place
            exit_code += 1

            if (os.getuid(), os.getgid()) != (uid, gid):
                # For multi-user systems, this is the expected path.  But for those
                # who run the multi-user setup as a non-privileged user, there is
                # no need to change the user: they're already executing _as that
                # uid_!
                try:
                    # The initialization of groups is "fungible" if not a
                    # privileged user
                    log.debug("Initializing groups for %s, %s", uname, gid)
                    os.initgroups(uname, gid)
                except PermissionError as e:
                    log.warning(
                        "Unable to initialize groups; unprivileged user?  Ignoring"
                        " error, but further attempts to drop privileges may fail."
                        "\n  Process ID (pid): %s"
                        "\n  Current user: %s (uid: %s, gid: %s)"
                        "\n  Attempted to initgroups to: %s (uid: %s, name: %s)",
                        os.getpid(),
                        self._mu_user.pw_name,
                        os.getuid(),
                        os.getgid(),
                        gid,
                        uid,
                        uname,
                    )
                    log.debug("Exception text: (%s) %s", e.__class__.__name__, e)
                exit_code += 1

                # But actually becoming the correct UID is _not_ fungible.  If we
                # can't -- for whatever reason -- that's a problem.  So do NOT
                # ignore the potential error.
                log.debug("Setting process group for %s to %s", pid, gid)
                os.setresgid(gid, gid, gid)  # raises (good!) on error
                exit_code += 1
                log.debug("Setting process uid for %s to %s (%s)", pid, uid, uname)
                os.setresuid(uid, uid, uid)  # raises (good!) on error
                exit_code += 1

            # If we had any capabilities, we drop them now.
            pyprctl.CapState().set_current()

            # Even if exec'ing might return some privileges, "no."
            pyprctl.set_no_new_privs()

            # some Q&D verification for admin debugging purposes
            if not shutil.which(proc_args[0], path=env["PATH"]):
                log.warning(
                    "Unable to find executable."
                    f"\n  Executable (not found): {proc_args[0]}"
                    f'\n  Path: "{env["PATH"]}"'
                    f"\n\n  Will attempt exec anyway -- WARNING - it will likely fail."
                    f"\n  (pid: {pid}, user: {uname}, {ep_name})"
                )

            os.setsid()
            exit_code += 1

            umask = 0o077  # Let child process set less restrictive, if desired
            log.debug("Setting process umask for %s to 0o%04o (%s)", pid, umask, uname)
            os.umask(umask)
            exit_code += 1

            log.debug("Changing directory to '%s'", wd)
            os.chdir(wd)
            exit_code += 1

            os.environ["PWD"] = wd
            os.environ["CWD"] = wd
            env["PWD"] = wd
            env["CWD"] = wd

            # in case "something gets stuck," let cmdline show it
            args_title = " ".join(proc_args)
            startup_proc_title = f"Endpoint starting up for {uname} [{args_title}]"
            setproctitle.setproctitle(startup_proc_title)

            gc_dir: pathlib.Path = GC.sdk.login_manager.tokenstore.ensure_compute_dir()
            (gc_dir / ep_name).mkdir(mode=0o700, parents=True, exist_ok=True)

            user_opts = kwargs.get("user_opts", {})
            user_runtime = kwargs.get("user_runtime", {})
            user_config = render_config_user_template(
                self._config, template_str, user_config_schema, user_opts, user_runtime
            )
            stdin_data_dict = {
                "amqp_creds": kwargs.get("amqp_creds"),
                "config": user_config,
            }
            stdin_data = json.dumps(stdin_data_dict)
            exit_code += 1

            # Reminder: this is *os*.open, not *open*.  Descriptors will not be closed
            # unless we explicitly do so, so `null_fd =` in loop will work.
            null_fd = os.open(os.devnull, os.O_WRONLY, mode=0o200)
            while null_fd < 3:  # reminder 0/1/2 == std in/out/err, so ...
                # ... overkill, but "just in case": don't step on them
                null_fd = os.open(os.devnull, os.O_WRONLY, mode=0o200)
            exit_code += 1

            log.debug("Setting up process stdin")
            read_handle, write_handle = os.pipe()

            # fcntl.F_GETPIPE_SZ is not available in Python versions less than 3.10
            F_GETPIPE_SZ = 1032
            # 256 - Allow some headroom for multiple kernel-specific factors
            max_buf_size = fcntl.fcntl(write_handle, F_GETPIPE_SZ) - 256
            stdin_data_size = len(stdin_data)
            if stdin_data_size > max_buf_size:
                raise ValueError(
                    f"Unable to write {stdin_data_size} bytes of data to stdin; "
                    f"the maximum allowed is {max_buf_size} bytes"
                )

            exit_code += 1
            if os.dup2(read_handle, 0) != 0:  # close old stdin, use read_handle
                raise OSError("Unable to close stdin")
            os.close(read_handle)
            exit_code += 1

            log.debug("Redirecting stdout and stderr (%s)", os.devnull)
            with os.fdopen(null_fd, "w") as null_f:
                if os.dup2(null_f.fileno(), 1) != 1:
                    raise OSError("Unable to close stdout")
                exit_code += 1
                if os.dup2(null_f.fileno(), 2) != 2:
                    raise OSError("Unable to close stderr")

            # After the last os.dup2(), we are unable to get logs at *all*; hence the
            # exit_code as a last-ditch attempt at sharing "what went wrong where" to
            # the parent process.
            exit_code += 1
            log.debug("Writing credentials and config to stdin")
            with os.fdopen(write_handle, "w") as stdin_pipe:
                # intentional side effect: close handle
                stdin_pipe.write(stdin_data)

            exit_code += 1
            _soft_no, hard_no = resource.getrlimit(resource.RLIMIT_NOFILE)

            # Save closerange until last so that we can still get logs written
            # to the endpoint.log.  Meanwhile, use the exit_code as a
            # last-ditch attempt at sharing "what went wrong where" to the
            # parent process.
            exit_code += 1
            os.closerange(3, hard_no)

            exit_code += 1
            os.execvpe(proc_args[0], args=proc_args, env=env)

            # not executed, except perhaps in testing
            exit_code += 1  # type: ignore
        except Exception as e:
            msg = (
                f"Unable to start user endpoint process for {uname}"
                f" [exit code: {exit_code}; ({type(e).__name__}) {e}]"
            )
            log.error(msg)
            log.debug(f"Failed to exec for {uname}", exc_info=e)
            self.send_failure_notice(kwargs, msg=msg, fork=False)
        finally:
            # Only executed if execvpe fails (or isn't reached)
            sys.exit(exit_code)
