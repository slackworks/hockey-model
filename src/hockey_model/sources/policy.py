"""Per-host request spacing and retry policy for the external sources."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HostPolicy:
    """Request spacing and retry count for one host."""

    min_interval_s: float
    max_retries: int
    # A response larger than this is refused rather than read into memory.
    max_body_bytes: int = 64 * 1024 * 1024
    # The whole response must arrive within this time, however slowly it trickles.
    total_timeout_s: float = 120.0


HOST_POLICIES: Mapping[str, HostPolicy] = {
    "api-web.nhle.com": HostPolicy(min_interval_s=0.5, max_retries=3),
    # The shift charts (#35). The host answers at the same pace as the web API.
    "api.nhle.com": HostPolicy(min_interval_s=0.5, max_retries=3),
    "site.api.espn.com": HostPolicy(min_interval_s=2.0, max_retries=3),
    "www.dailyfaceoff.com": HostPolicy(min_interval_s=5.0, max_retries=3),
    # all_teams.csv is about 130 MB. On a link of 84 KB/s it takes about 26 minutes, so the
    # deadline is one hour.
    "moneypuck.com": HostPolicy(
        min_interval_s=5.0,
        max_retries=3,
        max_body_bytes=512 * 1024 * 1024,
        total_timeout_s=3600.0,
    ),
    "peter-tanner.com": HostPolicy(min_interval_s=5.0, max_retries=3),
}
