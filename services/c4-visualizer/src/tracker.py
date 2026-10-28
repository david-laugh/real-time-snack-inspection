"""Cookie tracking across frames so that each cookie is counted once.

C3 object_id is only unique inside a frame, so C4 links detections itself:
- results are processed per session in frame_id order (short reorder buffer),
- a previous-frame cookie and a current cookie match when they are on the same
  line (|dy| <= diameter / 2) and the current one is ahead in the travel
  direction; the closest pairs are matched first,
- a cookie is counted once when its center crosses the scan line, and its
  status is the majority of the C3 statuses within `vote_frames` frames
  before and after the crossing.
"""
from collections import Counter
from dataclasses import dataclass, field

DIRECTIONS = ('left', 'right')


@dataclass
class Observation:
    frame_id: int
    cx: float
    status: str
    defect_type: str | None
    defect_score: float
    object_id: str


@dataclass
class Track:
    track_id: str
    cx: float
    cy: float
    diameter: float
    last_frame_id: int
    history: list[Observation] = field(default_factory=list)
    missed: int = 0
    cross_index: int | None = None
    counted: bool = False


@dataclass
class SessionState:
    buffer: dict = field(default_factory=dict)  # frame_id -> (arrived_ms, detections, width)
    tracks: list[Track] = field(default_factory=list)
    last_frame_id: int | None = None
    width: int | None = None
    last_seen_ms: float = 0.0
    next_id: int = 0


class CookieTracker:
    def __init__(self, scan_line_ratio=0.34, direction='left', vote_frames=2, reorder_ms=500,
                 max_step_ratio=1.0, jitter_ratio=0.25, max_missed=3, idle_ms=10000):
        if direction not in DIRECTIONS:
            raise ValueError(f'direction must be one of {DIRECTIONS}')
        self.set_scan_line(scan_line_ratio)
        self.direction = direction
        self.vote_frames = vote_frames
        self.reorder_ms = reorder_ms
        self.max_step_ratio = max_step_ratio
        self.jitter_ratio = jitter_ratio
        self.max_missed = max_missed
        self.idle_ms = idle_ms
        self.sessions: dict[str, SessionState] = {}
        self.late_results = 0

    def set_scan_line(self, ratio):
        ratio = float(ratio)
        if not 0 < ratio < 1:
            raise ValueError('scan line ratio must be between 0 and 1')
        self.scan_line_ratio = ratio

    def config(self):
        return {'scan_line_ratio': self.scan_line_ratio, 'direction': self.direction,
                'vote_frames': self.vote_frames, 'reorder_ms': self.reorder_ms}

    def note_width(self, session_id, width):
        self._session(session_id).width = width

    def add_result(self, session_id, frame_id, detections, now_ms, width=None):
        """Buffer one C3 result; detections are dicts with bbox/status/defect_type/defect_score/item_id."""
        state = self._session(session_id)
        state.last_seen_ms = now_ms
        if (state.last_frame_id is not None and frame_id <= state.last_frame_id) or frame_id in state.buffer:
            self.late_results += 1  # too late to keep frame_id order, or a duplicate delivery
            return
        state.buffer[frame_id] = (now_ms, detections, width)

    def flush(self, now_ms, force=False):
        """Process buffered results that are old enough; returns newly counted cookies."""
        counted = []
        for session_id, state in list(self.sessions.items()):
            while state.buffer:
                frame_id = min(state.buffer)
                arrived, detections, width = state.buffer[frame_id]
                if not force and now_ms - arrived < self.reorder_ms:
                    break  # keep order: a smaller frame_id may still arrive
                del state.buffer[frame_id]
                counted += self._process(session_id, state, frame_id, detections, width)
            if force or (not state.buffer and now_ms - state.last_seen_ms > self.idle_ms):
                for track in state.tracks:
                    counted += self._finalize(session_id, track)
                state.tracks = []
                if not force:
                    del self.sessions[session_id]
        return counted

    def _session(self, session_id):
        return self.sessions.setdefault(session_id, SessionState())

    def _progress(self, before_x, after_x):
        return before_x - after_x if self.direction == 'left' else after_x - before_x

    def _before_line(self, cx, width):
        line = self.scan_line_ratio * width
        return cx > line if self.direction == 'left' else cx < line

    def _process(self, session_id, state, frame_id, detections, width):
        state.last_frame_id = frame_id
        width = width or state.width
        if width:
            state.width = width
        cookies = []
        for det in detections:
            x1, y1, x2, y2 = det['bbox']
            cookies.append(((x1 + x2) / 2, (y1 + y2) / 2, max(1.0, ((x2 - x1) + (y2 - y1)) / 2), det))

        pairs = []
        for ti, track in enumerate(state.tracks):
            gap = max(1, frame_id - track.last_frame_id)
            for ci, (cx, cy, diameter, _) in enumerate(cookies):
                size = (track.diameter + diameter) / 2
                if abs(cy - track.cy) > size / 2:
                    continue  # different line
                step = self._progress(track.cx, cx)
                if -self.jitter_ratio * size <= step <= self.max_step_ratio * size * gap:
                    pairs.append((abs(step), ti, ci))
        pairs.sort()

        matched_tracks, matched_cookies = set(), set()
        for _, ti, ci in pairs:
            if ti in matched_tracks or ci in matched_cookies:
                continue
            matched_tracks.add(ti)
            matched_cookies.add(ci)
            track = state.tracks[ti]
            cx, cy, diameter, det = cookies[ci]
            was_before = width and self._before_line(track.cx, width)
            self._observe(track, frame_id, cx, cy, diameter, det)
            if was_before and track.cross_index is None and not self._before_line(cx, width):
                track.cross_index = len(track.history) - 1

        for ci, (cx, cy, diameter, det) in enumerate(cookies):
            if ci not in matched_cookies:
                state.next_id += 1
                track = Track(f'{session_id}#{state.next_id}', cx, cy, diameter, frame_id)
                self._observe(track, frame_id, cx, cy, diameter, det)
                state.tracks.append(track)

        counted, alive = [], []
        for track in state.tracks:
            if track.last_frame_id != frame_id:
                track.missed += 1
            if track.cross_index is not None and len(track.history) - 1 - track.cross_index >= self.vote_frames:
                counted += self._finalize(session_id, track)
            if track.missed > self.max_missed:
                counted += self._finalize(session_id, track)
            else:
                alive.append(track)
        state.tracks = alive
        return counted

    @staticmethod
    def _observe(track, frame_id, cx, cy, diameter, det):
        track.cx, track.cy, track.last_frame_id, track.missed = cx, cy, frame_id, 0
        track.diameter = (track.diameter * len(track.history) + diameter) / (len(track.history) + 1)
        track.history.append(Observation(frame_id, cx, det['status'], det.get('defect_type'),
                                         float(det.get('defect_score') or 0.0), str(det.get('item_id', ''))))

    def _finalize(self, session_id, track):
        if track.counted or track.cross_index is None:
            return []
        track.counted = True
        k = self.vote_frames
        votes = track.history[max(0, track.cross_index - k):track.cross_index + k + 1]
        tally = Counter(v.status for v in votes)
        # unknown abstains; a tie between normal and defect is reported as defect.
        if tally['defect'] == tally['normal'] == 0:
            status = 'unknown'
        else:
            status = 'defect' if tally['defect'] >= tally['normal'] else 'normal'
        defect_votes = [v for v in votes if v.status == 'defect']
        defect_types = Counter(v.defect_type for v in defect_votes if v.defect_type)
        crossing = track.history[track.cross_index]
        return [{
            'cookie_id': track.track_id,
            'session_id': session_id,
            'status': status,
            'defect_type': defect_types.most_common(1)[0][0] if status == 'defect' and defect_types else None,
            'defect_score': round(max(v.defect_score for v in votes), 4),
            'votes': {'normal': tally['normal'], 'defect': tally['defect'], 'unknown': tally['unknown']},
            'crossed_frame_id': crossing.frame_id,
            'first_frame_id': track.history[0].frame_id,
            'last_frame_id': track.history[-1].frame_id,
            'frames_seen': len(track.history),
            'last_object_id': track.history[-1].object_id,
        }]
