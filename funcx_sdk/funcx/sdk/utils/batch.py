from funcx.serialize import FuncXSerializer


class Batch:
    """Utility class for creating batch submission in funcX"""

    def __init__(self, task_group_id=None):
        """
        Parameters
        ==========

        task_group_id : str
            UUID indicating the task group that this batch belongs to
        """
        self.tasks = []
        self.fx_serializer = FuncXSerializer()
        self.task_group_id = task_group_id

    def add(self, *args, endpoint_id=None, function_id=None, **kwargs):
        """Add an function invocation to a batch submission

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
        None
        """
        assert endpoint_id is not None, "endpoint_id key-word argument must be set"
        assert function_id is not None, "function_id key-word argument must be set"

        ser_args = self.fx_serializer.serialize(args)
        ser_kwargs = self.fx_serializer.serialize(kwargs)
        payload = self.fx_serializer.pack_buffers([ser_args, ser_kwargs])

        data = {'endpoint': endpoint_id,
                'function': function_id,
                'payload': payload}

        self.tasks.append(data)

    def prepare(self):
        """Prepare the payloads to be post to web service in a batch

        Parameters
        ----------

        Returns
        -------
        payloads in dictionary, Dict[str, list]
        """
        data = {
            'task_group_id': self.task_group_id,
            'tasks': []
        }

        for task in self.tasks:
            new_task = (task['function'], task['endpoint'], task['payload'])
            data['tasks'].append(new_task)

        return data
