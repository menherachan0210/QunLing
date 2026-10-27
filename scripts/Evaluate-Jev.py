"""Evaluate the pinned review gate against labelled drafts without sending QQ messages.

Example: QUNLING_JEV_API_KEY_FILE=/protected/typesafe.key python scripts/Evaluate-Jev.py
The output contains decisions and timings, never credentials or conversation text.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qunling import dialogue_review, jev


def samples(path):
    value = json.loads(path.read_text(encoding='utf-8'))
    cases = value.get('cases') if isinstance(value, dict) else None
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError('Expected 1–100 paired review cases')
    result, seen = [], set()
    for case in cases:
        if (not isinstance(case, dict) or not isinstance(case.get('id'), str)
                or case['id'] in seen or not isinstance(case.get('persona'), str)
                or not isinstance(case.get('dialogue'), dict)
                or not isinstance(case.get('drafts'), dict)
                or set(case['drafts']) != {'good', 'bad'}):
            raise ValueError('Invalid paired review case')
        seen.add(case['id'])
        for expected, draft in case['drafts'].items():
            if not isinstance(draft, str) or not draft.strip():
                raise ValueError('Expected a nonempty draft')
            payload = {'dialogue':case['dialogue'], 'draft':draft,
                       'reply_character_limit':case.get('reply_character_limit',800)}
            state = {'review_policy':dialogue_review.PROMPT+'\n\n当前管理员人格：'+case['persona'],
                     'review':payload}
            body = json.dumps({'model':jev.MODEL,'state':state,'questions':jev.QUESTIONS},
                              ensure_ascii=False,allow_nan=False).encode('utf-8')
            result.append((case['id'], expected, body))
    return result


async def evaluate(args):
    key = jev._key()
    if not key:
        raise ValueError('Configure a protected QUNLING_JEV_API_KEY_FILE before evaluation')
    rows = samples(args.cases)
    report = {'model':jev.MODEL, 'cases_sha256':hashlib.sha256(args.cases.read_bytes()).hexdigest(),
              'questions_sha256':hashlib.sha256(json.dumps(jev.QUESTIONS,sort_keys=True).encode()).hexdigest(),
              'thresholds':{'keep_probability':jev.MIN_KEEP,'max_issue_probability':jev.MAX_ISSUE},
              'timeout_seconds':jev.TIMEOUT, 'repetitions':args.repeat, 'qq_messages_sent':0, 'results':[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for repeat in range(args.repeat):
        for name,expected,body in rows:
            result = {'id':name,'expected':expected,'repeat':repeat+1,
                      'input_sha256':hashlib.sha256(body).hexdigest()}
            started = time.monotonic()
            try:
                async with asyncio.timeout(jev.TIMEOUT):
                    value = await jev._request(key,body)
                result['approved'] = jev.approved(value)
                result['answers'] = value['answers']
            except Exception as exc:
                result['error_type'] = type(exc).__name__
            result['seconds'] = round(time.monotonic()-started,4)
            report['results'].append(result)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    results = report['results']
    times = sorted(row['seconds'] for row in results)
    summary = {'requests':len(results), 'errors':sum('error_type' in row for row in results),
               'good_fast_passes':sum(row.get('approved',False) and row['expected']=='good' for row in results),
               'bad_fast_passes':sum(row.get('approved',False) and row['expected']=='bad' for row in results),
               'median_seconds':round(statistics.median(times),4),
               'p95_seconds':times[math.ceil(len(times)*.95)-1]}
    report['summary'] = summary
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False))
    return 0 if summary['errors']==0 and summary['bad_fast_passes']==0 and summary['good_fast_passes']>0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',type=Path,default=ROOT/'tests/fixtures/jev_review_cases.json')
    parser.add_argument('--output',type=Path,default=ROOT/'.local/jev-review-evaluation.json')
    parser.add_argument('--repeat',type=int,choices=range(1,6),default=1)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output already exists; choose a new filename to preserve prior evidence')
    try:
        return asyncio.run(evaluate(args))
    except (OSError,ValueError) as exc:
        print('Evaluation could not start: '+str(exc),file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
