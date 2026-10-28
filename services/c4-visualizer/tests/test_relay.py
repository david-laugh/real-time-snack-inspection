import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

from src import main
from src.models import CameraFrame, DetectionResult, ScanLine
from src.store import InspectionStore
from src.tracker import CookieTracker
from src.metrics import PipelineMetrics


def camera_body(**changes):
    body = dict(trace_id='t', session_id='s', frame_id=1, gateway_received_at_ms=100,
                preprocess_started_at_ms=110, preprocess_finished_at_ms=120, width=3, height=2,
                prompts=[dict(candidate_id='c0', type='box', bbox=[0, 0, 2, 1.5])],
                image_b64='jpeg')
    body.update(changes)
    return body


class CameraFrameHttpTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(main, 'broadcast', new_callable=AsyncMock)
        self.send = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)

    def test_valid_frame_is_accepted_with_202_and_relayed(self):
        response = self.client.post('/api/camera/frame', json=camera_body())
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.send.call_args.args[0]['type'], 'camera_frame')

    def test_csv_exports_and_scan_line_endpoint(self):
        cookies = self.client.get('/api/export/csv')
        self.assertTrue(cookies.text.startswith('cookie_id,'))
        self.assertIn('inspection-cookies.csv', cookies.headers['content-disposition'])
        self.assertTrue(self.client.get('/api/export/detections.csv').text.startswith('item_id,'))
        self.assertEqual(self.client.post('/api/scan-line', json={'ratio': 1.5}).status_code, 422)
        self.assertEqual(self.client.get('/api/scan-line').json()['direction'], 'left')

    def test_empty_prompts_are_valid(self):
        self.assertEqual(self.client.post('/api/camera/frame', json=camera_body(prompts=[])).status_code, 202)

    def test_schema_violations_are_rejected_with_422(self):
        legacy = camera_body()
        for name in ('gateway_received_at_ms', 'preprocess_started_at_ms', 'preprocess_finished_at_ms'):
            legacy.pop(name)
        bad_bodies = {
            'legacy seven fields': legacy,
            'extra field': camera_body(captured_at_ms=1),
            'string integer': camera_body(frame_id='1'),
            'negative frame_id': camera_body(frame_id=-1),
            'zero width': camera_body(width=0),
            'empty image': camera_body(image_b64=''),
            'prompt type': camera_body(prompts=[dict(candidate_id='c0', type='point', bbox=[0, 0, 1, 1])]),
            'prompt bbox length': camera_body(prompts=[dict(candidate_id='c0', type='box', bbox=[0, 0, 1])]),
            'prompt extra field': camera_body(prompts=[dict(candidate_id='c0', type='box', bbox=[0, 0, 1, 1], score=1)]),
        }
        for name, body in bad_bodies.items():
            with self.subTest(name):
                self.assertEqual(self.client.post('/api/camera/frame', json=body).status_code, 422)
        self.send.assert_not_called()


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for name, fresh in (('metrics', PipelineMetrics()), ('store', InspectionStore()),
                            ('tracker', CookieTracker(reorder_ms=0, vote_frames=1))):
            self.addCleanup(setattr, main, name, getattr(main, name))
            setattr(main, name, fresh)

    async def test_cookie_seen_in_many_frames_is_counted_once(self):
        def frame(frame_id, x):
            return DetectionResult(trace_id=f't{frame_id}', session_id='s', frame_id=frame_id,
                captured_at_ms=0, inference_container_id='c3', inference_started_at_ms=0,
                inference_finished_at_ms=1, inference_latency_ms=1,
                detections=[dict(object_id='c0', bbox=[x - 10, 40, x + 10, 60], status='defect',
                                 defect_type='broken', defect_score=0.9)])
        await main.receive_camera_frame(CameraFrame(**camera_body(width=100, height=100)), BackgroundTasks())
        with patch.object(main, 'broadcast', new_callable=AsyncMock) as send:
            for frame_id, x in enumerate([60, 50, 40, 30, 20]):
                await main.receive_result(frame(frame_id, x))
        cookies = [c.args[0]['data'] for c in send.call_args_list if c.args[0]['type'] == 'cookie_counted']
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]['status'], 'defect')
        stats = main.dashboard_stats()
        self.assertEqual((stats['total'], stats['fail_count'], stats['detections']['total']), (1, 1, 5))
        cookie_csv = main.store.export_cookies_csv().strip().splitlines()
        self.assertEqual(len(cookie_csv), 2)
        self.assertTrue(cookie_csv[0].startswith('cookie_id,'))
        self.assertEqual(len(main.store.export_detections_csv().strip().splitlines()), 6)

    async def test_scan_line_api_validates_and_broadcasts(self):
        with patch.object(main, 'broadcast', new_callable=AsyncMock) as send:
            reply = await main.set_scan_line(ScanLine(ratio=0.5))
        self.assertEqual(reply['scan_line_ratio'], 0.5)
        self.assertEqual(main.tracker.scan_line_ratio, 0.5)
        self.assertEqual(send.call_args.args[0]['type'], 'scan_line')
        for ratio in (0, 1, -0.1):
            with self.assertRaises(ValueError):
                ScanLine(ratio=ratio)

    async def test_poll_c2_failure_hides_stale_counters(self):
        main.metrics.update_c2({'events': {'dropped_stale': 1, 'dropped_old': 2}})
        with patch.object(main, 'C2_STATS_URL', 'http://c2/stats'), \
             patch.object(main.asyncio, 'to_thread', new_callable=AsyncMock, side_effect=OSError), \
             patch.object(main.asyncio, 'sleep', new_callable=AsyncMock, side_effect=asyncio.CancelledError), \
             patch.object(main, 'broadcast', new_callable=AsyncMock) as send:
            with self.assertRaises(asyncio.CancelledError):
                await main.poll_metrics()
        self.assertIsNone(send.call_args.args[0]['data']['c2_metrics'])

    async def test_main_ten_field_camera_contract(self):
        background = BackgroundTasks()
        with patch.object(main, 'broadcast', new_callable=AsyncMock) as send:
            reply = await main.receive_camera_frame(CameraFrame(**camera_body()), background)
            send.assert_not_called()  # relayed after the 202 is sent
            await background()
        self.assertEqual(reply, {'status': 'accepted'})
        data = send.call_args.args[0]['data']
        expected = camera_body()
        expected['frame'] = expected.pop('image_b64')
        self.assertEqual(data, expected)
        connections = await main.connections()
        self.assertIsNotNone(connections['camera']['last_received_at_ms'])
        self.assertIsNone(connections['results']['last_received_at_ms'])
        self.assertFalse(connections['mqtt_required'])

    async def test_c3_status_is_authoritative_in_feed_store_and_csv(self):
        from src.store import InspectionStore
        old_store = main.store
        main.store = InspectionStore()
        self.addCleanup(setattr, main, 'store', old_store)
        result = DetectionResult(trace_id='classification', session_id='s', frame_id=1,
            captured_at_ms=1000, inference_container_id='c3',
            inference_started_at_ms=1010, inference_finished_at_ms=1020,
            inference_latency_ms=10, latency_breakdown_ms={'c3_queue': None, 'c3_service': 10},
            detections=[dict(object_id=status, bbox=[0,0,10,10], status=status,
                             defect_type='broken' if status == 'defect' else None)
                        for status in ('normal', 'defect', 'unknown')])
        with patch.object(main, 'broadcast', new_callable=AsyncMock) as send:
            await main.receive_result(result)
        stats = main.store.get_stats()['detections']
        self.assertEqual((stats['pass_count'], stats['fail_count'], stats['unknown_count']), (1,1,1))
        rows = main.store.get_recent()
        self.assertEqual({r['status'] for r in rows}, {'normal','defect','unknown'})
        self.assertTrue(all(r['circularity'] is None for r in rows))
        self.assertEqual(main.store.get_defects()[0]['defect_type'], 'broken')
        self.assertIn('broken', main.store.export_detections_csv())
        event = next(c.args[0] for c in send.call_args_list if c.args[0]['type']=='frame_result')
        self.assertEqual(event['data']['latency_breakdown_ms']['c3_service'], 10)

    async def test_result_preserves_mask_and_emits_empty_frames(self):
        mask = dict(encoding='rle', width=3, height=2,
                    data=dict(order='row-major', counts=[0, 2, 4]))
        for detections in [[], [dict(object_id='c0', bbox=[0, 0, 2, 1],
                                     status='normal', mask=mask)]]:
            result = DetectionResult(trace_id='t', session_id='s', frame_id=1,
                captured_at_ms=0, inference_container_id='c3',
                inference_started_at_ms=0, inference_finished_at_ms=1,
                inference_latency_ms=1, detections=detections)
            with patch.object(main, 'broadcast', new_callable=AsyncMock) as send:
                await main.receive_result(result)
            event = next(c.args[0] for c in send.call_args_list
                         if c.args[0]['type'] == 'frame_result')
            self.assertEqual(event['data']['trace_id'], 't')
            self.assertEqual(event['data']['session_id'], 's')
            self.assertEqual(len(event['data']['detections']), len(detections))
            if detections:
                self.assertEqual(event['data']['detections'][0]['mask'], mask)


if __name__ == '__main__':
    unittest.main()
