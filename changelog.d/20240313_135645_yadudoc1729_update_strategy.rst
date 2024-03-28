
Bug Fixes
^^^^^^^^^

- A bug in the ``GlobusComputeEngine`` where a faulty endpoint-config could result in
  the endpoint getting stuck repeatedly submitting jobs to the batch scheduler is now
  fixed. The endpoint now shuts down with the ``endpoint.log`` reporting the root cause
  for the submission error. Any submitted jobs will report an exception that reports
  the endpoint failure.

- Fixed bug where ``GlobusComputeEngine`` lost track of submitted jobs that failed to
  have workers connect back. The endpoint will now report a fault if multiple jobs
  have failed to connect back and shutdown, tasks submitted to the endpoint will
  return an exception.

Changed
^^^^^^^

- ``GlobusComputeEngine``'s ``strategy`` kwarg now only accepts ``str``, valid options are
  ``{'none', 'simple'}`` where ``simple`` is the default.
- The maximum duration that workers are allowed to idle when using ``GlobusComputeEngine``
  can now be configured with the new kwarg ``max_idletime`` which accepts a float and defaults
  to 120s.
