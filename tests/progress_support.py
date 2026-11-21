"""The one grammar of a progress line, for the tests of the Python and of the shell writers."""

import re

LINE = re.compile(
    r"progress (\S+) (\S+) (start|tick|stop|end) elapsed=([0-9.]+)s"
    r"( done=[0-9]+(/[0-9]+)?)?( budget_left=[0-9]+s)?( reason=\S.*)?"
)
