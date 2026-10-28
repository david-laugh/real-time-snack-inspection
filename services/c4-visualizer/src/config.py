"""C4 environment settings. Compose injects .env; local Python uses its environment."""
import math
import os
from urllib.parse import urlsplit


def positive_seconds(name, default):
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be a finite positive number')
    return value


C2_STATS_URL = os.getenv('C4_C2_STATS_URL', 'http://c2-preprocessing:8001/stats').strip()
if C2_STATS_URL and (urlsplit(C2_STATS_URL).scheme not in ('http', 'https') or not urlsplit(C2_STATS_URL).hostname):
    raise ValueError('C4_C2_STATS_URL must be an HTTP(S) URL or empty to disable polling')
C2_STATS_TIMEOUT_S = positive_seconds('C4_C2_STATS_TIMEOUT_S', 1)
C2_STATS_INTERVAL_S = positive_seconds('C4_C2_STATS_INTERVAL_S', 2)
