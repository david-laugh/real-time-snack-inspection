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
C2_STATS_INTERVAL_S = positive_seconds('C4_C2_STATS_INTERVAL_S', 1)

C1_METRICS_URL = os.getenv('C4_C1_METRICS_URL', 'http://c1-gateway:8000/api/metrics').strip()
if C1_METRICS_URL and (urlsplit(C1_METRICS_URL).scheme not in ('http', 'https') or not urlsplit(C1_METRICS_URL).hostname):
    raise ValueError('C4_C1_METRICS_URL must be an HTTP(S) URL or empty')

# Cookie tracking (src/tracker.py). The scan line can also be moved from the dashboard at runtime.
SCAN_LINE_RATIO = float(os.getenv('C4_SCAN_LINE_RATIO', '0.34'))
if not 0 < SCAN_LINE_RATIO < 1:
    raise ValueError('C4_SCAN_LINE_RATIO must be between 0 and 1 (fraction of image width)')
TRAVEL_DIRECTION = os.getenv('C4_TRAVEL_DIRECTION', 'left').strip().lower()
if TRAVEL_DIRECTION not in ('left', 'right'):
    raise ValueError('C4_TRAVEL_DIRECTION must be left or right')
TRACK_VOTE_FRAMES = int(os.getenv('C4_TRACK_VOTE_FRAMES', '2'))
if TRACK_VOTE_FRAMES < 0:
    raise ValueError('C4_TRACK_VOTE_FRAMES must be >= 0')
TRACK_REORDER_MS = positive_seconds('C4_TRACK_REORDER_S', 0.5) * 1000
