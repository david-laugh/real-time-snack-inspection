"""Frame-level metrics, independent of detection/object counts."""
import math
import time
from collections import OrderedDict, deque


class PipelineMetrics:
    def __init__(self):
        self.frames = OrderedDict()
        self.result_samples = OrderedDict()
        self.last_camera_at_ms = None
        self.last_result_at_ms = None
        self.samples = deque(maxlen=200)
        self.received = deque()
        self.seen = OrderedDict()
        self.received_frames = 0
        self.c1 = None
        self.c1_available = False
        self.c1_updated_at_ms = None
        self.drop_baseline = None
        self.drop_window = None
        self.c2 = None
        self.c2_updated_at_ms = None
        self.c2_available = False

    @staticmethod
    def elapsed(end, start):
        if start is None or end is None:
            return None
        value = end - start
        return value if start > 0 and math.isfinite(value) and value >= 0 else None

    def record_camera(self, frame):
        self.last_camera_at_ms = int(time.time() * 1000)
        key = (frame.session_id, frame.trace_id, frame.frame_id)
        self.frames[key] = (frame.gateway_received_at_ms, frame.preprocess_started_at_ms,
                            frame.preprocess_finished_at_ms)
        self.frames.move_to_end(key)
        if len(self.frames) > 500:
            self.frames.popitem(last=False)
        self.join_timing(key)

    def join_timing(self, key):
        if key not in self.frames or key not in self.result_samples:
            return
        gateway, started, finished = self.frames[key]
        sample, inference_started = self.result_samples[key]
        sample['c1_c2'] = self.elapsed(started, gateway)
        sample['upload'] = self.elapsed(gateway, sample['_captured_at_ms'])
        if 'preprocess' not in sample['_breakdown']:
            sample['preprocess'] = self.elapsed(finished, started)
        if 'c2_to_c3' not in sample['_breakdown']:
            sample['c2_c3'] = self.elapsed(inference_started, finished)

    def record(self, result, received_at_ms=None):
        now = time.time() * 1000 if received_at_ms is None else received_at_ms
        self.last_result_at_ms = int(now)
        key = (result.session_id, result.trace_id, result.frame_id)
        if key in self.seen:
            return
        self.received_frames += 1
        self.seen[key] = None
        if len(self.seen) > 5000:
            self.seen.popitem(last=False)
        self.received.append(time.monotonic())
        elapsed = self.elapsed
        self.samples.append({
            'e2e': elapsed(now, result.captured_at_ms),
            'before_c3': elapsed(result.inference_started_at_ms, result.captured_at_ms),
            'inference': result.inference_latency_ms if math.isfinite(result.inference_latency_ms) and result.inference_latency_ms >= 0 else None,
            'c3_c4': elapsed(now, result.inference_finished_at_ms),
            'c1_c2': None, 'preprocess': None, 'c2_c3': None, 'upload': None,
            '_captured_at_ms': result.captured_at_ms,
            '_breakdown': getattr(result, 'latency_breakdown_ms', None) or {},
        })

        sample = self.samples[-1]
        for name in ('capture_to_preprocess', 'preprocess', 'c2_to_c3', 'c3_queue',
                     'c3_service', 'c3_total', 'capture_to_c3_done'):
            value = sample['_breakdown'].get(name)
            sample[name] = value if value is not None and math.isfinite(value) and value >= 0 else None
        if 'c2_to_c3' in sample['_breakdown']:
            sample['c2_c3'] = sample['c2_to_c3']
        self.result_samples[key] = (self.samples[-1], result.inference_started_at_ms)
        if len(self.result_samples) > 200:
            self.result_samples.popitem(last=False)
        self.join_timing(key)

    def update_c2(self, data):
        events = data['events']
        names = ('dropped_stale', 'dropped_old')
        values = {name: events[name] for name in names}
        if any(type(v) is not int or v < 0 for v in values.values()):
            raise ValueError('Invalid C2 counters')
        for name in ('c4_skipped', 'c4_error', 'forwarded', 'error', 'timeout'):
            source = events if name in ('forwarded', 'error', 'timeout') else data
            value = source.get(name)
            values[name] = value if type(value) is int and value >= 0 else None
        self.c2 = values
        self.c2_available = True
        self.c2_updated_at_ms = int(time.time() * 1000)

    def update_c1(self, data):
        counters = data['counters']
        values = {name: counters[name] for name in ('received', 'dropped')}
        if any(type(v) is not int or v < 0 for v in values.values()):
            raise ValueError('Invalid C1 counters')
        self.c1 = values
        self.c1_available = True
        self.c1_updated_at_ms = int(time.time() * 1000)

    def sample_drop_window(self):
        # Called only after polling upstream counters, not per frame or API read.
        if not self.c1_available or not self.c2_available or any(
                self.c2.get(k) is None for k in ('forwarded', 'error', 'timeout')):
            self.drop_baseline = self.drop_window = None
            return
        now = time.monotonic()
        current = dict(received=self.c1['received'], c1_drop=self.c1['dropped'],
            c2_drop=self.c2['dropped_stale'] + self.c2['dropped_old'],
            forwarded=self.c2['forwarded'], errors=self.c2['error'] + self.c2['timeout'],
            results=self.received_frames)
        if self.drop_baseline is None:
            self.drop_baseline = (now, current)
            return
        before, previous = self.drop_baseline
        delta = {k: v - previous[k] for k, v in current.items()}
        if any(v < 0 for v in delta.values()):
            self.drop_baseline, self.drop_window = (now, current), None
            return
        if now - before < 5:
            return
        def rate(n, d):
            return round(min(1, max(0, n / d)) * 100, 2) if d > 0 else None
        self.drop_window = {
            'seconds': round(now - before, 2), 'estimated': True,
            'c1_drop_pct': rate(delta['c1_drop'], delta['received']),
            'c2_drop_pct': rate(delta['c2_drop'], delta['received']),
            'c2_error_pct': rate(delta['errors'], delta['forwarded'] + delta['errors']),
            'result_gap_estimate': max(0, delta['forwarded'] - delta['results']),
            'overall_loss_pct_estimate': rate(delta['received'] - delta['results'], delta['received']),
            'deltas': delta,
        }
        self.drop_baseline = (now, current)

    def get_stats(self):
        now = time.monotonic()
        while self.received and self.received[0] <= now - 5:
            self.received.popleft()
        latency = {}
        for name in ('e2e', 'before_c3', 'inference', 'c3_c4', 'c1_c2', 'preprocess', 'c2_c3', 'upload', 'capture_to_preprocess', 'c2_to_c3', 'c3_queue', 'c3_service', 'c3_total', 'capture_to_c3_done'):
            values = sorted(s[name] for s in self.samples if s.get(name) is not None)
            latency[name] = {
                'p50_ms': round(values[math.ceil(len(values) * .50) - 1], 1) if values else None,
                'p95_ms': round(values[math.ceil(len(values) * .95) - 1], 1) if values else None,
                'samples': len(values),
            }
        return {'received_frames': self.received_frames, 'drop_window': self.drop_window,
                'c1_metrics': self.c1 if self.c1_available else None,
                'fps': round(len(self.received) / 5, 1), 'latency': latency,
                'c2_metrics': self.c2 if self.c2_available else None,
                'c2_updated_at_ms': self.c2_updated_at_ms}
