#!/usr/bin/env python3
"""Collect changed Office records; never rate efficiency or edit source sessions."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

TASK_FIELDS = ('id', 'job', 'title', 'owner', 'status', 'note', 'rework_of',
               'completion', 'execution', 'metrics', 'wait_on', 'wait_pid',
               'wait_log', 'wait_reason', 'claimed_at', 'done_at', 'kind', 'level',
               'handoffs', 'failover_from', 'failover_to', 'failover_reason',
               'failover_at', 'wait_overdue_count', 'wait_deadline')
JOB_FIELDS = ('id', 'text', 'status', 'cancelled', 'summary', 'finished_at',
              'rework_count', 'lead', 'acting_lead', 'acting_reason', 'verification')
V2_TASK_FIELDS = {'progress_state', 'kind', 'level', 'handoffs', 'failover_from',
                  'failover_to', 'failover_reason', 'failover_at',
                  'wait_overdue_count', 'wait_deadline'}
V2_JOB_FIELDS = {'rework_count', 'lead', 'acting_lead', 'acting_reason', 'verification'}
NOTICE_PREFIX = '[효율 감시]'
ERROR_THRESHOLD = 3


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def save(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def load(path):
    return json.loads(path.read_text())


def records(source):
    rows = {}
    for kind, fields in (('task', TASK_FIELDS), ('job', JOB_FIELDS)):
        for item in source.get(kind + 's', []):
            row = {key: item[key] for key in fields if key in item}
            if kind == 'task':
                row['progress_state'] = (item.get('progress') or {}).get('state')
            rows[f'{kind}:{item["id"]}'] = row
    for item in source.get('msgs', []):
        # Progress/notes are intentionally quiet; turns retain actual execution context.
        if (item.get('kind') in ('progress', 'note', 'event')
                or (item.get('from') in ('codex', 'claude')
                    and item.get('text', '').startswith(NOTICE_PREFIX))):
            continue
        rows[f'msg:{item["id"]}'] = item
    return rows


def scan(root, state, now):
    errors = []
    added = 0
    changed = {}
    lineage = state.setdefault('lineage', {})
    excluded = set(state.get('excluded_sessions', [state['exclude']]))
    excluded.add(state['exclude'])
    failures = state.setdefault('read_failures', {})
    for path in sorted((root / '.office/sessions').glob('*/state.json')):
        sid = path.parent.name
        if sid in excluded:
            continue
        try:
            st = path.stat()
            signature = [st.st_mtime_ns, st.st_size, st.st_ino]
            previous = state['sessions'].get(sid, {})
            if previous.get('signature') == signature and state.get('version') == 2:
                continue
            source = load(path)
            parent = source.get('continued_from', {})
            if isinstance(parent, dict) and parent.get('session'):
                lineage[sid] = parent['session']
            changed[sid] = (signature, records(source))
            failures.pop(sid, None)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            errors.append({'session': sid, 'error': str(error)})
            old = failures.get(sid, {})
            count = old.get('count', 0) + 1
            failures[sid] = {'count': count, 'notified': old.get('notified', False)}
    while True:
        descendants = {sid for sid, parent in lineage.items() if parent in excluded}
        if descendants <= excluded:
            break
        excluded.update(descendants)
    state['excluded_sessions'] = sorted(excluded)
    for sid in excluded:
        state['sessions'].pop(sid, None)
        failures.pop(sid, None)
    errors = [row for row in errors if row['session'] not in excluded]
    for sid, (signature, rows) in changed.items():
        if sid in excluded:
            continue
        previous = state['sessions'].get(sid, {})
        # Keep message identities even after old records are removed or archived.
        hashes = {key: value for key, value in previous.get('hashes', {}).items()
                  if key.startswith('msg:')}
        for key, row in rows.items():
            fingerprint = digest(row)
            hashes[key] = fingerprint
            old = previous.get('hashes', {}).get(key)
            comparison = fingerprint
            if state.get('version') == 1 and not key.startswith('msg:'):
                introduced = V2_TASK_FIELDS if key.startswith('task:') else V2_JOB_FIELDS
                comparison = digest({k: v for k, v in row.items() if k not in introduced})
            is_new = old is None if key.startswith('msg:') else old != comparison
            if is_new and not state.get('bootstrapping'):
                state['queue'].append({'session': sid, 'key': key, 'hash': fingerprint,
                                       'detected_at': now, 'record': row})
                added += 1
        state['sessions'][sid] = {'signature': signature, 'hashes': hashes}
    for row in errors:
        failure = failures[row['session']]
        if failure['count'] >= ERROR_THRESHOLD and not failure['notified']:
            state['queue'].append({'session': row['session'], 'key': 'read_error',
                                   'detected_at': now, 'record': row})
            failure['notified'] = True
            added += 1
    if state['queue'] and state.get('first_pending_at') is None:
        state['first_pending_at'] = now
    state['last_scan_at'] = now
    state['errors'] = errors
    state['version'] = 2
    return added


def publish(folder, state, now, batch_seconds):
    if not state['queue'] or now - state['first_pending_at'] < batch_seconds:
        return False
    # Do not overwrite an unreviewed batch. A restart returns the same batch.
    if not state.get('pending'):
        batch_id = digest(state['queue'])[:20]
        state['pending'] = {'id': batch_id, 'count': len(state['queue']),
                            'path': str(folder / f'batch-{batch_id}.json')}
        save(Path(state['pending']['path']), {'id': batch_id, 'events': state['queue'],
             'created_at': now, 'read_errors': state['errors']})
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'scan', 'watch', 'ack'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--exclude-session', required=True)
    parser.add_argument('--days', type=float, default=7)
    parser.add_argument('--poll-seconds', type=float, default=60)
    parser.add_argument('--batch-seconds', type=float, default=900)
    parser.add_argument('--batch-id')
    args = parser.parse_args()
    if args.days <= 0 or args.poll_seconds < 1 or args.batch_seconds < 0:
        parser.error('days > 0, poll-seconds >= 1, batch-seconds >= 0 required')
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / 'watch.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(2, 'monitor already running\n')
        path = args.output / 'cursor.json'
        if args.action == 'init':
            if path.exists():
                parser.exit(2, 'existing cursor preserved; use scan/watch\n')
            now = time.time()
            state = {'version': 2, 'root': str(args.root.resolve()),
                     'exclude': args.exclude_session, 'started_at': now,
                     'expires_at': now + args.days * 86400, 'sessions': {},
                     'queue': [], 'first_pending_at': None, 'bootstrapping': True}
            scan(args.root, state, now)
            state.pop('bootstrapping')
            save(path, state)
            print(json.dumps({'baseline_sessions': len(state['sessions']),
                              'expires_at': state['expires_at'], 'errors': state['errors']}))
            return
        state = load(path)
        if state['exclude'] != args.exclude_session or state['root'] != str(args.root.resolve()):
            parser.error('cursor root/session mismatch')
        if args.action == 'ack':
            pending = state.get('pending')
            if not pending or pending['id'] != args.batch_id:
                parser.error('matching pending batch-id required')
            state['queue'] = state['queue'][pending['count']:]
            state['first_pending_at'] = state['queue'][0]['detected_at'] if state['queue'] else None
            state['last_reviewed'] = pending
            state.pop('pending')
            save(path, state)
            print('review acknowledged')
            return
        while True:
            now = time.time()
            scan(args.root, state, now)
            ready = publish(args.output, state, now, args.batch_seconds)
            save(path, state)
            if ready:
                print(json.dumps({'review_ready': state['pending']}, ensure_ascii=False), flush=True)
                return
            if now >= state['expires_at']:
                # Deliver the final short batch before stopping; no silent data loss.
                publish(args.output, state, now, 0)
                save(path, state)
                print(json.dumps({'expired': True, 'pending': state.get('pending'),
                                  'read_errors': state['errors']}), flush=True)
                return
            if args.action == 'scan':
                print(json.dumps({'queued': len(state['queue']), 'errors': state['errors']}))
                return
            time.sleep(min(args.poll_seconds, max(0.01, state['expires_at'] - now)))


if __name__ == '__main__':
    main()
