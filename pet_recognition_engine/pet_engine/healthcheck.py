"""The health check of the app's container: python -m pet_engine.healthcheck.

Docker runs it in the container and marks the app unhealthy after a few
failures, which the Supervisor's watchdog acts on. It asks /health, which needs
no key, on the port the engine serves (ENGINE_PORT, else PORT). It only uses
the standard library, so that it starts quickly and needs no model.
"""

from __future__ import annotations

import http.client
import os
import sys
import urllib.request
from collections.abc import Mapping

# The engine's default port, also used by the app's start-up.
PORT = 8765
# Seconds to wait for the answer: less than the HEALTHCHECK timeout.
TIMEOUT = 4


def healthy(environ: Mapping[str, str]) -> bool:
    """Whether the engine answers /health with 200."""
    url = f"http://127.0.0.1:{environ.get('ENGINE_PORT', PORT)}/health"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            return response.status == 200
    # No engine, an engine that answers with an error, or a port that is no number.
    except (OSError, http.client.HTTPException):
        return False


def main() -> int:
    """0 when the engine is healthy, else 1, as Docker expects."""
    return 0 if healthy(os.environ) else 1


if __name__ == "__main__":
    sys.exit(main())
