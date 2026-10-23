"""仅补写删除审计：用成对的真实删除计划及完成日志恢复旧事件，不执行清理。"""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.database.models import DeletionRecord
from app.database.session import session_scope
from app.utils import find_serial_numbers

_LINE = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \|\s*\w+\s*\| app\.services\.medialink:([^:]+):\d+ - (.*)$')
_PLAN = re.compile(r'^\[(.+)\] 媒体联动删除(（演练）)? —— 删种 (\d+) 个: (.*); 保留做种 (\d+) 个: (.*); 源文件 (\d+) 个: (.*); 硬链接 (\d+) 个: (.*)$')
_DONE = re.compile(r'^\[(.+)\] 联动删除完成 —— 种子 (\d+)(?:（另有 \d+ 个合集种子保留做种）)?，文件 (\d+)，刮削附属 (\d+)，目录 (\d+)，错误 (\d+)$')


def _values(count, text):
    count = int(count)
    values = [] if not count else [text] if count == 1 else text.split(', ')
    if len(values) != count:
        raise ValueError('旧日志列表存在歧义')
    return values


def parse_logs(paths, log_timezone='Asia/Shanghai'):
    tz = ZoneInfo(log_timezone)
    pending, events = {}, {}
    summary = dict(files=0, dry_runs=0, unmatched=0, ambiguous=0)
    for path in sorted(map(Path, paths), key=lambda p: (p.name == 'cinefold.log', p.name)):
        summary['files'] += 1
        with path.open(encoding='utf-8', errors='replace') as stream:
            for number, line in enumerate(stream, 1):
                match = _LINE.match(line.rstrip('\r\n'))
                if not match:
                    continue
                stamp, function, message = match.groups()
                now = datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S').replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
                # 防止缺失完成行的旧计划无限保留或与数日后的同番号事件配错。
                pending = {key: value for key, value in pending.items() if timedelta(0) <= now - value['created_at'] <= timedelta(minutes=30)}
                plan = _PLAN.match(message) if function in ('handle_media_deleted', '_handle_media_deleted') else None
                if plan:
                    label, dry, *parts = plan.groups()
                    if dry:
                        summary['dry_runs'] += 1
                        continue
                    try:
                        hashes, kept, sources, links = [_values(*parts[i:i+2]) for i in range(0, 8, 2)]
                        if not all(re.fullmatch(r'[0-9a-fA-F]{40,64}', value) for value in hashes + kept):
                            raise ValueError('无效 Hash')
                    except ValueError:
                        summary['ambiguous'] += 1
                        pending.pop(label, None)
                        continue
                    current = dict(label=label, created_at=now, file=path.name, line=number, message=message,
                                   hashes=hashes, kept=kept, sources=sources, links=links, deleted=[], ambiguous=False)
                    if label in pending:
                        current['ambiguous'] = True
                    pending[label] = current
                    continue
                if '仅记录不执行' in message:
                    label = re.match(r'^\[(.+)\]', message)
                    if label:
                        pending.pop(label[1], None)
                        summary['dry_runs'] += 1
                    continue
                if function == '_delete_file' and message.startswith('已删除文件: '):
                    deleted = message.removeprefix('已删除文件: ')
                    matches = [value for value in pending.values() if deleted in value['sources'] + value['links']]
                    if len(matches) == 1:
                        matches[0]['deleted'].append(deleted)
                    continue
                done = _DONE.match(message) if function in ('handle_media_deleted', '_handle_media_deleted') else None
                if not done:
                    continue
                label, *counts = done.groups()
                event = pending.pop(label, None)
                if event is None or event['ambiguous']:
                    summary['unmatched'] += 1
                    continue
                code = label if len(label) <= 64 and re.fullmatch(r'[A-Za-z0-9_-]+', label) else ''
                if not code:
                    codes = set(find_serial_numbers(' '.join(event['links'] + event['sources'])))
                    code = next(iter(codes)) if len(codes) == 1 else ''
                fingerprint = hashlib.sha256(f"{stamp}|{event['created_at'].isoformat()}|{event['message']}|{message}".encode()).hexdigest()[:32]
                counters = dict(zip(('torrents', 'files', 'sidecars', 'directories', 'errors'), map(int, counts)))
                source = event['sources'][0] if len(event['sources']) == 1 else ''
                provenance = dict(kind='log_import', file=event['file'], line=event['line'], finished_file=path.name,
                    finished_line=number, timezone=log_timezone, plan_message=event['message'], completion_message=message,
                    note='旧日志补录。路径与 Hash 来自当时计划；仅“已删除文件”日志可确认的路径列入实际删除结果，其他细节以完成汇总为准，无法恢复完整删除前快照。')
                events[fingerprint] = dict(id=fingerprint, code=code.upper(), title=label if not code else '',
                    source='log_import', action='media_delete', status='partial' if counters['errors'] else 'completed',
                    owner='legacy-log', link_path=event['links'][0] if event['links'] else '', source_path=source,
                    created_at=event['created_at'], finished_at=now, error='',
                    snapshot={'links': [dict(code=code, link_path=p, source_path=source if len(event['links']) == 1 else '') for p in event['links']],
                              'downloads': [dict(code=code, hash=h, save_path=source, site='', origin='log_plan') for h in event['hashes']],
                              'metadata': {}, 'plan': {'source_paths': event['sources'], 'link_paths': event['links'],
                                  'torrents_to_delete': event['hashes'], 'torrents_to_keep': event['kept']}, 'provenance': provenance},
                    result={'files_deleted': sorted(set(event['deleted'])), 'summary': counters,
                            'errors': [f"旧日志完成汇总记载 {counters['errors']} 项错误，逐项原因可能不完整"] if counters['errors'] else []})
    summary['unmatched'] += len(pending)
    return list(events.values()), summary


def import_log_history(paths=None, *, dry_run=True, log_timezone='Asia/Shanghai'):
    if paths is None:
        from app.utils.log import list_log_files, _log_path
        paths = [_log_path(row['name']) for row in list_log_files()]
    events, summary = parse_logs(paths, log_timezone)
    added, existing = [], 0
    with session_scope() as session:
        for event in events:
            # 稳定 ID 防止重复扫描；相同时间和路径的原生审计记录优先保留。
            hit = session.get(DeletionRecord, event['id']) or session.scalar(select(DeletionRecord).where(
                DeletionRecord.action == 'media_delete', DeletionRecord.code == event['code'],
                DeletionRecord.link_path == event['link_path'],
                DeletionRecord.created_at >= event['created_at'] - timedelta(seconds=2),
                DeletionRecord.created_at <= event['created_at'] + timedelta(seconds=2),
                DeletionRecord.source != 'log_import'))
            if hit:
                existing += 1
                continue
            added.append({'id': event['id'], 'code': event['code'], 'created_at': event['created_at'].isoformat() + 'Z'})
            if not dry_run:
                session.add(DeletionRecord(**event))
                session.flush()
    return dict(**summary, matched=len(events), added=len(added), existing=existing, dry_run=dry_run, items=added)
