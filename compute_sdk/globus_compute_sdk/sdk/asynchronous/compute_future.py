import typing as t
from concurrent.futures import Future


class ComputeFuture(Future):
    """
    Extend `concurrent.futures.Future`_ to include an optional task UUID.

    .. _concurrent.futures.Future: https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.Future
    """  # noqa

    task_id: t.Optional[str]
    """The UUID for the task behind this Future. In batch mode, this will
    not be populated immediately, but will appear later when the task is
    submitted to the Globus Compute services."""

    _metadata: dict
    """Used to store metadata related to this Future. For example, we may
    store the ID of the submitting Executor so we can tie it back."""

    def __init__(self, task_id: t.Optional[str] = None):
        super().__init__()
        self.task_id = task_id
        self._metadata = {}
