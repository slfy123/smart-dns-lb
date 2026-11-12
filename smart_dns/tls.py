"""Share immutable TLS verification settings between short-lived HTTP probes."""

from functools import lru_cache
from ssl import SSLContext

import httpx


@lru_cache(maxsize=1)
def verified_context() -> SSLContext:
    # Loading the CA bundle for every probe repeatedly allocates native OpenSSL
    # memory. Keep fresh probe connections, but load their trust store only once.
    # Callers must not mutate the shared context.
    return httpx.create_ssl_context(verify=True, trust_env=False)
