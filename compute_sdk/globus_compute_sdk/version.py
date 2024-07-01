from globus_compute_sdk.errors import VersionMismatch
from packaging.version import Version

# single source of truth for package version,
# see https://packaging.python.org/en/latest/single_source_version/
__version__ = "2.23.0a0"


def compare_versions(
    current: str, min_version: str, *, package_name: str = "globus-compute-sdk"
) -> None:
    current_v = Version(current)
    min_v = Version(min_version)

    if (
        current_v.is_devrelease
        or min_v.is_devrelease
        and current_v.release == min_v.release
    ):
        return

    if current_v < min_v:
        raise VersionMismatch(
            f"Your version={current} is lower than the "
            f"minimum version for {package_name}: {min_version}.  "
            "Please update. "
            f"pip install {package_name}>={min_version}"
        )
