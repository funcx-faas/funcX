New Functionality
^^^^^^^^^^^^^^^^^

- The engine that renders user endpoint config files now receives information about
  the runtime environment used to submit tasks, such as Python environment and Globus
  Compute SDK version, via the ``user_runtime`` variable. For a complete list of the
  fields that are sent, `reference the documentation on batch.UserRuntime. <https://globus-compute.readthedocs.io/en/latest/reference/client.html#globus_compute_sdk.sdk.batch.UserRuntime>`_
