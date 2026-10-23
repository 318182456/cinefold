"""读取 Emby Playback Reporting 的历史；不导入本地观测表，避免重复累计。"""
from __future__ import annotations

import calendar
import math
import re
from datetime import datetime, timedelta, timezone

import httpx

from app.services.library import emby_link
from app.utils import find_serial_numbers


def plugin_report(settings, month, tz, user_id='', day='', page=1, size=30):
    year, number = map(int, month.split('-'))
    begin = datetime(year, number, 1, tzinfo=tz).astimezone(timezone.utc)
    end = (begin.astimezone(tz).replace(day=28) + timedelta(days=4)).replace(day=1).astimezone(timezone.utc)
    # 插件以 UTC 存储。julianday 同时兼容带 Z、偏移及旧版 SQLite 日期文本。
    # WHERE 中额外保留跨月开始、在本月结束的播放。
    query = ("SELECT rowid AS RecordId, DateCreated, UserId, ItemId, ItemName, ClientName, DeviceName, PlayDuration "
             "FROM PlaybackActivity WHERE ItemType IN ('Movie','Episode','Video') "
             f"AND julianday(DateCreated) < julianday('{end.isoformat()}') "
             f"AND julianday(DateCreated) + MAX(COALESCE(PlayDuration,0),0)/86400.0 >= julianday('{begin.isoformat()}') "
             "ORDER BY DateCreated DESC, rowid DESC LIMIT 20001")
    with httpx.Client(timeout=15, trust_env=False, headers={'X-Emby-Token': settings.emby_api_key}) as client:
        response = client.post(f"{settings.emby_url.rstrip('/')}/emby/user_usage_stats/submit_custom_query",
                               json={'CustomQueryString': query, 'ReplaceUserId': False})
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get('results'), list) or not isinstance(data.get('colums'), list):
            raise ValueError('插件返回格式无效')
        if data.get('message'):
            raise ValueError('插件查询失败，请检查插件版本及管理员权限')
        if len(data['results']) > 20000:
            raise ValueError('单月记录超过读取上限，未展示不完整的插件统计')
        columns = data['colums']
        if not {'RecordId', 'DateCreated', 'UserId', 'ItemId', 'PlayDuration'} <= set(columns):
            raise ValueError('插件缺少必要字段')
        try:
            users_response = client.get(f"{settings.emby_url.rstrip('/')}/emby/Users")
            users_response.raise_for_status()
            users = {str(u['Id']).replace('-', '').casefold(): u.get('Name') or str(u['Id']) for u in users_response.json()}
        except Exception:
            users = {}
    selected = (settings.emby_user_id or user_id).replace('-', '').casefold()
    days = {f'{month}-{i:02d}': {'date': f'{month}-{i:02d}', 'seconds': 0.0, 'plays': 0}
            for i in range(1, calendar.monthrange(year, number)[1] + 1)}
    records, observed_users, all_plays = [], {}, 0
    for values in data['results']:
        if not isinstance(values, list) or len(values) != len(columns):
            raise ValueError('插件记录格式无效')
        row = dict(zip(columns, values))
        uid = str(row['UserId'] or '')
        normalized = uid.replace('-', '').casefold()
        if selected and selected != normalized:
            continue
        try:
            text = re.sub(r'(\.\d{6})\d+', r'\1', str(row['DateCreated']))  # .NET ticks 有七位小数
            started = datetime.fromisoformat(text.replace('Z', '+00:00'))
            started = started.replace(tzinfo=timezone.utc) if started.tzinfo is None else started.astimezone(timezone.utc)
            seconds = float(row['PlayDuration'] or 0)
            if not math.isfinite(seconds) or seconds < 0 or seconds > 366 * 86400:
                raise ValueError
            ended = started + timedelta(seconds=seconds)
        except (ValueError, OverflowError, TypeError) as exc:
            raise ValueError('插件记录的时间或时长无效') from exc
        dates = set()
        if begin <= started < end:
            dates.add(started.astimezone(tz).date().isoformat())
        cursor, stop = max(started, begin), min(ended, end)
        while cursor < stop:
            local = cursor.astimezone(tz)
            boundary = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            boundary = min(boundary, stop)
            date = local.date().isoformat()
            days[date]['seconds'] += (boundary - cursor).total_seconds()
            dates.add(date)
            cursor = boundary
        if not dates:
            continue
        all_plays += 1
        observed_users[uid] = users.get(normalized, uid)
        for date in dates:
            days[date]['plays'] += 1
        if day and day not in dates:
            continue
        title = str(row.get('ItemName') or '')
        codes = find_serial_numbers(title)
        records.append(dict(id=f"plugin-{row['RecordId']}", user_id=uid, user_name=users.get(normalized, uid),
                            item_id=str(row['ItemId'] or ''), title=title, code=codes[0] if len(set(codes)) == 1 else '',
                            device=row.get('DeviceName') or '', client=row.get('ClientName') or '',
                            started_at=started.isoformat(), last_seen=ended.isoformat(), ended_at=ended.isoformat(),
                            watched_seconds=seconds, active=False, paused=False, emby_url=emby_link(str(row['ItemId'] or ''))))
    for cell in days.values():
        cell['seconds'] = round(cell['seconds'])
    # 日历保持整月汇总，明细单独按日期过滤与分页。
    return dict(enabled=True, source='playback_reporting', source_error='', timezone=str(tz), month=month,
                days=list(days.values()), plays=all_plays, seconds=sum(d['seconds'] for d in days.values()),
                active_days=sum(d['plays'] > 0 for d in days.values()), records=records[(page-1)*size:page*size],
                total=len(records), page=page, size=size,
                users=[dict(user_id=uid, name=name) for uid, name in observed_users.items()],
                note='来自 Emby Playback Reporting 历史，不与本地观测重复累计。时长采用插件记录；跨日分配按开始时间与累计时长估算，插件未提供暂停发生的具体时间。')
