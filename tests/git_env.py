"""Remove the git variables that point a command at one repository from the test environment.

A git hook sets ``GIT_DIR`` and ``GIT_INDEX_FILE``. A test that makes its own repository in a
temporary folder would then read and write the index of the repository that the hook runs in.
"""

import os

# Each variable names where git finds a repository, its index, or its objects.
REPO_VARS = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
)
# These carry configuration, so a test keeps them: they do not choose a repository.
CONFIG_VARS = ("GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS")


def clear_repo_env() -> None:
    """Delete each repository variable from this process, so a child process cannot inherit it."""
    for name in REPO_VARS:
        os.environ.pop(name, None)
