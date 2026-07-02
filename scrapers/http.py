"""
Shared HTTP session with retry, throttling, and User-Agent.
"""
from __future__ import annotations

import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import USER_AGENT, HTTP_TIMEOUT, HTTP_RETRY, HTTP_RETRY_BACKOFF


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    retries = Retry(
        total=HTTP_RETRY,
        backoff_factor=HTTP_RETRY_BACKOFF,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class Sleeper:
    """Simple per-request throttle."""

    def __init__(self, interval: float = 1.5):
        self.interval = interval
        self._last = 0.0

    def wait(self):
        elapsed = time.time() - self._last
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self._last = time.time()
