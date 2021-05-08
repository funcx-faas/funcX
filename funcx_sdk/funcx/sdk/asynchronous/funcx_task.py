import asyncio


class FuncXTask(asyncio.Future):
    """
    Represents a submitted funcX task with an asychio wrapper
    """
    def __init__(self, task_id):
        """
        Parameters
        ----------
        task_id : uuid str
            The uuid of the funcX task this instance is shadowing
        """
        super(FuncXTask, self).__init__()
        self.task_id = task_id

    def __str__(self):
        return "FuncX Task ID " + self.task_id

    async def get_result(self):
        """
        Coroutine to wait on the funcX task to complete and then return the result
        :return:
            result : Any
        """
        await self
        return self.result()
