from __future__ import annotations

import json
import logging
import os
import typing as t
import uuid
import warnings

from globus_compute_sdk.errors import (
    SerializationError,
    TaskExecutionFailed,
    TaskPending,
)
from globus_compute_sdk.sdk._environments import get_web_service_url
from globus_compute_sdk.sdk.utils.uuid_like import UUID_LIKE_T
from globus_compute_sdk.sdk.web_client import FunctionRegistrationData
from globus_compute_sdk.serialize import ComputeSerializer, SerializationStrategy
from globus_compute_sdk.version import __version__, compare_versions

from .batch import Batch
from .login_manager import LoginManager, LoginManagerProtocol, requires_login

logger = logging.getLogger(__name__)

_FUNCX_HOME = os.path.join("~", ".globus_compute")


class Client:
    """Main class for interacting with the Globus Compute service

    Holds helper operations for performing common tasks with the Globus Compute service.
    """

    FUNCX_SDK_CLIENT_ID = os.environ.get(
        "FUNCX_SDK_CLIENT_ID", "4cf29807-cf21-49ec-9443-ff9a3fb9f81c"
    )
    FUNCX_SCOPE = os.environ.get(
        "FUNCX_SCOPE",
        "https://auth.globus.org/scopes/facd7ccc-c5f4-42aa-916b-a0e270e2c2a9/all",
    )

    def __init__(
        self,
        http_timeout=None,
        funcx_home=_FUNCX_HOME,
        environment: str | None = None,
        task_group_id: t.Union[None, uuid.UUID, str] = None,
        funcx_service_address: str | None = None,
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

        environment: str
            For internal use only. The name of the environment to use. Sets
            funcx_service_address appropriately, unless already set.

        funcx_service_address: str
            For internal use only. The address of the web service.

        do_version_check: bool
            Set to ``False`` to skip the version compatibility check on client
            initialization
            Default: True

        task_group_id: str|uuid.UUID
            Set the TaskGroup ID (a UUID) for this Client instance.
            Typically, one uses this to submit new tasks to an existing
            session or to reestablish Executor futures.
            Default: None (will be auto generated)

        code_serialization_strategy: SerializationStrategy
            Strategy to use when serializing function code. If None,
            globus_compute_sdk.serialize.DEFAULT_STRATEGY_CODE will be used.

        data_serialization_strategy: SerializationStrategy
            Strategy to use when serializing function arguments. If None,
            globus_compute_sdk.serialize.DEFAULT_STRATEGY_DATA will be used.

        Keyword arguments are the same as for BaseClient.

        """
        # resolve URLs if not set
        if funcx_service_address is None:
            funcx_service_address = get_web_service_url(environment)

        self._task_status_table: t.Dict[str, t.Dict] = {}
        self.funcx_home = os.path.expanduser(funcx_home)

        for arg, name in [(http_timeout, "http_timeout")]:
            if arg is not None:
                msg = (
                    f"The '{name}' argument is deprecated. "
                    "It will be removed in a future release."
                )
                warnings.warn(msg)

        # if a login manager was passed, no login flow is triggered
        if login_manager is not None:
            self.login_manager: LoginManagerProtocol = login_manager
        # but if login handling is implicit (as when no login manager is passed)
        # then ensure that the user is logged in
        else:
            self.login_manager = LoginManager(environment=environment)
            self.login_manager.ensure_logged_in()

        self.web_client = self.login_manager.get_web_client(
            base_url=funcx_service_address
        )
        self.fx_serializer = ComputeSerializer(
            strategy_code=code_serialization_strategy,
            strategy_data=data_serialization_strategy,
        )

        self.funcx_service_address = funcx_service_address

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

    def _update_task_table(self, return_msg: str | t.Dict, task_id: str):
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

        if not pending:
            if "result" not in r_dict and "exception" not in r_dict:
                raise ValueError("non-pending result is missing result data")
            completion_t = r_dict["completion_t"]
            if "result" in r_dict:
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
        rets = self._update_task_table(r.text, task_id)
        return rets

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
    def run(self, *args, endpoint_id=None, function_id=None, **kwargs) -> str:
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
        assert endpoint_id is not None, "endpoint_id key-word argument must be set"
        assert function_id is not None, "function_id key-word argument must be set"

        batch = self.create_batch(endpoint_id)
        batch.add(function_id, args, kwargs)
        r = self.batch_run(batch)

        return r["tasks"][function_id][0]

    def create_batch(
        self,
        endpoint_id: UUID_LIKE_T,
        task_group_id: UUID_LIKE_T | None = None,
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

        create_websocket_queue : bool
            Whether to create a websocket queue for the task_group_id if
            it isn't already created

        Returns
        -------
        Batch instance
        """
        return Batch(
            endpoint_id,
            task_group_id,
            create_websocket_queue,
            serializer=self.fx_serializer,
        )

    @requires_login
    def batch_run(self, batch: Batch) -> dict[str, str | list[str]]:
        """
        Initiate a batch of tasks to Globus Compute

        :param batch: a Batch object

        Returns
        -------
        dictionary with the following keys:
            tasks: a mapping of function IDs to lists of task IDs
            request_id: arbitrary unique string the web-service assigns this request
                (only intended for help with support requests)
            task_group_id: the task group identifier associated with the submitted tasks
            endpoint_id: the endpoint the tasks were submitted to
        """
        if not batch:
            raise ValueError("No tasks specified for batch run")

        # Send the data to Globus Compute
        return self.web_client.submit(batch.prepare()).data

    @requires_login
    def register_endpoint(
        self,
        name,
        endpoint_id,
        metadata=None,
        multi_tenant=False,
        display_name=None,
    ):
        """Register an endpoint with the Globus Compute service.

        Parameters
        ----------
        :param name str Name of the endpoint
        :param endpoint_id str The uuid of the endpoint
        :param metadata dict endpoint metadata
        :param multi_tenant bool Whether the endpoint supports multiple users

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
            multi_tenant=multi_tenant,
            display_name=display_name,
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
            public=public,
            group=group,
            serializer=self.fx_serializer,
        )
        logger.info(f"Registering function : {data}")
        r = self.web_client.register_function(data)
        return r.data["function_uuid"]

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
