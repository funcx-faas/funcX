import os
from pathlib import Path

from setuptools import find_packages, setup

REQUIRES = [
    "requests>=2.31.0,<3",
    "globus-sdk",  # version will be bounded by `globus-compute-sdk`
    "globus-compute-sdk==2.22.0",
    "globus-compute-common==0.4.1",
    "globus-identity-mapping==0.3.0",
    # table printing used in list-endpoints
    "texttable>=1.6.4,<2",
    # although psutil does not declare itself to use semver, it appears to offer
    # strong backwards-compatibility promises based on its changelog, usage, and
    # history
    #
    # TODO: re-evaluate bound after we have an answer of some kind from psutil
    # see:
    #   https://github.com/giampaolo/psutil/issues/2002
    "psutil<6",
    # provides easy daemonization of the endpoint
    "python-daemon>=2,<3",
    # CLI parsing
    "click>=8,<9",
    "click-option-group>=0.5.6,<1",
    # disallow use of 22.3.0; the whl package on some platforms causes ZMQ issues
    #
    # NOTE: 22.3.0 introduced a patched version of libzmq.so to the wheel packaging
    # which may be the source of the problems , the problem can be fixed by
    # building from source, which may mean there's an issue in the packaged library
    # further investigation may be needed if the issue persists in the next pyzmq
    # release
    "pyzmq>=22.0.0,!=22.3.0,<27.0.0",
    # 'parsl' is a core requirement of the globus-compute-endpoint, essential to a range
    # of different features and functions
    # pin exact versions because it does not use semver
    "parsl==2024.3.18",
    "pika>=1.2.0",
    "pyprctl<0.2.0",
    "setproctitle>=1.3.2,<1.4",
    "pyyaml>=6.0,<7.0",
    "jinja2>=3.1.3,<3.2",
    "jsonschema>=4.19.0,<4.20",
    "cachetools>=5.3.1",
    "types-cachetools>=5.3.0.6",
]

TEST_REQUIRES = [
    "responses",
    "pytest>=7.2",
    "coverage>=5.2",
    "pytest-mock==3.2.0",
    "pyfakefs",
]


version_ns = {}
with open(os.path.join("globus_compute_endpoint", "version.py")) as f:
    exec(f.read(), version_ns)
version = version_ns["__version__"]

directory = Path(__file__).parent
long_description = (directory / "PyPI.md").read_text()

setup(
    name="globus-compute-endpoint",
    version=version,
    packages=find_packages(),
    description="Globus Compute: High Performance Function Serving for Science",
    long_description=long_description,
    long_description_content_type="text/markdown",
    install_requires=REQUIRES,
    extras_require={
        "test": TEST_REQUIRES,
    },
    python_requires=">=3.7",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: Apache Software License",
        "Natural Language :: English",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Topic :: Scientific/Engineering",
    ],
    keywords=["Globus Compute", "FaaS", "Function Serving"],
    entry_points={
        "console_scripts": [
            "globus-compute-endpoint=globus_compute_endpoint.cli:cli_run",
            "globus-compute-interchange"
            "=globus_compute_endpoint.engines.high_throughput.interchange:cli_run",
            "globus-compute-manager"
            "=globus_compute_endpoint.engines.high_throughput.manager:cli_run",
            "globus-compute-worker"
            "=globus_compute_endpoint.engines.high_throughput.worker:cli_run",
            # "globus-compute-diagnostic"
            # "=globus_compute_sdk.sdk.diagnostic:run_diagnostic",
        ]
    },
    include_package_data=True,
    author="Globus Compute Team",
    author_email="support@globus.org",
    license="Apache License, Version 2.0",
    url="https://github.com/globus/globus-compute",
    project_urls={
        "Changelog": "https://globus-compute.readthedocs.io/en/latest/changelog.html",  # noqa: E501
        "Upgrade to Globus Compute": "https://globus-compute.readthedocs.io/en/latest/funcx_upgrade.html",  # noqa: E501
    },
)
