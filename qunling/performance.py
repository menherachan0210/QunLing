"""Bounded timing metadata, without prompts, credentials or tool arguments."""
from __future__ import annotations

from collections import OrderedDict, deque
from contextvars import ContextVar
import json
import os
import math
import threading
import time

MODEL_STAGES = frozenset({'judge', 'generate', 'reconcile', 'review', 'review_gate'})
USAGE_CONTEXT = ContextVar('qunling_model_usage', default=None)


def capture_usage(value):
    """Provider hook: retain counters only, inside this invocation's task context."""
    target = USAGE_CONTEXT.get()
    if target is None or not isinstance(value, dict):
        return
    counters = {}
    for field in ('prompt_tokens', 'completion_tokens'):
        item = value.get(field)
        if type(item) is int and 0 <= item <= 10**9:
            counters[field] = item
    if len(counters) == 2:
        target['usage'] = counters


def _usage_summary(samples):
    total_in = total_out = known = priced = 0
    cost = 0.0
    try:
        prices = json.loads(os.environ.get('QUNLING_MODEL_PRICES', '{}'))
        if not isinstance(prices, dict):
            prices = {}
    except ValueError:
        prices = {}
    for sample in samples:
        usage, model = sample.get('usage'), sample.get('model_id')
        if not usage:
            continue
        known += 1
        total_in += usage['prompt_tokens']
        total_out += usage['completion_tokens']
        price = prices.get(model)
        if (isinstance(price, dict) and all(type(price.get(key)) in (int, float)
                and math.isfinite(price[key]) and 0 <= price[key] <= 10**9
                for key in ('input_per_million', 'output_per_million'))):
            priced += 1
            cost += (usage['prompt_tokens'] * price['input_per_million']
                     + usage['completion_tokens'] * price['output_per_million']) / 1_000_000
    return {'requests': len(samples), 'with_usage': known, 'unknown_usage': len(samples) - known,
            'input_tokens': total_in, 'output_tokens': total_out, 'priced_requests': priced,
            'estimated_cost': round(cost, 8) if priced == len(samples) and priced else None}


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

    def record(self, group, stage, seconds, success, *, usage=None, member='', model_id=''):
        if (stage not in MODEL_STAGES or not isinstance(group, str) or len(group) > 32
                or group and (not group.isascii() or not group.isdigit())
                or type(seconds) not in (float, int) or not math.isfinite(seconds) or seconds < 0):
            return
        with self._lock:
            samples = self._groups.setdefault(group, deque(maxlen=self.limit))
            self._groups.move_to_end(group)
            if not isinstance(member, str) or len(member) > 64:
                member = ''
            counters = usage if isinstance(usage, dict) and all(type(usage.get(k)) is int
                and 0 <= usage[k] <= 10**9 for k in ('prompt_tokens', 'completion_tokens')) else None
            samples.append((self.clock(), stage, min(seconds, 3600), bool(success),
                            {'usage': counters, 'member': member, 'model_id': model_id}))
            while len(self._groups) > self.max_groups:
                self._groups.popitem(last=False)

    def snapshot(self, group=None):
        now = self.clock()
        with self._lock:
            groups = [self._groups.get(group, ())] if group is not None else self._groups.values()
            samples = [entry for values in groups for entry in values if 0 <= now - entry[0] <= 3600]
        return {'window_seconds': 3600, 'max_samples_per_group': self.limit, 'reset_on_restart': True,
                'stages': {stage: _summary([(seconds, success) for _, kind, seconds, success, _ in samples
                                           if kind == stage]) for stage in sorted(MODEL_STAGES)},
                'usage': {'stages': {stage: _usage_summary([info for _, kind, _, _, info in samples if kind == stage])
                                     for stage in sorted(MODEL_STAGES)},
                          'members': {member: _usage_summary([info for *_, info in samples if info['member'] == member])
                                      for member in sorted({info['member'] for *_, info in samples if info['member']})}}}


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
