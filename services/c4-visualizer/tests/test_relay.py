import unittest
from unittest.mock import AsyncMock, patch
from src import main
from src.models import CameraFrame, DetectionResult


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_camera_preserves_coordinates_and_identity(self):
        frame = CameraFrame(trace_id='t', session_id='s', frame_id=1,
                            width=3, height=2, prompts=[], image_b64='jpeg')
        with patch.object(main, 'broadcast', new_callable=AsyncMock) as send:
            await main.receive_camera_frame(frame)
        data = send.call_args.args[0]['data']
        self.assertEqual(data, dict(trace_id='t', session_id='s', frame_id=1,
                                   width=3, height=2, prompts=[], frame='jpeg'))

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
