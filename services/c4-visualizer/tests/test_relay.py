import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from src import main
from src.models import CameraFrame, DetectionResult
from src.metrics import PipelineMetrics


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        previous = main.metrics
        main.metrics = PipelineMetrics()
        self.addCleanup(setattr, main, 'metrics', previous)

    async def test_poll_c2_failure_hides_stale_counters(self):
        main.metrics.update_c2({'events': {'dropped_stale': 1, 'dropped_old': 2}})
        with patch.object(main, 'C2_STATS_URL', 'http://c2/stats'), \
             patch.object(main.asyncio, 'to_thread', new_callable=AsyncMock, side_effect=OSError), \
             patch.object(main.asyncio, 'sleep', new_callable=AsyncMock, side_effect=asyncio.CancelledError), \
             patch.object(main, 'broadcast', new_callable=AsyncMock) as send:
            with self.assertRaises(asyncio.CancelledError):
                await main.poll_metrics()
        self.assertIsNone(send.call_args.args[0]['data']['c2_metrics'])

    async def test_camera_preserves_coordinates_and_identity(self):
        frame = CameraFrame(trace_id='t', session_id='s', frame_id=1,
                            width=3, height=2, prompts=[], image_b64='jpeg')
        with patch.object(main, 'broadcast', new_callable=AsyncMock) as send:
            await main.receive_camera_frame(frame)
        data = send.call_args.args[0]['data']
        self.assertEqual(data, dict(trace_id='t', session_id='s', frame_id=1,
                                   width=3, height=2, prompts=[], frame='jpeg'))

    async def test_main_ten_field_camera_contract(self):
        frame = CameraFrame(trace_id='t', session_id='s', frame_id=1,
            width=3, height=2, prompts=[], image_b64='jpeg',
            gateway_received_at_ms=100, preprocess_started_at_ms=110,
            preprocess_finished_at_ms=120)
        with patch.object(main, 'broadcast', new_callable=AsyncMock) as send:
            await main.receive_camera_frame(frame)
        data = send.call_args.args[0]['data']
        self.assertEqual(data['gateway_received_at_ms'], 100)
        self.assertEqual(data['preprocess_started_at_ms'], 110)
        self.assertEqual(data['preprocess_finished_at_ms'], 120)
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
        stats = main.store.get_stats()
        self.assertEqual((stats['pass_count'], stats['fail_count'], stats['unknown_count']), (1,1,1))
        rows = main.store.get_recent()
        self.assertEqual({r['status'] for r in rows}, {'normal','defect','unknown'})
        self.assertTrue(all('circularity' not in r for r in rows))  # C4 never re-judges
        self.assertEqual(rows[0]['bbox'], [0, 0, 10, 10])
        self.assertNotIn('circularity', main.store.export_csv().splitlines()[0])
        self.assertEqual(main.store.get_defects()[0]['defect_type'], 'broken')
        self.assertIn('broken', main.store.export_csv())
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
