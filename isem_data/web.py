"""Web requests that try again when the server is slow or briefly unavailable.

SEMO's and EirGrid's sites time out now and then. One timeout used to stop the whole
daily download, so every request now gets a few more tries, with a longer pause
before each one.
"""

import time

import requests

# Answers worth another try: too many requests, and the server-side errors.
RETRY_STATUS = {429, 500, 502, 503, 504}

# Seconds to wait before the second, third and fourth tries.
WAITS = [5, 20, 60]


def get(url: str, params: dict | None = None, timeout: int = 60, **kwargs) -> requests.Response:
    """Like requests.get, but tries up to four times on timeouts, dropped connections and
    server errors. Raises the last error if every try fails; other errors (such as 404)
    are raised at once."""
    for wait in [*WAITS, None]:
        try:
            response = requests.get(url, params=params, timeout=timeout, **kwargs)
            if response.status_code not in RETRY_STATUS or wait is None:
                response.raise_for_status()
                return response
            problem = f"HTTP {response.status_code}"
        except (requests.ConnectionError, requests.Timeout) as error:
            if wait is None:
                raise
            problem = type(error).__name__
        print(f"note: {url} failed ({problem}); trying again in {wait}s")
        time.sleep(wait)
    raise AssertionError("unreachable")  # the last pass either returns or raises
