"""Measure serial medium answer checks through the deployed app using invented Case 1.

Uses normal session, CSRF, queue and quota controls. Never reads an AMD key, changes
limits or retries a failed inference. Session cookies remain in memory only.
"""
import argparse
from datetime import datetime, timezone
from http.cookiejar import CookieJar
import json
from pathlib import Path
import statistics
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPCookieProcessor, Request


QUESTIONS = ['What does follow-up mean?', 'What date and time is my follow-up appointment?']


def summarize(rows):
    result = {'attempts': len(rows), 'completed': sum(r['status'] == 'completed' for r in rows),
              'failed': sum(r['status'] != 'completed' for r in rows)}
    completed = [r for r in rows if r['status'] == 'completed']
    keys = sorted({k for r in completed for k, v in r.items() if k.endswith('_seconds') and isinstance(v, (int, float))})
    result['successful_request_seconds'] = {k: {'minimum': round(min(values), 4),
        'median': round(statistics.median(values), 4), 'maximum': round(max(values), 4)}
        for k in keys if (values := [r[k] for r in completed if k in r])}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='https://afterward-demo.up.railway.app')
    parser.add_argument('--samples', type=int, default=8)
    parser.add_argument('--interval', type=float, default=20, help='Minimum seconds between new requests.')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    url = urlsplit(base)
    if url.scheme != 'https' or url.username or url.password or url.query or url.fragment or url.path:
        parser.error('Use an HTTPS origin without credentials, path, query or fragment.')
    if not 1 <= args.samples <= 20 or not 10 <= args.interval <= 300:
        parser.error('Use 1-20 samples and a 10-300 second start interval.')
    if args.output.exists():
        parser.error('Choose a new output file to preserve previous measurements.')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def request(path, payload=None, csrf=None):
        headers = {'Origin': base, 'X-AfterWard-Request': '1'}
        if csrf:
            headers['X-AfterWard-CSRF'] = csrf
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        req = Request(base + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        try:
            with opener.open(req, timeout=35) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise RuntimeError(f'Application request failed with HTTP {code}; no automatic retry.') from None

    report = {'started_at_utc': datetime.now(timezone.utc).isoformat(), 'base_url': base,
        'method': 'Normal public app session and patient answer endpoint, invented letter and two fixed Case 1 questions. Serial requests, medium unchanged. All failures retained; stop after first failure. Three-second polling approximates browser polling without measuring rendering.',
        'requested_samples': args.samples, 'minimum_start_interval_seconds': args.interval,
        'scope': 'Small operational latency sample, not clinical validation or a stable tail-latency estimate. Connection includes DNS/TCP and TLS/proxy tunnel; response-header wait combines upstream queue, non-streaming generation and network time.',
        'runs': []}

    def save():
        report['updated_at_utc'] = datetime.now(timezone.utc).isoformat()
        report['summary'] = summarize(report['runs'])
        args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')

    save()
    request('/api/start', {'route': 'hospital', 'acknowledged': True})
    state = request('/api/patient')
    if [d['identifier'] for d in state['documents']] != ['letter']:
        raise RuntimeError('Case 1 must expose only the letter to the patient.')
    last_start = None
    for index in range(args.samples):
        if last_start is not None:
            time.sleep(max(0, args.interval - (time.perf_counter() - last_start)))
        state = request('/api/patient')
        previous = {j['id'] for j in state['ai_jobs']}
        row = {'sample': index + 1, 'started_at_utc': datetime.now(timezone.utc).isoformat(), 'status': 'client_error'}
        last_start = time.perf_counter()
        try:
            state = request('/api/patient/start_ai', {'kind': 'answer', 'documents': ['letter'],
                'questions': QUESTIONS, 'revision': state['revision']}, state['csrf'])
            row['submission_seconds'] = round(time.perf_counter() - last_start, 4)
            job = next(j for j in state['ai_jobs'] if j['id'] not in previous)
            row['task_id'] = job['id']
            while job['status'] in ('queued', 'running'):
                if time.perf_counter() - last_start > 600:
                    raise RuntimeError('Observation timed out; task may still be running. No replacement request sent.')
                time.sleep(3)
                state = request('/api/patient')
                job = next(j for j in state['ai_jobs'] if j['id'] == row['task_id'])
            row['status'] = job['status']
            created = datetime.fromisoformat(job['created_at_utc'])
            started = datetime.fromisoformat(job['started_at_utc'])
            finished = datetime.fromisoformat(job['finished_at_utc'])
            row.update(queue_seconds=round((started-created).total_seconds(), 4),
                processing_seconds=round((finished-started).total_seconds(), 4),
                server_total_seconds=round((finished-created).total_seconds(), 4))
            if job.get('result'):
                result = job['result']
                row['labels'] = [answer['status'] for answer in result['answers']]
                row['expected_labels_match'] = row['labels'] == ['found', 'not_found']
                call, = result['calls']
                if call['reasoning_effort'] != 'medium':
                    raise RuntimeError('Unexpected model effort; stop the measurement.')
                row.update(model=call['model'], reasoning_effort=call['reasoning_effort'],
                    attempt_id=call['attempt_id'], usage=call['usage'],
                    call_seconds=call['elapsed_seconds'], **call.get('timings', {}))
            if job.get('retry_after_utc'):
                row['retry_after_utc'] = job['retry_after_utc']
        except Exception as exc:
            row['status'] = 'client_error'
            row['error_type'] = type(exc).__name__
        row['observed_seconds'] = round(time.perf_counter() - last_start, 4)
        report['runs'].append(row)
        save()
        print(json.dumps(row), flush=True)
        if row['status'] != 'completed':
            report['stopped_after_failure'] = True
            break
    report['completed_at_utc'] = datetime.now(timezone.utc).isoformat()
    save()
    print(json.dumps(report['summary']), flush=True)


if __name__ == '__main__':
    main()
