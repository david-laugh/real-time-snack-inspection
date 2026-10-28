import json
import random
import unittest
from pathlib import Path

from src.tracker import CookieTracker

LABELS = Path(__file__).resolve().parents[2] / 'c2-preprocessing/tests/fixtures/labels_2026-10-28T01-48-30.json'
WIDTH = 1024  # C2 resizes the 1045 px simulator frame to 1024 px (C3 mask fixture size)


def simulator_frames(frames=40, fps=10, direction='left'):
    """Move the labelled cookies at the simulator speed and emit C3-like detections per frame."""
    labels = json.loads(LABELS.read_text(encoding='utf-8'))
    scale = WIDTH / labels['image']['width']
    step = labels['params']['speed'] / fps * scale * (1 if direction == 'right' else -1)
    out = []
    for frame_id in range(frames):
        detections = []
        for a in labels['annotations']:
            x, y, w, h = (v * scale for v in a['bbox'])
            if direction == 'right':
                x = WIDTH - x - w
            x += step * frame_id
            if x + w / 2 < 0 or x + w / 2 > WIDTH:
                continue  # left the image
            defect = a['category'] != 'round'
            detections.append(dict(item_id=f'c{len(detections)}', bbox=[x, y, x + w, y + h],
                                   status='defect' if defect else 'normal',
                                   defect_type='broken' if defect else None,
                                   defect_score=0.8 if defect else 0.1, label_id=a['id']))
        out.append((frame_id, detections))
    return labels, out


def expected_crossings(labels, frames=40, fps=10, ratio=0.34):
    line = ratio * labels['image']['width']
    travel = labels['params']['speed'] / fps * (frames - 1)
    crossing = [a for a in labels['annotations']
                if a['bbox'][0] + a['bbox'][2] / 2 > line >= a['bbox'][0] + a['bbox'][2] / 2 - travel]
    return len(crossing), sum(a['category'] != 'round' for a in crossing)


def run(tracker, frames, order=None, session='s'):
    counted = []
    now = 0
    for frame_id, detections in (order or frames):
        now += 100
        tracker.add_result(session, frame_id, detections, now, width=WIDTH)
        counted += tracker.flush(now)
    counted += tracker.flush(now + 10_000, force=True)
    return counted


class SimulatorLabelTests(unittest.TestCase):
    def test_simulator_labels_assumption(self):
        labels, _ = simulator_frames()
        self.assertAlmostEqual(labels['params']['scan_line_x'] / labels['image']['width'], 0.34, places=2)
        self.assertEqual(expected_crossings(labels), (6, 3))

    def test_each_cookie_is_counted_once_across_many_frames(self):
        labels, frames = simulator_frames()
        counted = run(CookieTracker(reorder_ms=0), frames)
        total, defects = expected_crossings(labels)
        self.assertEqual(len(counted), total)
        self.assertEqual(sum(c['status'] == 'defect' for c in counted), defects)
        self.assertEqual(len({c['cookie_id'] for c in counted}), total)
        self.assertTrue(all(c['frames_seen'] >= 10 for c in counted))
        self.assertTrue(all(c['defect_type'] == 'broken' for c in counted if c['status'] == 'defect'))

    def test_out_of_order_duplicates_and_dropped_frames(self):
        labels, frames = simulator_frames()
        rng = random.Random(7)
        kept = [f for f in frames if f[0] % 4 != 2]          # C2 dropped some frames
        shuffled = kept[:]
        for i in range(0, len(shuffled) - 2, 3):              # C3 replicas reorder neighbours
            shuffled[i], shuffled[i + 2] = shuffled[i + 2], shuffled[i]
        shuffled.insert(10, shuffled[5])                       # duplicate delivery
        for _, detections in shuffled:
            for d in detections:                               # bbox jitter
                d['bbox'] = [v + rng.uniform(-2, 2) for v in d['bbox']]
        tracker = CookieTracker(reorder_ms=500)
        counted = run(tracker, frames, order=shuffled)
        total, defects = expected_crossings(labels)
        self.assertEqual(len(counted), total)
        self.assertEqual(sum(c['status'] == 'defect' for c in counted), defects)
        self.assertEqual(tracker.late_results, 1)

    def test_status_is_majority_around_the_line(self):
        labels, frames = simulator_frames()
        line = 0.34 * WIDTH
        for _, detections in frames:  # one noisy C3 vote right at the line flips nothing
            for d in detections:
                cx = (d['bbox'][0] + d['bbox'][2]) / 2
                if line - 8 < cx <= line + 8:
                    d['status'] = 'normal' if d['status'] == 'defect' else 'defect'
        counted = run(CookieTracker(reorder_ms=0), frames)
        self.assertEqual(sum(c['status'] == 'defect' for c in counted), expected_crossings(labels)[1])
        self.assertTrue(all(sum(c['votes'].values()) == 5 for c in counted))

    def test_right_direction_and_scan_line_change(self):
        _, frames = simulator_frames(direction='right')
        self.assertEqual(len(run(CookieTracker(direction='right', reorder_ms=0), frames)), 6)
        tracker = CookieTracker(reorder_ms=0)
        tracker.set_scan_line(0.95)  # every cookie starts left of x=0.95 when travelling left
        _, frames = simulator_frames()
        self.assertEqual(len(run(tracker, frames)), 0)
        with self.assertRaises(ValueError):
            tracker.set_scan_line(1.0)


class TrackerRuleTests(unittest.TestCase):
    def det(self, x, y=50, d=20, status='normal'):
        return dict(item_id='c', bbox=[x - d / 2, y - d / 2, x + d / 2, y + d / 2], status=status)

    def test_other_line_and_backwards_motion_start_new_tracks(self):
        t = CookieTracker(reorder_ms=0)
        t.add_result('s', 1, [self.det(50)], 0, width=100)
        t.add_result('s', 2, [self.det(45, y=70)], 0, width=100)  # dy 20 > d/2: another line
        t.flush(0)
        self.assertEqual(len(t.sessions['s'].tracks), 2)
        t.add_result('s', 3, [self.det(60)], 0, width=100)          # moved against travel direction
        t.flush(0)
        self.assertEqual(len(t.sessions['s'].tracks), 3)

    def test_counted_once_even_if_cookie_oscillates_at_line(self):
        t = CookieTracker(reorder_ms=0, vote_frames=1)
        counted = []
        for frame_id, x in enumerate([40, 36, 33, 35, 32, 30, 28]):
            t.add_result('s', frame_id, [self.det(x)], 0, width=100)
            counted += t.flush(0)
        counted += t.flush(0, force=True)
        self.assertEqual(len(counted), 1)
        self.assertEqual(counted[0]['crossed_frame_id'], 2)

    def test_reorder_buffer_waits_for_smaller_frame_id(self):
        t = CookieTracker(reorder_ms=500)
        t.add_result('s', 2, [], 0, width=100)
        t.add_result('s', 1, [], 400, width=100)
        t.flush(600)
        self.assertEqual(sorted(t.sessions['s'].buffer), [1, 2])  # 1 is not old enough, 2 waits for it
        t.flush(900)
        self.assertEqual(t.sessions['s'].buffer, {})
        self.assertEqual(t.sessions['s'].last_frame_id, 2)

    def test_idle_session_is_finalized_and_dropped(self):
        t = CookieTracker(reorder_ms=0, vote_frames=5)
        t.note_width('s', 100)  # width from the C2 camera frame when results carry no mask
        for frame_id, x in enumerate([40, 33, 30]):
            t.add_result('s', frame_id, [self.det(x, status='defect')], frame_id)
        self.assertEqual(t.flush(5), [])
        counted = t.flush(20_000)
        self.assertEqual([c['status'] for c in counted], ['defect'])
        self.assertNotIn('s', t.sessions)


if __name__ == '__main__':
    unittest.main()
