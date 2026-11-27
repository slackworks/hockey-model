"""Remove the git variables from the test environment.

A git hook sets ``GIT_DIR``, ``GIT_INDEX_FILE``, and others. A test that makes its own repository
in a temporary folder would then read and write the index and the config of the repository that
the hook runs in. The list of variables that redirect git is long and it grows, so the whole
``GIT_`` prefix goes.
"""

import os

PREFIX = "GIT_"


def clear_git_env() -> None:
    """Delete each ``GIT_`` variable from this process, so a child process cannot inherit it."""
    for name in [name for name in os.environ if name.startswith(PREFIX)]:
        del os.environ[name]
