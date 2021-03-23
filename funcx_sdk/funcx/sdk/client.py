import asyncio
import json
import os
import logging
from inspect import getsource

from globus_sdk import AuthClient

from fair_research_login import NativeClient, JSONTokenStorage

from funcx.sdk.asynchronous.funcx_task import FuncXTask
from funcx.sdk.asynchronous.polling_task import PollingTask
from funcx.sdk.search import SearchHelper, FunctionSearchResults

from funcx.serialize import FuncXSerializer
# from funcx.sdk.utils.futures import FuncXFuture
from funcx.sdk.error_handling_client import FuncXErrorHandlingClient
from funcx.sdk.utils.batch import Batch
from funcx.utils.errors import FailureResponse, VersionMismatch, SerializationError
from funcx.utils.handle_service_response import handle_response_errors

try:
    from funcx_endpoint.version import VERSION as ENDPOINT_VERSION

except ModuleNotFoundError:
    ENDPOINT_VERSION = None

from funcx.sdk import VERSION as SDK_VERSION


logger = logging.getLogger(__name__)


class FuncXClient(FuncXErrorHandlingClient):
    """Main class for interacting with the funcX service

    Holds helper operations for performing common tasks with the funcX service.
    """

    TOKEN_DIR = os.path.expanduser("~/.funcx/credentials")
    TOKEN_FILENAME = "funcx_sdk_tokens.json"
    FUNCX_SDK_CLIENT_ID = os.environ.get(
        "FUNCX_SDK_CLIENT_ID", "4cf29807-cf21-49ec-9443-ff9a3fb9f81c"
    )
    FUNCX_SCOPE = os.environ.get(
        "FUNCX_SCOPE",
        "https://auth.globus.org/scopes/facd7ccc-c5f4-42aa-916b-a0e270e2c2a9/all",
    )

    def __init__(self, http_timeout=None, funcx_home=os.path.join('~', '.funcx'),
                 force_login=False, fx_authorizer=None, search_authorizer=None,
                 openid_authorizer=None,
                 funcx_service_address='https://api.funcx.org/v1',
                 asynchronous=False,
                 loop=None,
                 **kwargs):
        """ Initialize the client

        Parameters
        ----------
        http_timeout: int
        Timeout for any call to service in seconds.
        Default is no timeout

        force_login: bool
        Whether to force a login to get new credentials.

        fx_authorizer:class:`GlobusAuthorizer <globus_sdk.authorizers.base.GlobusAuthorizer>`:
        A custom authorizer instance to communicate with funcX.
        Default: ``None``, will be created.

        search_authorizer:class:`GlobusAuthorizer <globus_sdk.authorizers.base.GlobusAuthorizer>`:
        A custom authorizer instance to communicate with Globus Search.
        Default: ``None``, will be created.

        openid_authorizer:class:`GlobusAuthorizer <globus_sdk.authorizers.base.GlobusAuthorizer>`:
        A custom authorizer instance to communicate with OpenID.
        Default: ``None``, will be created.

        funcx_service_address: str
        The address of the funcX web service to communicate with.
        Default: https://api.funcx.org/v1

        asynchronous: bool
        Should the API use asynchronous interactions with the web service? Currently
        only impacts the run method
        Default: False

        loop: AbstractEventLoop
        If asynchronous mode is requested, then you can provide an optional event loop
        instance. If None, then we will access asyncio.get_event_loop()
        Default: None

        Keyword arguments are the same as for BaseClient.
        """
        self.func_table = {}
        self.ep_registration_path = 'register_endpoint_2'
        self.funcx_home = os.path.expanduser(funcx_home)

        if not os.path.exists(self.TOKEN_DIR):
            os.makedirs(self.TOKEN_DIR)

        tokens_filename = os.path.join(self.TOKEN_DIR, self.TOKEN_FILENAME)
        self.native_client = NativeClient(client_id=self.FUNCX_SDK_CLIENT_ID,
                                          app_name="FuncX SDK",
                                          token_storage=JSONTokenStorage(tokens_filename))

        # TODO: if fx_authorizer is given, we still need to get an authorizer for Search
        search_scope = "urn:globus:auth:scope:search.api.globus.org:all"
        scopes = [self.FUNCX_SCOPE, search_scope, "openid"]

        if not fx_authorizer or not search_authorizer or not openid_authorizer:
            self.native_client.login(requested_scopes=scopes,
                                     no_local_server=kwargs.get("no_local_server", True),
                                     no_browser=kwargs.get("no_browser", True),
                                     refresh_tokens=kwargs.get("refresh_tokens", True),
                                     force=force_login)

            all_authorizers = self.native_client.get_authorizers_by_scope(requested_scopes=scopes)
            fx_authorizer = all_authorizers[self.FUNCX_SCOPE]
            search_authorizer = all_authorizers[search_scope]
            openid_authorizer = all_authorizers["openid"]

        super(FuncXClient, self).__init__("funcX",
                                          environment='funcx',
                                          authorizer=fx_authorizer,
                                          http_timeout=http_timeout,
                                          base_url=funcx_service_address,
                                          **kwargs)
        self.fx_serializer = FuncXSerializer()

        authclient = AuthClient(authorizer=openid_authorizer)
        user_info = authclient.oauth2_userinfo()
        self.searcher = SearchHelper(authorizer=search_authorizer, owner_uuid=user_info['sub'])
        self.funcx_service_address = funcx_service_address

        self.asynchronous = asynchronous
        if asynchronous:
            self.loop = loop if loop else asyncio.get_event_loop()

            # Start up an asynchronous polling loop in the background
            # Throttle to ten calls per second
            self.polling_task = PollingTask(self, self.loop)
        else:
            self.loop = None

    def version_check(self):
        """Check this client version meets the service's minimum supported version.
        """
        resp = self.get("version", params={"service": "all"})
        versions = resp.data
        if "min_ep_version" not in versions:
            raise VersionMismatch("Failed to retrieve version information from funcX service.")

        min_ep_version = versions['min_ep_version']

        if ENDPOINT_VERSION is None:
            raise VersionMismatch("You do not have the funcx endpoint installed.  You can use 'pip install funcx-endpoint'.")
        if ENDPOINT_VERSION < min_ep_version:
            raise VersionMismatch(f"Your version={ENDPOINT_VERSION} is lower than the "
                                  f"minimum version for an endpoint: {min_ep_version}.  Please update.")

    def logout(self):
        """Remove credentials from your local system
        """
        self.native_client.logout()

    def update_table(self, return_msg, task_id):
        """ Parses the return message from the service and updates the internal func_table

        Parameters
        ----------

        return_msg : str
           Return message received from the funcx service
        task_id : str
           task id string
        """
        if isinstance(return_msg, str):
            r_dict = json.loads(return_msg)
        else:
            r_dict = return_msg

        r_status = r_dict.get('status', 'unknown')
        status = {'pending': True,
                  'status': r_status}

        if 'result' in r_dict:
            try:
                r_obj = self.fx_serializer.deserialize(r_dict['result'])
                completion_t = r_dict['completion_t']
            except Exception:
                raise SerializationError("Result Object Deserialization")
            else:
                status.update({'pending': False,
                               'result': r_obj,
                               'completion_t': completion_t})
                self.func_table[task_id] = status

        elif 'exception' in r_dict:
            try:
                r_exception = self.fx_serializer.deserialize(r_dict['exception'])
                completion_t = r_dict['completion_t']
                logger.info(f"Exception : {r_exception}")
            except Exception:
                raise SerializationError("Task's exception object deserialization")
            else:
                status.update({'pending': False,
                               'exception': r_exception,
                               'completion_t': completion_t})
                self.func_table[task_id] = status
        return status

    def get_task(self, task_id):
        """Get a funcX task.

        Parameters
        ----------
        task_id : str
            UUID of the task

        Returns
        -------
        dict
            Task block containing "status" key.
        """
        if task_id in self.func_table:
            return self.func_table[task_id]

        r = self.get("tasks/{task_id}".format(task_id=task_id))
        logger.debug("Response string : {}".format(r))
        try:
            rets = self.update_table(r.text, task_id)
        except Exception as e:
            raise e
        return rets

    def get_result(self, task_id):
        """ Get the result of a funcX task

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
        if task['pending'] is True:
            raise Exception(task['status'])
        else:
            if 'result' in task:
                return task['result']
            else:
                logger.warning("We have an exception : {}".format(task['exception']))
                task['exception'].reraise()

    def get_batch_result(self, task_id_list):
        """ Request status for a batch of task_ids
        """
        assert isinstance(task_id_list, list), "get_batch_result expects a list of task ids"

        pending_task_ids = [t for t in task_id_list if t not in self.func_table]

        results = {}

        if pending_task_ids:
            payload = {'task_ids': pending_task_ids}
            r = self.post("/batch_status", json_body=payload)
            logger.debug("Response string : {}".format(r))

        pending_task_ids = set(pending_task_ids)

        for task_id in task_id_list:
            if task_id in pending_task_ids:
                try:
                    data = r['results'][task_id]
                    rets = self.update_table(data, task_id)
                    results[task_id] = rets
                except KeyError:
                    logger.debug("Task {} info was not available in the batch status")
                except Exception:
                    logger.exception("Failure while unpacking results fom get_batch_result")
            else:
                results[task_id] = self.func_table[task_id]

        return results

    def run(self, *args, endpoint_id=None, function_id=None, **kwargs):
        """Initiate an invocation

        Parameters
        ----------
        *args : Any
            Args as specified by the function signature
        endpoint_id : uuid str
            Endpoint UUID string. Required
        function_id : uuid str
            Function UUID string. Required
        asynchronous : bool
            Whether or not to run the function asynchronously

        Returns
        -------
        task_id : str
        UUID string that identifies the task if asynchronous is False

        funcX Task: asyncio.Task
        A future that will eventually resolve into the function's result if
        asynchronous is True
        """
        assert endpoint_id is not None, "endpoint_id key-word argument must be set"
        assert function_id is not None, "function_id key-word argument must be set"

        batch = self.create_batch()
        batch.add(*args, endpoint_id=endpoint_id, function_id=function_id, **kwargs)
        r = self.batch_run(batch)

        # If we are presenting the asynch interface then return a asynchio task
        # that will eventually resolve to the result
        if self.asynchronous:
            funcx_task = FuncXTask(r[0])
            asyncio_task = self.loop.create_task(funcx_task.get_result())

            # Add it to the list of tasks to be polled
            self.polling_task.put(funcx_task)
            return asyncio_task
        else:
            return r[0]

    def create_batch(self):
        """
        Create a Batch instance to handle batch submission in funcX

        Parameters
        ----------

        Returns
        -------
        Batch instance
            Status block containing "status" key.
        """
        batch = Batch()
        return batch

    def batch_run(self, batch):
        """Initiate a batch of tasks to funcX

        Parameters
        ----------
        batch: a Batch object

        Returns
        -------
        task_ids : a list of UUID strings that identify the tasks
        """
        servable_path = 'submit'
        assert isinstance(batch, Batch), "Requires a Batch object as input"
        assert len(batch.tasks) > 0, "Requires a non-empty batch"

        data = batch.prepare()

        # Send the data to funcX
        r = self.post(servable_path, json_body=data)
        task_uuids = []
        for result in r['results']:
            task_id = result['task_uuid']
            task_uuids.append(task_id)
            if result['http_status_code'] != 200:
                # this method of handling errors for a batch response is not
                # ideal, as it will raise any error in the multi-response,
                # but it will do until batch_run is deprecated in favor of Executer
                handle_response_errors(result)
        return task_uuids

    def map_run(self, *args, endpoint_id=None, function_id=None, asynchronous=False, **kwargs):
        """Initiate an invocation

        Parameters
        ----------
        *args : Any
            Args as specified by the function signature
        endpoint_id : uuid str
            Endpoint UUID string. Required
        function_id : uuid str
            Function UUID string. Required
        asynchronous : bool
            Whether or not to run the function asynchronously

        Returns
        -------
        task_id : str
        UUID string that identifies the task
        """
        servable_path = 'submit_batch'
        assert endpoint_id is not None, "endpoint_id key-word argument must be set"
        assert function_id is not None, "function_id key-word argument must be set"

        ser_kwargs = self.fx_serializer.serialize(kwargs)

        batch_payload = []
        iterator = args[0]
        for arg in iterator:
            ser_args = self.fx_serializer.serialize((arg,))
            payload = self.fx_serializer.pack_buffers([ser_args, ser_kwargs])
            batch_payload.append(payload)

        data = {'endpoints': [endpoint_id],
                'func': function_id,
                'payload': batch_payload,
                'is_async': asynchronous}

        # Send the data to funcX
        r = self.post(servable_path, json_body=data)
        return r['task_uuids']

    def register_endpoint(self, name, endpoint_uuid, metadata=None, endpoint_version=None):
        """Register an endpoint with the funcX service.

        Parameters
        ----------
        name : str
            Name of the endpoint
        endpoint_uuid : str
                The uuid of the endpoint
        metadata : dict
            endpoint metadata, see default_config example
        endpoint_version: str
            Version string to be passed to the webService as a compatibility check

        Returns
        -------
        A dict
            {'endpoint_id' : <>,
             'address' : <>,
             'client_ports': <>}
        """
        self.version_check()

        data = {
            "endpoint_name": name,
            "endpoint_uuid": endpoint_uuid,
            "version": endpoint_version
        }
        if metadata:
            data['meta'] = metadata

        r = self.post(self.ep_registration_path, json_body=data)

        # Return the result
        return r.data

    def get_containers(self, name, description=None):
        """Register a DLHub endpoint with the funcX service and get the containers to launch.

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
        registration_path = 'get_containers'

        data = {"endpoint_name": name, "description": description}

        r = self.post(registration_path, json_body=data)

        # Return the result
        return r.data['endpoint_uuid'], r.data['endpoint_containers']

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
        container_path = f'containers/{container_uuid}/{container_type}'

        r = self.get(container_path)

        # Return the result
        return r.data['container']

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
        stats_path = f'endpoints/{endpoint_uuid}/status'

        r = self.get(stats_path)

        # Return the result
        return r.data

    def register_function(self, function, function_name=None, container_uuid=None, description=None,
                          public=False, group=None, searchable=True):
        """Register a function code with the funcX service.

        Parameters
        ----------
        function : Python Function
            The function to be registered for remote execution
        function_name : str
            The entry point (function name) of the function. Default: None
        container_uuid : str
            Container UUID from registration with funcX
        description : str
            Description of the file
        public : bool
            Whether or not the function is publicly accessible. Default = False
        group : str
            A globus group uuid to share this function with
        searchable : bool
            If true, the function will be indexed into globus search with the appropriate permissions

        Returns
        -------
        function uuid : str
            UUID identifier for the registered function
        """
        registration_path = 'register_function'

        source_code = ""
        try:
            source_code = getsource(function)
        except OSError:
            logger.error("Failed to find source code during function registration.")

        serialized_fn = self.fx_serializer.serialize(function)
        packed_code = self.fx_serializer.pack_buffers([serialized_fn])

        data = {"function_name": function.__name__,
                "function_code": packed_code,
                "function_source": source_code,
                "container_uuid": container_uuid,
                "entry_point": function_name if function_name else function.__name__,
                "description": description,
                "public": public,
                "group": group,
                "searchable": searchable}

        logger.info("Registering function : {}".format(data))

        r = self.post(registration_path, json_body=data)

        func_uuid = r.data['function_uuid']

        # Return the result
        return func_uuid

    def update_function(self, func_uuid, function):
        pass

    def search_function(self, q, offset=0, limit=10, advanced=False):
        """Search for function via the funcX service

        Parameters
        ----------
        q : str
            free-form query string
        offset : int
            offset into total results
        limit : int
            max number of results to return
        advanced : bool
            allows elastic-search like syntax in query string

        Returns
        -------
        FunctionSearchResults
        """
        return self.searcher.search_function(q, offset=offset, limit=limit, advanced=advanced)

    def search_endpoint(self, q, scope='all', owner_id=None):
        """

        Parameters
        ----------
        q
        scope : str
            Can be one of {'all', 'my-endpoints', 'shared-with-me'}
        owner_id
            should be urn like f"urn:globus:auth:identity:{owner_uuid}"

        Returns
        -------

        """
        return self.searcher.search_endpoint(q, scope=scope, owner_id=owner_id)

    def register_container(self, location, container_type, name='', description=''):
        """Register a container with the funcX service.

        Parameters
        ----------
        location : str
            The location of the container (e.g., its docker url). Required
        container_type : str
            The type of containers that will be used (Singularity, Shifter, Docker). Required

        name : str
            A name for the container. Default = ''
        description : str
            A description to associate with the container. Default = ''

        Returns
        -------
        str
            The id of the container
        """
        container_path = 'containers'

        payload = {'name': name, 'location': location, 'description': description, 'type': container_type}

        r = self.post(container_path, json_body=payload)

        # Return the result
        return r.data['container_id']

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
        req_path = f'endpoints/{endpoint_id}/whitelist'

        if not isinstance(function_ids, list):
            function_ids = [function_ids]

        payload = {'func': function_ids}

        r = self.post(req_path, json_body=payload)

        # Return the result
        return r

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
        req_path = f'endpoints/{endpoint_id}/whitelist'

        r = self.get(req_path)

        # Return the result
        return r

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
            req_path = f'endpoints/{endpoint_id}/whitelist/{fid}'

            r = self.delete(req_path)
            res.append(r)

        # Return the result
        return res
