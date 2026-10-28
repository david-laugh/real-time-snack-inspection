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


    def test_camera_timestamps_join_in_both_orders(self):
        for camera_first in (True, False):
            m = PipelineMetrics()
            camera = SimpleNamespace(session_id='s', trace_id='t1', frame_id=1,
                gateway_received_at_ms=1010, preprocess_started_at_ms=1020,
                preprocess_finished_at_ms=1030)
            if camera_first:
                m.record_camera(camera)
                m.record(result(), 1120)
            else:
                m.record(result(), 1120)
                m.record_camera(camera)
            latency = m.get_stats()['latency']
            for name in ('c1_c2', 'preprocess', 'c2_c3'):
                self.assertEqual(latency[name]['p50_ms'], 10)
            m.record_camera(camera)
            self.assertEqual(m.get_stats()['latency']['c1_c2']['samples'], 1)

    def test_camera_timing_never_joins_other_session(self):
        m = PipelineMetrics()
        m.record(result(), 1120)
        m.record_camera(SimpleNamespace(session_id='other', trace_id='t1', frame_id=1,
            gateway_received_at_ms=1010, preprocess_started_at_ms=1020,
            preprocess_finished_at_ms=1030))
        self.assertIsNone(m.get_stats()['latency']['c1_c2']['p50_ms'])

    def test_breakdown_precedes_camera_fallback_including_null(self):
        m = PipelineMetrics()
        m.record(result(latency_breakdown_ms={'preprocess': 7, 'c2_to_c3': None,
                                             'c3_service': 20, 'c3_queue': None}), 1120)
        m.record_camera(SimpleNamespace(session_id='s', trace_id='t1', frame_id=1,
            gateway_received_at_ms=1010, preprocess_started_at_ms=1020,
            preprocess_finished_at_ms=1030))
        latency = m.get_stats()['latency']
        self.assertEqual(latency['preprocess']['p50_ms'], 7)
        self.assertIsNone(latency['c2_c3']['p50_ms'])
        self.assertEqual(latency['c3_service']['p50_ms'], 20)

    def test_drop_window_deltas_and_restart(self):
        m = PipelineMetrics()
        def update(received, forwarded, errors, results):
            m.update_c1({'counters': {'received':received, 'dropped':0}})
            m.update_c2({'events': {'dropped_stale':0,'dropped_old':0,
                                    'forwarded':forwarded,'error':errors,'timeout':0}})
            m.received_frames = results
        with patch('src.metrics.time.monotonic', return_value=0):
            update(100,80,10,80)
            m.sample_drop_window()
        with patch('src.metrics.time.monotonic', return_value=6):
            update(120,98,12,97)
            m.sample_drop_window()
        window = m.get_stats()['drop_window']
        self.assertEqual(window['c2_error_pct'], 10)
        self.assertEqual(window['result_gap_estimate'], 1)
        self.assertEqual(window['overall_loss_pct_estimate'], 15)
        with patch('src.metrics.time.monotonic', return_value=12):
            update(0,0,0,97)
            m.sample_drop_window()
        self.assertIsNone(m.get_stats()['drop_window'])

    def test_skipped_empty_frames_count_as_c2_throughput_not_loss(self):
        m = PipelineMetrics()
        def update(received, forwarded, skipped_empty, results):
            m.update_c1({'counters': {'received': received, 'dropped': 0}})
            m.update_c2({'events': {'dropped_stale': 0, 'dropped_old': 0, 'forwarded': forwarded,
                                    'skipped_empty': skipped_empty, 'error': 0, 'timeout': 0}})
            m.received_frames = results
        with patch('src.metrics.time.monotonic', return_value=0):
            update(0, 0, 0, 0)
            m.sample_drop_window()
        with patch('src.metrics.time.monotonic', return_value=5):
            update(100, 60, 40, 60)
            m.sample_drop_window()
        window = m.get_stats()['drop_window']
        self.assertEqual(window['overall_loss_pct_estimate'], 0)
        self.assertEqual(window['c2_processed_per_s'], 20)
        self.assertEqual(window['result_gap_estimate'], 0)
        self.assertEqual(m.get_stats()['c2_metrics']['skipped_empty'], 40)
