"""Emby 播放观察：轮询会话与播放 Webhook 共用，既不播放也不修改 Emby。"""
from __future__ import annotations

import calendar
import hashlib
import math
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from loguru import logger
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import PlaybackSession, PlaybackSpan
from app.database.session import session_scope
from app.services.library import emby_link
from app.utils import find_serial_numbers

_LOCK = threading.Lock()
MAX_GAP = 150  # 正常每分钟观测；服务断开期间不补算未知时间。
EVENTS = {"playback.start", "playback.stop", "playback.pause", "playback.unpause", "playback.progress"}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _server(settings):
    return hashlib.sha256(settings.emby_url.rstrip('/').encode()).hexdigest()


def _position(state):
    try:
        value = float(state['PositionTicks']) / 10_000_000
        return value if math.isfinite(value) and value >= 0 else None
    except (KeyError, TypeError, ValueError):
        return None


def _snapshot(data, event=""):
    item = data.get('NowPlayingItem') or data.get('Item') or {}
    user = data.get('User') or {}
    device = data.get('Device') or {}
    session = data.get('Session') or {}
    if not isinstance(session, dict):
        return None
    state = data.get('PlayState') or data.get('PlaybackInfo') or session.get('PlayState') or {}
    if not all(isinstance(x, dict) for x in (item, user, device, session, state)):
        return None
    if item.get('Type') not in ('Movie', 'Episode', 'Video') or not item.get('Id'):
        return None
    user_id = str(data.get('UserId') or user.get('Id') or session.get('UserId') or '')
    # 使用设备标识，能把轮询与 Webhook 的两种 SessionId 格式归到同一次播放。
    identity = str(data.get('DeviceId') or device.get('Id') or session.get('DeviceId') or data.get('Id') or session.get('Id') or '')
    if not user_id or not identity:
        return None
    codes = find_serial_numbers(f"{item.get('Path') or ''} {item.get('Name') or ''}")
    return dict(session_key=hashlib.sha256(f'{user_id}:{identity}'.encode()).hexdigest(), user_id=user_id,
                user_name=str(data.get('UserName') or user.get('Name') or ''), item_id=str(item['Id']),
                code=codes[0] if len(set(codes)) == 1 else '', title=str(item.get('Name') or ''),
                device=str(data.get('DeviceName') or device.get('Name') or session.get('DeviceName') or ''),
                client=str(data.get('Client') or device.get('AppName') or session.get('Client') or ''),
                position_seconds=_position(state), paused=event == 'playback.pause' or bool(state.get('IsPaused', False)))


def _observe(session, server, snapshot, now, event=''):
    row = session.scalar(select(PlaybackSession).where(PlaybackSession.server == server,
        PlaybackSession.session_key == snapshot['session_key'], PlaybackSession.active.is_(True)).order_by(PlaybackSession.id.desc()))
    if row and row.item_id != snapshot['item_id'] and event == 'playback.stop':
        return None
    if row and (row.item_id != snapshot['item_id'] or (now - row.last_seen).total_seconds() > MAX_GAP):
        row.active = False
        row.ended_at = row.last_seen
        row = None
    if event == 'playback.stop' and row is None:
        # 重复停止事件或从未观测过的播放，不凭空补一段完整历史。
        return None
    if row is None:
        row = PlaybackSession(server=server, started_at=now, last_seen=now, watched_seconds=0, active=True, **snapshot)
        session.add(row)
        session.flush()
        return row
    elapsed = (now - row.last_seen).total_seconds()
    if elapsed <= 0:
        return row
    position = snapshot['position_seconds']
    moving = position is None or row.position_seconds is None or position != row.position_seconds
    if not row.paused and moving and elapsed <= MAX_GAP:
        span = session.scalar(select(PlaybackSpan).where(PlaybackSpan.playback_id == row.id, PlaybackSpan.ended_at == row.last_seen).order_by(PlaybackSpan.id.desc()))
        if span:
            span.ended_at = now
        else:
            session.add(PlaybackSpan(playback_id=row.id, started_at=row.last_seen, ended_at=now))
        row.watched_seconds += elapsed
    row.last_seen = now
    row.position_seconds = position
    row.paused = snapshot['paused']
    if event == 'playback.stop':
        row.active = False
        row.ended_at = now
    return row


def poll() -> int:
    settings = get_settings()
    if not settings.playback_tracking_enabled or not settings.emby_url or not settings.emby_api_key:
        return 0
    if not _LOCK.acquire(blocking=False):
        return 0
    try:
        try:
            with httpx.Client(timeout=15, trust_env=False) as client:
                response = client.get(f"{settings.emby_url.rstrip('/')}/emby/Sessions", headers={'X-Emby-Token': settings.emby_api_key})
                response.raise_for_status()
                data = response.json()
            if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
                raise ValueError('会话返回格式无效')
        except Exception as exc:
            # 不输出带认证信息的请求 URL，也不把拉取失败当作播放停止。
            logger.warning(f'[播放统计] 读取 Emby 会话失败: {type(exc).__name__}')
            return 0
        now, server = _now(), _server(settings)
        snapshots = [s for raw in data if (s := _snapshot(raw)) and (not settings.emby_user_id or s['user_id'] == settings.emby_user_id)]
        with session_scope() as session:
            seen = set()
            for snapshot in snapshots:
                _observe(session, server, snapshot, now)
                seen.add(snapshot['session_key'])
            for row in session.scalars(select(PlaybackSession).where(PlaybackSession.server == server, PlaybackSession.active.is_(True))):
                if row.session_key not in seen:
                    row.active = False
                    row.ended_at = row.last_seen  # 消失前的最后一分钟未知，不补算。
        return len(snapshots)
    finally:
        _LOCK.release()


def webhook(data: dict) -> dict:
    settings = get_settings()
    if not settings.playback_tracking_enabled:
        return {'ignored': True, 'reason': '播放统计未启用'}
    event = str(data.get('Event') or data.get('event') or data.get('NotificationType') or '').lower().replace('_', '.')
    snapshot = _snapshot(data, event)
    if event not in EVENTS or not snapshot or settings.emby_user_id and snapshot['user_id'] != settings.emby_user_id:
        return {'ignored': True, 'reason': '未匹配到所选用户的有效播放事件'}
    with _LOCK, session_scope() as session:
        row = _observe(session, _server(settings), snapshot, _now(), event)
        return {'recorded': row is not None}


def report(month: str, user_id: str = '', day: str = '', page: int = 1, size: int = 30) -> dict:
    settings = get_settings()
    try:
        tz = ZoneInfo(settings.playback_timezone)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    year, number = map(int, month.split('-'))
    begin = datetime(year, number, 1, tzinfo=tz)
    end = (begin.replace(day=28) + timedelta(days=4)).replace(day=1)
    start_utc, end_utc = (x.astimezone(timezone.utc).replace(tzinfo=None) for x in (begin, end))
    now = _now()
    days = {f'{month}-{i:02d}': {'date': f'{month}-{i:02d}', 'seconds': 0.0, 'plays': 0} for i in range(1, calendar.monthrange(year, number)[1] + 1)}
    memberships = defaultdict(set)
    with session_scope() as session:
        conditions = [PlaybackSession.server == _server(settings)]
        selected_user = user_id or settings.emby_user_id
        if selected_user:
            conditions.append(PlaybackSession.user_id == selected_user)
        rows = session.scalars(select(PlaybackSession).where(*conditions, PlaybackSession.started_at < end_utc,
            PlaybackSession.last_seen >= start_utc).order_by(PlaybackSession.started_at.desc())).all()
        for row in rows:
            date = row.started_at.replace(tzinfo=timezone.utc).astimezone(tz).date().isoformat()
            if date in days:
                memberships[date].add(row.id)
        spans = session.execute(select(PlaybackSpan, PlaybackSession.id).join(PlaybackSession, PlaybackSession.id == PlaybackSpan.playback_id).where(*conditions, PlaybackSpan.started_at < end_utc, PlaybackSpan.ended_at > start_utc))
        for span, row_id in spans:
            cursor = max(span.started_at, start_utc).replace(tzinfo=timezone.utc).astimezone(tz)
            stop = min(span.ended_at, end_utc).replace(tzinfo=timezone.utc).astimezone(tz)
            while cursor < stop:
                boundary = min((cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0), stop)
                date = cursor.date().isoformat()
                days[date]['seconds'] += (boundary.astimezone(timezone.utc) - cursor.astimezone(timezone.utc)).total_seconds()
                memberships[date].add(row_id)
                cursor = boundary
        for date in days:
            days[date]['seconds'] = round(days[date]['seconds'])
            days[date]['plays'] = len(memberships[date])
        records = []
        visible = [r for r in rows if not day or r.id in memberships[day]]
        for row in visible[(page - 1) * size: page * size]:
            record = row.to_dict()
            for key in ('started_at', 'last_seen', 'ended_at'):
                value = getattr(row, key)
                record[key] = value.replace(tzinfo=timezone.utc).isoformat() if value else None
            record['active'] = row.active and (now - row.last_seen).total_seconds() <= MAX_GAP
            record['emby_url'] = emby_link(row.item_id)
            records.append(record)
        users = [dict(user_id=uid, name=name or uid) for uid, name in session.execute(select(PlaybackSession.user_id, PlaybackSession.user_name).where(PlaybackSession.server == _server(settings)).distinct()) if not settings.emby_user_id or uid == settings.emby_user_id]
    return {'enabled': settings.playback_tracking_enabled, 'timezone': str(tz), 'month': month, 'days': list(days.values()),
            'plays': len(set().union(*memberships.values())) if memberships else 0, 'seconds': sum(d['seconds'] for d in days.values()), 'active_days': sum(d['plays'] > 0 for d in days.values()),
            'records': records, 'total': len(visible), 'page': page, 'size': size, 'users': users,
            'note': '从启用后开始记录。时长为观测到的播放活动估算，排除已观测的暂停与断连空档；跳转不按进度差累加。短于轮询间隔的播放可能漏记，Webhooks 可补充起止事件。'}
