from qunling.performance import ModelTimings, request_timings


def test_timings_bound_groups_samples_and_age_without_retaining_content():
    now = [0.0]
    stats = ModelTimings(limit=2, max_groups=2, clock=lambda: now[0])
    for value in (1, 2, 3):
        stats.record('100', 'judge', value, value != 3)
    item = stats.snapshot('100')['stages']['judge']
    assert item == {'count': 2, 'failed': 1, 'p50_seconds': 2, 'p95_seconds': 3, 'total_seconds': 5}
    stats.record('200', 'generate', 1, True)
    stats.record('300', 'generate', 1, True)
    assert stats.snapshot('100')['stages']['judge']['count'] == 0
    stats.record('private text', 'generate', 1, True)
    stats.record('300', 'unknown', 1, True)
    stats.record('300', 'generate', float('nan'), True)
    assert stats.snapshot()['stages']['generate']['count'] == 2
    now[0] = 3601
    assert stats.snapshot()['stages']['generate']['count'] == 0


def test_request_intervals_and_incomplete_history_are_not_misrepresented():
    events = [{'at': 1, 'phase': 'queued'}, {'at': 4, 'phase': 'model'},
              {'at': 6, 'phase': 'tool', 'tool': 'search_web'}, {'at': 10, 'phase': 'ended'}]
    result = request_timings(events, 10)
    assert result['complete']
    assert result['phase_seconds'] == {'queued': 3, 'model': 2, 'tool': 4, 'ended': 0}
    assert result['tool_seconds'] == {'search_web': 4}
    partial = request_timings(events, 10, max_events=4)
    assert not partial['complete'] and partial['unobserved_seconds'] == 3
    assert 'queued' not in partial['phase_seconds']
