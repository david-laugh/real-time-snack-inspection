"""Frame-level metrics, independent of detection/object counts."""
import math
import time
from collections import OrderedDict, deque


class PipelineMetrics:
    def __init__(self):
        self.samples = deque(maxlen=200)
        self.received = deque()
        self.seen = OrderedDict()
        self.c2 = None
        self.c2_updated_at_ms = None
        self.c2_available = False

    def record(self, result, received_at_ms=None):
        now = time.time() * 1000 if received_at_ms is None else received_at_ms
        key = (result.session_id, result.trace_id, result.frame_id)
        if key in self.seen:
            return
        self.seen[key] = None
        if len(self.seen) > 5000:
            self.seen.popitem(last=False)
        self.received.append(time.monotonic())
        def elapsed(end, start):
            value = end - start
            return value if start > 0 and math.isfinite(value) and value >= 0 else None
        self.samples.append({
            'e2e': elapsed(now, result.captured_at_ms),
            'before_c3': elapsed(result.inference_started_at_ms, result.captured_at_ms),
            'inference': result.inference_latency_ms if math.isfinite(result.inference_latency_ms) and result.inference_latency_ms >= 0 else None,
            'c3_c4': elapsed(now, result.inference_finished_at_ms),
        })

    def update_c2(self, data):
        events = data['events']
        names = ('dropped_stale', 'dropped_old')
        values = {name: events[name] for name in names}
        if any(type(v) is not int or v < 0 for v in values.values()):
            raise ValueError('Invalid C2 counters')
        for name in ('c4_skipped', 'c4_error'):
            value = data.get(name)
            values[name] = value if type(value) is int and value >= 0 else None
        self.c2 = values
        self.c2_available = True
        self.c2_updated_at_ms = int(time.time() * 1000)

    def get_stats(self):
        now = time.monotonic()
        while self.received and self.received[0] <= now - 5:
            self.received.popleft()
        latency = {}
        for name in ('e2e', 'before_c3', 'inference', 'c3_c4'):
            values = sorted(s[name] for s in self.samples if s[name] is not None)
            latency[name] = {
                'p50_ms': round(values[math.ceil(len(values) * .50) - 1], 1) if values else None,
                'p95_ms': round(values[math.ceil(len(values) * .95) - 1], 1) if values else None,
                'samples': len(values),
            }
        return {'fps': round(len(self.received) / 5, 1), 'latency': latency,
                'c2_metrics': self.c2 if self.c2_available else None,
                'c2_updated_at_ms': self.c2_updated_at_ms}
