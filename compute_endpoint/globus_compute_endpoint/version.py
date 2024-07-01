# single source of truth for package version,
# see https://packaging.python.org/en/latest/single_source_version/
__version__ = "2.23.0a0"

# TODO: remove after a `globus-compute-sdk` release
# this is needed because it's imported by `globus-compute-sdk` to do the version check
VERSION = __version__

# app name to send as part of requests
app_name = f"Globus Compute Endpoint v{__version__}"
