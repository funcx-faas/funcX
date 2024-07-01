from __future__ import annotations

import json
import logging
import sys
import typing as t
import uuid
import warnings

from globus_compute_sdk.errors import (
    SerializationError,
    TaskExecutionFailed,
    TaskPending,
)
from globus_compute_sdk.sdk._environments import get_web_service_url
from globus_compute_sdk.sdk.hardware_report import run_hardware_report
from globus_compute_sdk.sdk.utils import check_version
from globus_compute_sdk.sdk.utils.uuid_like import UUID_LIKE_T
from globus_compute_sdk.sdk.web_client import (
    FunctionRegistrationData,
    FunctionRegistrationMetadata,
)
from globus_compute_sdk.serialize import ComputeSerializer, SerializationStrategy
from globus_compute_sdk.version import __version__, compare_versions
from globus_sdk.version import __version__ as __version_globus__

from .batch import Batch, UserRuntime
from .login_manager import LoginManager, LoginManagerProtocol, requires_login
from .utils import get_env_var_with_deprecation

logger = logging.getLogger(__name__)


class Client:
    """Main class for interacting with the Globus Compute service

    Holds helper operations for performing common tasks with the Globus Compute service.
    """

    FUNCX_SDK_CLIENT_ID = get_env_var_with_deprecation(
        "GLOBUS_COMPUTE_CLIENT_ID",
        "FUNCX_SDK_CLIENT_ID",
        "4cf29807-cf21-49ec-9443-ff9a3fb9f81c",
    )
    FUNCX_SCOPE = get_env_var_with_deprecation(
        "GLOBUS_COMPUTE_SCOPE",
        "FUNCX_SCOPE",
        "https://auth.globus.org/scopes/facd7ccc-c5f4-42aa-916b-a0e270e2c2a9/all",
    )

    def __init__(
        self,
        http_timeout=None,
        funcx_home=None,
        environment: str | None = None,
        task_group_id: t.Union[None, uuid.UUID, str] = None,
        local_compute_services: bool = False,
        do_version_check: bool = True,
        *,
        code_serialization_strategy: SerializationStrategy | None = None,
        data_serialization_strategy: SerializationStrategy | None = None,
        login_manager: LoginManagerProtocol | None = None,
        **kwargs,
    ):
        """
        Initialize the client

        Parameters
        ----------
        http_timeout: int
            Timeout for any call to service in seconds.
            Default is no timeout

            DEPRECATED - see self.web_client

        funcx_home: any
            DEPRECATED - was never used

        environment: str
            For internal use only. The name of the environment to use.

        task_group_id: str|uuid.UUID
            Set the TaskGroup ID (a UUID) for this Client instance.
            Typically, one uses this to submit new tasks to an existing
            session or to reestablish Executor futures.
            Default: None (will be auto generated)

            DEPRECATED - use create_batch or the executor instead

        local_compute_services: str
            For internal use only. TODO

        do_version_check: bool
            Set to ``False`` to skip the version compatibility check on client
            initialization
            Default: True

        code_serialization_strategy: SerializationStrategy
            Strategy to use when serializing function code. If None,
            globus_compute_sdk.serialize.DEFAULT_STRATEGY_CODE will be used.

        data_serialization_strategy: SerializationStrategy
            Strategy to use when serializing function arguments. If None,
            globus_compute_sdk.serialize.DEFAULT_STRATEGY_DATA will be used.

        login_manager: LoginManagerProtocol
            Allows login logic to be overridden for specific use cases. If None, a
            LoginManager will be used.
        """
        for arg, name in [
            (http_timeout, "http_timeout"),
            (funcx_home, "funcx_home"),
            (task_group_id, "task_group_id"),
        ]:
            if arg is not None:
                msg = (
                    f"The '{name}' argument is deprecated. "
                    "It will be removed in a future release."
                )
                warnings.warn(msg)

        for arg_name in kwargs:
            msg = (
                f"The '{arg_name}' argument is unrecognized. "
                "(It might have been removed in a previous release.)"
            )
            warnings.warn(msg)

        self.web_service_address = get_web_service_url(
            "local" if local_compute_services else environment
        )

        self._task_status_table: dict[str, dict] = {}

        # if a login manager was passed, no login flow is triggered
        if login_manager is not None:
            self.login_manager: LoginManagerProtocol = login_manager
        # but if login handling is implicit (as when no login manager is passed)
        # then ensure that the user is logged in
        else:
            self.login_manager = LoginManager(environment=environment)
            self.login_manager.ensure_logged_in()

        self.web_client = self.login_manager.get_web_client(
            base_url=self.web_service_address
        )
        self.fx_serializer = ComputeSerializer(
            strategy_code=code_serialization_strategy,
            strategy_data=data_serialization_strategy,
        )

        self._version_mismatch_already_warned_eps: t.Set[str | None] = set()

        if do_version_check:
            self.version_check()

    def version_check(self, endpoint_version: str | None = None) -> None:
        """Check this client version meets the service's minimum supported version.

        Raises a VersionMismatch error on failure.
        """
        data = self.web_client.get_version()

        min_ep_version = data["min_ep_version"]
        min_sdk_version = data["min_sdk_version"]

        compare_versions(__version__, min_sdk_version)
        if endpoint_version is not None:
            compare_versions(
                endpoint_version, min_ep_version, package_name="globus-compute-endpoint"
            )

    def logout(self):
        """Remove credentials from your local system"""
        self.login_manager.logout()

    def _log_version_mismatch(self, worker_details: dict | None) -> None:
        """
        If worker environment details was returned from the endpoint
        and an exception was encountered, we will log/print to console
        the mismatch information
        """
        if not worker_details:
            return

        worker_ep_id = worker_details.get("endpoint_id")
        if worker_ep_id in self._version_mismatch_already_warned_eps:
            return

        check_result = check_version(worker_details)
        if check_result is not None:
            logger.warning(check_result)
            self._version_mismatch_already_warned_eps.add(worker_ep_id)

    def _update_task_table(
        self,
        return_msg: str | t.Dict,
        task_id: str,
    ):
        """
        Parses the return message from the service and updates the
        internal _task_status_table

        Parameters
        ----------

        return_msg : str | t.Dict
           Return message received from the Globus Compute service
        task_id : str
           task id string
        """
        if isinstance(return_msg, str):
            r_dict = json.loads(return_msg)
        else:
            r_dict = return_msg

        r_status = r_dict.get("status", "unknown").lower()
        pending = r_status not in ("success", "failed")
        status = {"pending": pending, "status": r_status}
        if "details" in r_dict:
            status["details"] = r_dict["details"]

        if not pending:
            # We are tolerant on the other fields but task_id should be there
            if task_id != r_dict.get("task_id"):
                err_msg = f"Task {task_id} returned invalid response: ({r_dict})"
                logger.error(err_msg)
                raise ValueError(err_msg)

            completion_t = r_dict.get("completion_t", "unknown")
            self._log_version_mismatch(r_dict.get("details"))
            if "result" not in r_dict and "exception" not in r_dict:
                status["reason"] = r_dict.get("reason", "unknown")
            elif "result" in r_dict:
                try:
                    r_obj = self.fx_serializer.deserialize(r_dict["result"])
                except Exception:
                    raise SerializationError("Result Object Deserialization")
                else:
                    status.update({"result": r_obj, "completion_t": completion_t})
            elif "exception" in r_dict:
                raise TaskExecutionFailed(r_dict["exception"], completion_t)
            else:
                raise NotImplementedError("unreachable")

        self._task_status_table[task_id] = status
        return status

    @requires_login
    def get_task(self, task_id):
        """Get a Globus Compute task.

        Parameters
        ----------
        task_id : str
            UUID of the task

        Returns
        -------
        dict
            Task block containing "status" key.
        """
        task = self._task_status_table.get(task_id, {})
        if task.get("pending", True) is False:
            return task

        r = self.web_client.get_task(task_id)
        logger.debug(f"Response string : {r}")
        return self._update_task_table(r.text, task_id)

    @requires_login
    def get_result(self, task_id):
        """Get the result of a Globus Compute task

        Parameters
        ----------
        task_id: str
            UUID of the task

        Returns
        -------
        Result obj: If task completed

        Raises
        ------
        Exception obj: Exception due to which the task failed
        """
        task = self.get_task(task_id)
        if task["pending"] is True:
            raise TaskPending(task["status"])
        else:
            if "result" in task:
                return task["result"]
            else:
                logger.warning("We have an exception : {}".format(task["exception"]))
                task["exception"].reraise()

    @requires_login
    def get_batch_result(self, task_id_list):
        """Request status for a batch of task_ids"""
        assert isinstance(
            task_id_list, list
        ), "get_batch_result expects a list of task ids"

        pending_task_ids = [
            task_id
            for task_id in task_id_list
            if self._task_status_table.get(task_id, {}).get("pending", True) is True
        ]

        results = {}

        if pending_task_ids:
            r = self.web_client.get_batch_status(pending_task_ids)
            logger.debug(f"Response string : {r}")

        pending_task_ids = set(pending_task_ids)

        for task_id in task_id_list:
            if task_id in pending_task_ids:
                try:
                    data = r["results"][task_id]
                    rets = self._update_task_table(data, task_id)
                    results[task_id] = rets
                except KeyError:
                    logger.debug("Task {} info was not available in the batch status")
                except Exception:
                    logger.exception(
                        "Failure while unpacking results fom get_batch_result"
                    )
            else:
                results[task_id] = self._task_status_table[task_id]

        return results

    @requires_login
    def run(
        self, *args, endpoint_id: UUID_LIKE_T, function_id: UUID_LIKE_T, **kwargs
    ) -> str:
        """Initiate an invocation

        Parameters
        ----------
        *args : Any
            Args as specified by the function signature
        endpoint_id : uuid str
            Endpoint UUID string. Required
        function_id : uuid str
            Function UUID string. Required

        Returns
        -------
        task_id : str
        UUID string that identifies the task
        """
        batch = self.create_batch()
        batch.add(function_id, args, kwargs)
        r = self.batch_run(endpoint_id, batch)

        return r["tasks"][function_id][0]

    def create_batch(
        self,
        task_group_id: UUID_LIKE_T | None = None,
        resource_specification: dict[str, t.Any] | None = None,
        user_endpoint_config: dict[str, t.Any] | None = None,
        create_websocket_queue: bool = False,
    ) -> Batch:
        """
        Create a Batch instance to handle batch submission in Globus Compute

        Parameters
        ----------

        endpoint_id : UUID-like
            ID of the endpoint where the tasks in this batch will be executed

        task_group_id : UUID-like (optional)
            Associate this batch with a pre-existing Task Group. If there is no Task
            Group associated with the given ID, or the user is not authorized to use
            it, the services will respond with an error.
            If task_group_id is not specified, the services will create a Task Group.

        resource_specification: dict (optional)
            Specify resource requirements for individual task execution.

        user_endpoint_config: dict (optional)
            User endpoint configuration values as described and allowed by endpoint
            administrators

        create_websocket_queue : bool
            Whether to create a websocket queue for the task_group_id if
            it isn't already created

        Returns
        -------
        Batch instance
        """
        return Batch(
            task_group_id,
            resource_specification,
            user_endpoint_config,
            create_websocket_queue,
            serializer=self.fx_serializer,
            user_runtime=UserRuntime(
                globus_compute_sdk_version=__version__,
                globus_sdk_version=__version_globus__,
                python_version=sys.version,
            ),
        )

    @requires_login
    def batch_run(
        self, endpoint_id: UUID_LIKE_T, batch: Batch
    ) -> dict[str, str | dict[str, list[str]]]:
        """Initiate a batch of tasks to Globus Compute

        :param endpoint_id: The endpoint identifier to which to send the batch
        :param batch: a Batch object
        :return: a dictionary with the following keys:

          - tasks: a mapping of function IDs to lists of task IDs

          - request_id: arbitrary unique string the web-service assigns this request
            (only intended for help with support requests)

          - task_group_id: the task group identifier associated with the submitted tasks

          - endpoint_id: the endpoint the tasks were submitted to
        """
        if not batch:
            raise ValueError("No tasks specified for batch run")

        # Send the data to Globus Compute
        return self.web_client.submit(endpoint_id, batch.prepare()).data

    @requires_login
    def register_endpoint(
        self,
        name,
        endpoint_id: UUID_LIKE_T | None,
        metadata: dict | None = None,
        multi_user: bool | None = None,
        display_name: str | None = None,
        allowed_functions: list[UUID_LIKE_T] | None = None,
        auth_policy: UUID_LIKE_T | None = None,
        subscription_id: UUID_LIKE_T | None = None,
        public: bool | None = None,
    ):
        """Register an endpoint with the Globus Compute service.

        Parameters
        ----------
        name : str
            Name of the endpoint
        endpoint_id : str | UUID | None
            The uuid of the endpoint
        metadata : dict | None
            Endpoint metadata
        multi_user : bool | None
            Whether the endpoint supports multiple users
        display_name : str | None
            The display name of the endpoint
        allowed_functions: list[str | UUID] | None
            List of functions that are allowed to be run on the endpoint
        auth_policy: str | UUID | None
            Endpoint users are evaluated against this Globus authentication policy
        subscription_id : str | UUID | None
            Subscription ID to associate endpoint with
        public : bool | None
            Indicates if all users can discover the multi-user endpoint.

        Returns
        -------
        dict
            {'endpoint_id' : <>,
             'address' : <>,
             'client_ports': <>}
        """
        self.version_check()

        r = self.web_client.register_endpoint(
            endpoint_name=name,
            endpoint_id=endpoint_id,
            metadata=metadata,
            multi_user=multi_user,
            display_name=display_name,
            allowed_functions=allowed_functions,
            auth_policy=auth_policy,
            subscription_id=subscription_id,
            public=public,
        )
        return r.data

    @requires_login
    def get_result_amqp_url(self) -> dict[str, str]:
        r = self.web_client.get_result_amqp_url()
        return r.data

    @requires_login
    def get_containers(self, name, description=None):
        """
        Register a DLHub endpoint with the Globus Compute service and get
        the containers to launch.

        Parameters
        ----------
        name : str
            Name of the endpoint
        description : str
            Description of the endpoint

        Returns
        -------
        int
            The port to connect to and a list of containers
        """
        data = {"endpoint_name": name, "description": description}

        r = self.web_client.post("/v2/get_containers", data=data)
        return r.data["endpoint_uuid"], r.data["endpoint_containers"]

    @requires_login
    def get_container(self, container_uuid, container_type):
        """Get the details of a container for staging it locally.

        Parameters
        ----------
        container_uuid : str
            UUID of the container in question
        container_type : str
            The type of containers that will be used (Singularity, Shifter, Docker)

        Returns
        -------
        dict
            The details of the containers to deploy
        """
        self.version_check()

        r = self.web_client.get(f"/v2/containers/{container_uuid}/{container_type}")
        return r.data["container"]

    @requires_login
    def get_endpoint_status(self, endpoint_uuid):
        """Get the status reports for an endpoint.

        Parameters
        ----------
        endpoint_uuid : str
            UUID of the endpoint in question

        Returns
        -------
        dict
            The details of the endpoint's stats
        """
        r = self.web_client.get_endpoint_status(endpoint_uuid)
        return r.data

    @requires_login
    def get_endpoint_metadata(self, endpoint_uuid):
        """Get the metadata for an endpoint.

        Parameters
        ----------
        endpoint_uuid : str
            UUID of the endpoint in question

        Returns
        -------
        dict
            Informational fields about the metadata, such as IP, hostname, and
            configuration values. If there were any issues deserializing this data, may
            also include an "errors" key.
        """
        r = self.web_client.get_endpoint_metadata(endpoint_uuid)
        return r.data

    @requires_login
    def get_endpoints(self):
        """Get a list of all endpoints owned by the current user across all systems.

        Returns
        -------
        list
            A list of dictionaries which contain endpoint info
        """
        r = self.web_client.get_endpoints()
        return r.data

    @requires_login
    def register_function(
        self,
        function,
        function_name=None,
        container_uuid=None,
        description=None,
        metadata: dict | None = None,
        public=False,
        group=None,
        searchable=None,
    ) -> str:
        """Register a function code with the Globus Compute service.

        Parameters
        ----------
        function : Python Function
            The function to be registered for remote execution
        function_name : str
            The entry point (function name) of the function. Default: None
        container_uuid : str
            Container UUID from registration with Globus Compute
        description : str
            Description of the file
        metadata : dict
            Function metadata (E.g., Python version used when serializing the function)
        public : bool
            Whether or not the function is publicly accessible. Default = False
        group : str
            A globus group uuid to share this function with
        searchable : bool
            If true, the function will be indexed into globus search with the
            appropriate permissions

            DEPRECATED - ingesting functions to Globus Search is not currently supported

        Returns
        -------
        function uuid : str
            UUID identifier for the registered function
        """
        if searchable is not None:
            warnings.warn(
                "The 'searchable' argument is deprecated and no longer functional. "
                "It will be removed in a future release."
            )

        data = FunctionRegistrationData(
            function=function,
            container_uuid=container_uuid,
            description=description,
            metadata=FunctionRegistrationMetadata(**metadata) if metadata else None,
            public=public,
            group=group,
            serializer=self.fx_serializer,
        )
        logger.info(f"Registering function : {data}")
        r = self.web_client.register_function(data)
        return r.data["function_uuid"]

    @requires_login
    def get_function(self, function_id: UUID_LIKE_T):
        """Submit a request for details about a registered function.

        Parameters
        ----------
        function_id : UUID | str
            UUID of the registered function

        Returns
        -------
        dict
            Information about the registered function, such as name, description,
            serialized source code, python version, etc.
        """
        r = self.web_client.get_function(function_id)
        return r.data

    @requires_login
    def register_container(self, location, container_type, name="", description=""):
        """Register a container with the Globus Compute service.

        Parameters
        ----------
        location : str
            The location of the container (e.g., its docker url). Required
        container_type : str
            The type of containers that will be used (Singularity, Shifter, Docker).
            Required

        name : str
            A name for the container. Default = ''
        description : str
            A description to associate with the container. Default = ''

        Returns
        -------
        str
            The id of the container
        """
        payload = {
            "name": name,
            "location": location,
            "description": description,
            "type": container_type,
        }

        r = self.web_client.post("/v2/containers", data=payload)
        return r.data["container_id"]

    @requires_login
    def build_container(self, container_spec):
        """
        Submit a request to build a docker image based on a container spec. This
        container build service is based on repo2docker, so the spec reflects features
        supported by it.

        Only members of a managed globus group are allowed to use this service at
        present. This call will throw a ContainerBuildForbidden exception if you are
        not a member of this group.

        Parameters
        ----------
        container_spec : globus_compute_sdk.sdk.container_spec.ContainerSpec
            Complete specification of what goes into the container

        Returns
        -------
        str
            UUID of the container which can be used to register your function

        Raises
        ------
        ContainerBuildForbidden
            User is not in the globus group that protects the build
        """
        r = self.web_client.post("/v2/containers/build", data=container_spec.to_json())
        return r.data["container_id"]

    def get_container_build_status(self, container_id):
        r = self.web_client.get(f"/v2/containers/build/{container_id}")
        if r.http_status == 200:
            return r["status"]
        elif r.http_status == 404:
            raise ValueError(f"Container ID {container_id} not found")
        else:
            message = (
                f"Exception in fetching build status. HTTP Error Code "
                f"{r.http_status}, {r.http_reason}"
            )
            logger.error(message)
            raise SystemError(message)

    @requires_login
    def add_to_whitelist(self, endpoint_id, function_ids):
        """Adds the function to the endpoint's whitelist

        Parameters
        ----------
        endpoint_id : str
            The uuid of the endpoint
        function_ids : list
            A list of function id's to be whitelisted

        Returns
        -------
        json
            The response of the request
        """
        return self.web_client.whitelist_add(endpoint_id, function_ids)

    @requires_login
    def get_whitelist(self, endpoint_id):
        """List the endpoint's whitelist

        Parameters
        ----------
        endpoint_id : str
            The uuid of the endpoint

        Returns
        -------
        json
            The response of the request
        """
        return self.web_client.get_whitelist(endpoint_id)

    @requires_login
    def delete_from_whitelist(self, endpoint_id, function_ids):
        """List the endpoint's whitelist

        Parameters
        ----------
        endpoint_id : str
            The uuid of the endpoint
        function_ids : list
            A list of function id's to be whitelisted

        Returns
        -------
        json
            The response of the request
        """
        if not isinstance(function_ids, list):
            function_ids = [function_ids]
        res = []
        for fid in function_ids:
            res.append(self.web_client.whitelist_remove(endpoint_id, fid))
        return res

    @requires_login
    def stop_endpoint(self, endpoint_id: str):
        """Stop an endpoint by dropping it's active connections.

        Parameters
        ----------
        endpoint_id : str
            The uuid of the endpoint

        Returns
        -------
        json
            The response of the request
        """
        return self.web_client.stop_endpoint(endpoint_id)

    @requires_login
    def delete_endpoint(self, endpoint_id: str):
        """Delete an endpoint

        Parameters
        ----------
        endpoint_id : str
            The uuid of the endpoint

        Returns
        -------
        json
            The response of the request
        """
        return self.web_client.delete_endpoint(endpoint_id)

    @requires_login
    def delete_function(self, function_id: str):
        """Delete a function

        Parameters
        ----------
        function_id : str
            The UUID of the function

        Returns
        -------
        json
            The response of the request
        """
        return self.web_client.delete_function(function_id)

    @requires_login
    def get_worker_hardware_details(self, endpoint_id: UUID_LIKE_T) -> str:
        """
        Run a function to get hardware details. Returns a task ID; when that task is
        finished, the result of the run will be a string containing hardware
        information on the nodes that the endpoint workers are running on. For
        example::

            from globus_compute_sdk import Client
            gcc = Client()
            task_id = gcc.get_worker_hardware_details(ep_uuid)
            # wait some time...
            print(gcc.get_result(task_id))
        """

        function_id = self.register_function(run_hardware_report)
        return self.run(endpoint_id=endpoint_id, function_id=function_id)
