# ai_core/cache
# Intelligent Cache Layer for LogPulse.
#
# Public surface:
#   configure_cache(backend)  — swap the active backend at startup
#   CacheBackend              — abstract base for custom backends
#   InMemoryCacheBackend      — default (dev / test)
#   SqliteCacheBackend        — persistent (production)

from cache_manager import CacheBackend, InMemoryCacheBackend, SqliteCacheBackend
from cache_node import configure_cache

__all__ = [
    "CacheBackend",
    "InMemoryCacheBackend",
    "SqliteCacheBackend",
    "configure_cache",
]
