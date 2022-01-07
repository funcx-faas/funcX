# Contributing Guide

This doc covers dev setup and guidelines for contributing.

FIXME: This doc is a stub.

## Requirements

- python3.6+, pip, virtualenv

### Recommended

- [pre-commit](https://pre-commit.com/)

## Linting

Linting can be run via pre-commit. Run for all files in the repo:

    pre-commit run -a

### (Optional) Setup pre-commit Hooks

For the best development experience, set up linting and autofixing pre-commit
git hooks using the `pre-commit` tool.

After installing `pre-commit`, run

    pre-commit install

in the repo to configure hooks.

> NOTE: If necessary, you can always skip hooks with `git commit --no-verify`

## Installing Testing Requirements

Testing requirements for each of the two packages in this repository
(funcx-sdk and funcx-endpoint) are specified as installable extras.

To install the funcx-sdk test requirements

    cd funcx_sdk
    pip install '.[test]'

To install the funcx-endpoint test requirements

    cd funcx_endpoint
    pip install '.[test]'
