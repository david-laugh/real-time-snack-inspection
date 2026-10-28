import unittest
from types import SimpleNamespace
from unittest.mock import patch
from src.metrics import PipelineMetrics


def result(frame=1, session='s', **kwargs):
    data = dict(session_id=session, trace_id=f't{frame}', frame_id=frame,
                captured_at_ms=1000, inference_started_at_ms=1040,
                inference_finished_at_ms=1100, inference_latency_ms=60,
                detections=[])
    return SimpleNamespace(**{**data, **kwargs})


class MetricsTests(unittest.TestCase):
    def test_latency_and_frame_fps_include_zero_detections(self):
        m = PipelineMetrics()
        with patch('src.metrics.time.monotonic', return_value=10):
            m.record(result(), 1120)
            m.record(result(), 1120)  # duplicate delivery
            m.record(result(2, detections=[{}, {}]), 1140)
            stats = m.get_stats()
        self.assertEqual(stats['fps'], .4)
        self.assertEqual(stats['latency']['e2e']['p50_ms'], 120)
        self.assertEqual(stats['latency']['e2e']['p95_ms'], 140)
        self.assertEqual(stats['latency']['before_c3']['p50_ms'], 40)
        self.assertEqual(stats['latency']['c3_c4']['p50_ms'], 20)
        with patch('src.metrics.time.monotonic', return_value=16):
            self.assertEqual(m.get_stats()['fps'], 0)

    def test_reordered_frames_and_sessions_do_not_create_drops(self):
        m = PipelineMetrics()
        for frame, session in [(3, 'a'), (1, 'a'), (2, 'a'), (1, 'b')]:
            m.record(result(frame, session), 1120)
        self.assertEqual(len(m.samples), 4)
        self.assertIsNone(m.get_stats()['c2_metrics'])

    def test_clock_skew_and_missing_samples_are_unknown(self):
        m = PipelineMetrics()
        m.record(result(captured_at_ms=2000, inference_finished_at_ms=2000), 1120)
        stats = m.get_stats()['latency']
        self.assertIsNone(stats['e2e']['p50_ms'])
        self.assertIsNone(stats['c3_c4']['p95_ms'])
        self.assertEqual(stats['inference']['p50_ms'], 60)

    def test_c2_old_and_new_contract_and_disconnect(self):
        m = PipelineMetrics()
        m.update_c2({'events': {'dropped_stale': 3, 'dropped_old': 2}})
        self.assertEqual(m.get_stats()['c2_metrics']['dropped_stale'], 3)
        self.assertIsNone(m.get_stats()['c2_metrics']['c4_error'])
        m.update_c2({'events': {'dropped_stale': 3, 'dropped_old': 2},
                     'c4_error': 4, 'c4_skipped': 5})
        self.assertEqual(m.get_stats()['c2_metrics']['c4_error'], 4)
        m.c2_available = False
        self.assertIsNone(m.get_stats()['c2_metrics'])
        m.update_c2({'events': {'dropped_stale': 0, 'dropped_old': 0}})
        self.assertEqual(m.get_stats()['c2_metrics']['dropped_old'], 0)

    def test_sample_history_is_bounded(self):
        m = PipelineMetrics()
        for i in range(5500):
            m.record(result(i), 1120)
        self.assertEqual(len(m.samples), 200)
        self.assertEqual(len(m.seen), 5000)
