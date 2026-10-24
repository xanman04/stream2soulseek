"""slskd (Soulseek daemon) HTTP client and search orchestration."""

from .client import SlskdAuthError, SlskdClient, SlskdError, map_transfer_state
from .search import SearchCancelled, build_queries, run_search

__all__ = [
    "SlskdAuthError",
    "SlskdClient",
    "SlskdError",
    "map_transfer_state",
    "SearchCancelled",
    "build_queries",
    "run_search",
]
