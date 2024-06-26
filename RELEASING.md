# Releasing

Releases of `globus-compute-sdk` and `globus-compute-endpoint` are always done with a single version
number, even when only one package has changes.

The process is partially automated with tools to help along the way.

## Prerequisites

You must have the following tools installed and available:

- `git`
- `scriv`
- `tox`

You will also need the following credentials:

- a configured GPG key in `git` in order to create signed tags
- pypi credentials for use with `twine` (e.g. a token in `~/.pypirc`) valid for
    publishing `globus-compute-sdk` and `globus-compute-endpoint`

## Procedure

1. Bump versions of both packages to a new latest version number.  This is
   the next higher alpha version number like 2.16.0a1 if 2.16.0a0 already
   exists, or <X>.<Y+1>.<Z>a0 if this will be the first alpha release for an
   upcoming production release when the existing PyPI version is <X>.<Y>.<Z>

```bash
$EDITOR compute_sdk/globus_compute_sdk/version.py compute_endpoint/setup.py compute_endpoint/globus_compute_endpoint/version.py
```

2. Update the changelog by running `scriv collect --edit`

3. Add and commit the changes to version numbers and changelog files (including
   the removal of `changelog.d/` files), e.g. as follows

```bash
git add changelog.d/ docs/changelog.rst
git add compute_sdk/globus_compute_sdk/version.py compute_endpoint/setup.py compute_endpoint/globus_compute_endpoint/version.py
git commit -m 'Bump versions and changelog for release'
git push
```

4. Run the release script `./release.sh` from the repo root. This will use
   `tox` and your pypi credentials and will create a signed release tag. At the
   end of this step, new packages will be published to pypi and the tag to GitHub.


### DEB/RPM Packaging Workflow

#### Pre-requisites

Before building the packages, ensure that the release itself, either the alpha
or prod versions, is published on PyPI.

#### Build Process

In the future, building the DEB/RPM packages will be a simple one-step button
click of the green **Build** button on the Globus Compute Agent
[build page here](https://builds.globus.org/jenkins/job/BuildGlobusComputeAgentPackages/build?delay=0sec).

As a temporary workaround, we need to add a few lines to manually set some
env variables in our [JenkinsFile](https://github.com/globus/globus-compute/blob/743fa1e398fd40a00efb5880c55e3fa6e47392fc/compute_endpoint/packaging/JenkinsFile#L24) before triggering the build, as detailed below.

1. Git checkout both the current release branch that was recently pushed to
   PyPI, ie. ``v2.23.0`` or ``v2.25.0a0`` and the ``build_for_stable`` branch
2. Rebase ``build_for_stable`` on the release branch which should result in
   adding the following ~6 lines:

        ...
              env.BRANCH_NAME = scmVars.GIT_BRANCH.replaceFirst(/^.*origin\//, "")
        +     env.TAG_NAME = sh(returnStdout: true, script: "git tag --contains | head -1").trim()
              env.SOURCE_STASH_NAME = "${UUID.randomUUID()}"
              echo "env.BRANCH_NAME = ${env.BRANCH_NAME}"
              sh "git clean -fdx"

        +     // temporary hack to build for stable
        +     sh "git checkout build_for_stable"
        +     env.TAG_NAME = "v2.23.0"
        +     env.DEFAULT_BRANCH = "build_for_stable"
        +
              dir("compute_endpoint/packaging/") {
        ...

3. Change the ``env.TAG_NAME`` above to the current production release version
    * Note that ``env.TAG_NAME`` determines whether the build is sent to
      the ``unstable`` repo or also to the ``testing`` and ``stable`` ones.
    * Example of unstable repo:
        * https://downloads.globus.org/globus-connect-server/unstable/rpm/el/9/x86_64/
    * Example of stable repo:
        * https://downloads.globus.org/globus-connect-server/stable/rpm/el/9/x86_64/
    * The logic of whether a release is stable is determined by whether the
      package version of Globus Compute Endpoint set in ``version.py`` or
      ``setup.py`` matches ``env.TAG_NAME`` above.   If they are unequal, then
      [publishResults.groovy line 85](https://github.com/globusonline/gcs-build-scripts/blob/168617a0ccbb0aee7b3bee04ee67940bbe2a80f6/vars/publishResults.groovy#L85)
      will be (``tag`` : v2.23.0) != (``stable_tag`` : v2.23.0a0), where
      stable_tag is constructed from the package version of an alpha release.
4. Commit and push your ``build_for_stable`` branch
5. Click the [build button here](https://builds.globus.org/jenkins/job/BuildGlobusComputeAgentPackages/build?delay=0sec)
6. Wait 20-30 minutes and confirm that the [build is green](https://builds.globus.org/jenkins/job/BuildGlobusComputeAgentPackages/)
7. For production release, we will have finished the build before the GCS
   team, and can notify them that our build is complete.  They then will
   publish all packages when they finish their builds, which includes ours.
