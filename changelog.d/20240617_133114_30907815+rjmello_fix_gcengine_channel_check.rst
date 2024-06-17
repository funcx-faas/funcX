Bug Fixes
^^^^^^^^^

- We no longer raise an exception when using the ``GlobusComputeEngine`` with Parsl
  providers that do not utilize ``Channel`` objects (e.g., ``KubernetesProvider``).