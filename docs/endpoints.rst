Globus Compute Endpoints
========================

A Globus Compute Endpoint is a persistent service launched by the user on a compute system to serve as a conduit for
executing functions on that computer. Globus Compute supports a range of target systems, enabling
an endpoint to be deployed on a laptop, the login node of a campus cluster, a cloud instance,
or a Kubernetes cluster, for example.

The endpoint requires outbound network connectivity. That is, it must be able to connect to
Globus Compute at `compute.api.globus.org <https://compute.api.globus.org/v2>`_.

The Globus Compute Endpoint package `is available on PyPI <https://pypi.org/project/globus-compute-endpoint/>`_
(and thus available via ``pip``). However, *we strongly recommend installing the
Globus Compute endpoint into an isolated virtual environment*.
`Pipx <https://pypa.github.io/pipx/installation/>`_ automatically manages
package-specific virtual environments for command line applications, so install Globus Compute endpoint via:

.. code-block:: console

   $ python3 -m pipx install globus-compute-endpoint

.. note::

   Please note that the Globus Compute endpoint is only supported on Linux.


After installing the Globus Compute endpoint, use the ``globus-compute-endpoint`` command to manage existing endpoints.


First time setup
----------------

You will be required to authenticate the first time you run ``globus-compute-endpoint``.
If you have authenticated previously, the endpoint will cache access tokens in
the local configuration file.

Globus Compute requires authentication in order to associate endpoints with
users and that ensure only the authorized user can run tasks on that endpoint.
As part of this step, we request access to your identity and Globus Groups.

To get started, you will first want to configure a new endpoint:

.. code-block:: console

   $ globus-compute-endpoint configure

Once you've run this command, a directory will be created at ``$HOME/.globus_compute`` and a set of default configuration files will be generated.

You can also set up auto-completion for the ``globus-compute-endpoint`` commands in your shell, by using the command:

.. code-block:: console

   $ globus-compute-endpoint --install-completion [zsh bash fish ...]


Configuring an Endpoint
-----------------------

Globus Compute endpoints act as gateways to diverse computational resources, including clusters, clouds,
supercomputers, and even your laptop. To make the best use of your resources, the endpoint must be
configured to match the capabilities of the resource on which it is deployed.

Globus Compute provides a Python class-based configuration model that allows you to specify the shape of the
resources (number of nodes, number of cores per worker, walltime, etc.) as well as allowing you to place
limits on how Globus Compute may scale the resources in response to changing workload demands.

To generate the appropriate directories and default configuration template, run the following command:

.. code-block:: console

   $ globus-compute-endpoint configure <ENDPOINT_NAME>

This command will create a profile for your endpoint in ``$HOME/.globus_compute/<ENDPOINT_NAME>/`` and will instantiate a
``config.yaml`` file. This file should be updated with the appropriate configurations for the computational system you are
targeting before you start the endpoint.
Globus Compute is configured using a :class:`~compute_endpoint.endpoint.utils.config.Config` object.
Globus Compute uses `Parsl <https://parsl-project.org>`_ to manage resources. For more information,
see the :class:`~compute_endpoint.endpoint.utils.config.Config` class documentation and the
`Parsl documentation <https://parsl.readthedocs.io/en/stable/userguide/overview.html>`_ .

.. note:: If the ENDPOINT_NAME is not specified, a default endpoint named "default" is configured.


Starting an Endpoint
--------------------

To start a new endpoint run the following command:

.. code-block:: console

   $ globus-compute-endpoint start <ENDPOINT_NAME>

.. note:: If the ENDPOINT_NAME is not specified, a default endpoint named "default" is started.

Starting an endpoint will perform a registration process with Globus Compute.
The registration process provides Globus Compute with information regarding the endpoint.
The endpoint also establishes an outbound connection to RabbitMQ to retrieve tasks, send results, and communicate command information.
Thus, the Globus Compute endpoint requires outbound access to the Globus Compute services over HTTPS (port 443) and AMQPS (port 5671).

Once started, the endpoint uses a daemon process to run in the background.

.. note:: If the endpoint was not stopped correctly previously (e.g., after a computer restart when the endpoint was running), the endpoint directory will be cleaned up to allow a fresh start

.. warning::

    Only the owner of an endpoint is authorized to start an endpoint. Thus if you register an endpoint
    using one identity and try to start an endpoint owned by another identity, it will fail.


To start an endpoint using a client identity, rather than as a user, you can export the
``GLOBUS_COMPUTE_CLIENT_ID`` and ``GLOBUS_COMPUTE_CLIENT_SECRET`` environment variables.
This is explained in detail in :ref:`client credentials with globus compute clients`.


Stopping an Endpoint
--------------------

To stop an endpoint, run the following command:

.. code-block:: console

   $ globus-compute-endpoint stop <ENDPOINT_NAME>

If the endpoint is not running and was stopped correctly previously, this command does nothing.

If the endpoint is not running but was not stopped correctly previously (e.g., after a computer restart
when the endpoint was running), this command will clean up the endpoint directory such that the endpoint
can be started cleanly again.

.. note:: If the ENDPOINT_NAME is not specified, the default endpoint is stopped.


Listing Endpoints
-----------------

To list available endpoints on the current system, run:

.. code-block:: console

   $ globus-compute-endpoint list
   +---------------+-------------+--------------------------------------+
   | Endpoint Name |   Status    |             Endpoint ID              |
   +===============+=============+======================================+
   | default       | Active      | 1e999502-b434-49a2-a2e0-d925383d2dd4 |
   +---------------+-------------+--------------------------------------+
   | KNL_test      | Inactive    | 8c01d13c-cfc1-42d9-96d2-52c51784ea16 |
   +---------------+-------------+--------------------------------------+
   | gpu_cluster   | Initialized | None                                 |
   +---------------+-------------+--------------------------------------+

Endpoints can be the following states:

* **Initialized**: The endpoint has been created, but not started
  following configuration and is not registered with the `Globus Compute service`.
* **Running**: The endpoint is active and available for executing  functions.
* **Stopped**: The endpoint was stopped by the user. It is not running
  and therefore, cannot service any functions. It can be started again without issues.
* **Disconnected**: The endpoint disconnected unexpectedly. It is not running
  and therefore, cannot service any functions. Starting this endpoint will first invoke
  necessary endpoint cleanup, since it was not stopped correctly previously.


Ensuring execution environment
------------------------------

When running a function, endpoint worker processes expect to have all the necessary
dependencies readily available to them. For example, if a function uses ``numpy`` to do
some calculations, and a worker is running on a machine without ``numpy`` installed, any
attempts to execute that function using that worker will result in an error.

In HPC contexts, the endpoint process - which receives tasks from the Compute central
services and queues them up for execution - generally runs on a separate node from the
workers which actually do the computation. As a result, it's often necessary to load in
some kind of pre-initialized environment. In general there are two solutions here:

1. Python based environment isolation such as ``conda`` environment or ``venv``,
2. Containers: containerization with ``docker`` or ``apptainer`` (``singularity``)

.. note::
   Please note that worker environment is required to have the ``globus-compute-endpoint`` python
   module installed. We recommend matching the Python version and ``globus-compute-endpoint`` module
   version on the worker environment to that on the endpoint itself.


Python based environment isolation
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

To use python based environment management, use the |worker_init|_ config option:

.. code-block:: yaml

  engine:
    provider:
        worker_init: conda activate my-conda-env

The exact behavior of ``worker_init`` depends on the |Provider|_ being used.

In some cases, it may also be helpful to run some setup during the startup process of
the endpoint itself, before any workers start. This can be achieved using the top-level
``endpoint_setup`` config option:

.. code-block:: yaml

  endpoint_setup: |
    conda create -n my-conda-env
    conda activate my-conda-env
    pip install -r requirements.txt

Note that ``endpoint_setup`` is run by the system shell, as a child of the endpoint
startup process.

Similarly, artifacts created by ``endpoint_setup`` can be cleaned up with
``endpoint_teardown``:

.. code-block:: yaml

  endpoint_teardown: |
    conda remove -n my-conda-env --all

Containerized Environments
^^^^^^^^^^^^^^^^^^^^^^^^^^

Container support is limited to ``GlobusComputeEngine`` on the ``globus-compute-endpoint``. To
configure the endpoint the following options are now supported:

* `container_type` : Specify container type from one of ``('docker', 'apptainer', 'singularity', 'custom', 'None')``
* `container_uri`: Specify container uri, or file path if specifying sif files
* `container_cmd_options`: Specify custom command options to pass to the container launch command, such as filesystem mount paths, network options etc.

.. code-block:: yaml

    display_name: Docker
    engine:
      type: GlobusComputeEngine
      container_type: docker
      container_uri: funcx/kube-endpoint:main-3.10
      container_cmd_options: -v /tmp:/tmp
      provider:
        init_blocks: 1
        max_blocks: 1
        min_blocks: 0
        type: LocalProvider

For more custom use-cases where either an unsupported container technology is required
or building the container string programmatically is preferred use ``container_type='custom'``
In this case, `container_cmd_options` is treated as a string template, in which the following
two strings are expected:

* `{EXECUTOR_RUNDIR}` : Used to specify mounting of the RUNDIR to share logs
* `{EXECUTOR_LAUNCH_CMD}` : Used to specify the worker launch command within the container.

Here's an example:

.. code-block:: yaml

    display_name: Docker Custom
    engine:
      type: GlobusComputeEngine
      container_type: custom
      container_cmd_options: docker run -v {EXECUTOR_RUNDIR}:{EXECUTOR_RUNDIR} funcx/kube-endpoint:main-3.10 {EXECUTOR_LAUNCH_CMD}
      provider:
        init_blocks: 1
        max_blocks: 1
        min_blocks: 0
        type: LocalProvider

Restarting endpoint when machine restarts
-----------------------------------------

To ensure that a compute endpoint comes back online when its host machine restarts, a
systemd service can be configured to run the endpoint.

1. Ensure the endpoint isn't running:

   .. code-block:: console

      $ globus-compute-endpoint stop <my-endpoint-name>

2. Update the endpoint's ``config.yaml`` to set ``detach_endpoint`` to ``false``

3. Create a service file at ``/etc/systemd/system/my-globus-compute-endpoint.service``,
   and populate it with the following settings:

   .. code-block:: cfg

      [Unit]
      Description=Globus Compute Endpoint systemd service
      After=network.target
      StartLimitIntervalSec=0

      [Service]
      ExecStart=</full/path/to/globus-compute-endpoint/executable> start <my-endpoint-name>
      User=<user_account>
      Type=simple
      Restart=always
      RestartSec=1

      [Install]
      WantedBy=multi-user.target

4. Enable the service and start the endpoint:

   .. code-block:: console

      $ sudo systemctl enable my-globus-compute-endpoint.service --now

To edit an existing systemd service, make changes to the service file and then run the
following:

.. code-block:: console

   $ sudo systemctl daemon-reload
   $ sudo systemctl restart my-globus-compute-endpoint.service


AMQP Port
---------

Endpoints receive tasks and communicate task results via the AMQP messaging protocol.
As of v2.11.0, newly configured endpoints use AMQP over port 443 by default, since
firewall rules usually leave that port open. In case 443 is not open on a particular
cluster, the port to use can be changed in the endpoint config via the ``amqp_port``
option, like so:

.. code-block:: yaml

  amqp_port: 5671
  display_name: My Endpoint
  engine: ...

Note that only ports 5671, 5672, and 443 are supported with the Compute hosted services.
Also note that when ``amqp_port`` is omitted from the config, the port is based on the
connection URL the endpoint receives after registering itself with the services, which
typically means port 5671.


Example configurations
----------------------

.. include:: configuring.rst

.. |worker_init| replace:: ``worker_init``
.. _worker_init: https://parsl.readthedocs.io/en/stable/stubs/parsl.providers.SlurmProvider.html#parsl.providers.SlurmProvider#:~:text=worker_init%20%28str%29,env%E2%80%99

.. |Provider| replace:: ``ExecutionProvider``
.. _Provider: https://parsl.readthedocs.io/en/stable/stubs/parsl.providers.base.ExecutionProvider.html
