"""Bounded timing metadata, without prompts, credentials or tool arguments."""
from __future__ import annotations

from collections import OrderedDict, deque
import math
import threading
import time

MODEL_STAGES = frozenset({'judge', 'generate', 'reconcile'})


def _summary(samples):
    values = sorted(value for value, _ in samples)
    if not values:
        return {'count': 0, 'failed': 0, 'p50_seconds': 0, 'p95_seconds': 0, 'total_seconds': 0}
    return {'count': len(values), 'failed': sum(not success for _, success in samples),
            'p50_seconds': round(values[math.ceil(len(values) * .5) - 1], 3),
            'p95_seconds': round(values[math.ceil(len(values) * .95) - 1], 3),
            'total_seconds': round(sum(values), 3)}


class ModelTimings:
    """One Host bot owns this object; groups and samples have hard bounds."""
    def __init__(self, *, limit=200, max_groups=100, clock=time.monotonic):
        self.limit, self.max_groups, self.clock = limit, max_groups, clock
        self._groups = OrderedDict()
        self._lock = threading.Lock()

    def record(self, group, stage, seconds, success):
        if (stage not in MODEL_STAGES or not isinstance(group, str) or len(group) > 32
                or group and (not group.isascii() or not group.isdigit())
                or type(seconds) not in (float, int) or not math.isfinite(seconds) or seconds < 0):
            return
        with self._lock:
            samples = self._groups.setdefault(group, deque(maxlen=self.limit))
            self._groups.move_to_end(group)
            samples.append((self.clock(), stage, min(seconds, 3600), bool(success)))
            while len(self._groups) > self.max_groups:
                self._groups.popitem(last=False)

    def snapshot(self, group=None):
        now = self.clock()
        with self._lock:
            groups = [self._groups.get(group, ())] if group is not None else self._groups.values()
            samples = [entry for values in groups for entry in values if 0 <= now - entry[0] <= 3600]
        return {'window_seconds': 3600, 'max_samples_per_group': self.limit, 'reset_on_restart': True,
                'stages': {stage: _summary([(seconds, success) for _, kind, seconds, success in samples
                                           if kind == stage]) for stage in sorted(MODEL_STAGES)}}


def request_timings(events, end, *, max_events=96):
    """Aggregate recorded stage intervals; truncated history is never complete."""
    phases, tools = {}, {}
    complete = len(events) < max_events
    unobserved = 0.0
    for index, event in enumerate(events):
        start = event.get('at')
        stop = events[index + 1].get('at') if index + 1 < len(events) else end
        if (type(start) not in (int, float) or type(stop) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(stop)):
            complete = False
            continue
        seconds = max(0, stop - start)
        # The journal keeps its first event followed by its newest events.
        if not complete and index == 0:
            unobserved += seconds
            continue
        phase = event.get('phase', 'unknown')
        phases[phase] = phases.get(phase, 0) + seconds
        if phase == 'tool' and event.get('tool'):
            name = event['tool']
            tools[name] = tools.get(name, 0) + seconds
    return {'complete': complete, 'unobserved_seconds': round(unobserved, 3),
            'phase_seconds': {key: round(value, 3) for key, value in phases.items()},
            'tool_seconds': {key: round(value, 3) for key, value in tools.items()}}
