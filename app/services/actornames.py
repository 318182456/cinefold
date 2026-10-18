"""刮削演员名使用本地已确认别名，不增加逐演员网络等待。"""
from contextlib import nullcontext
import re

from sqlalchemy import select
from app.database.models import Actor, ActorProfile
from app.database.session import session_scope
from app.utils.actornames import cast_names, names, native, normalize, presentation


def _key(value):
    return re.sub(r'\s+', '', normalize(value)).casefold()


def localize_casts(value, session=None):
    tokens = cast_names(value)
    if not tokens or all(native(token) for token in tokens):
        return ','.join(tokens)
    candidates = {}
    def add(primary, aliases, preferred=''):
        known = names([primary, *names(aliases)])
        label = presentation(primary, known, preferred)['display_name']
        if native(label):
            for token in known:
                if not native(token):
                    candidates.setdefault(_key(token), set()).add(label)
    with (nullcontext(session) if session is not None else session_scope()) as current:
        for name, aliases in current.execute(select(Actor.name, Actor.name_2)):
            add(name, aliases)
        for name, data in current.execute(select(ActorProfile.name, ActorProfile.data)):
            data = data or {}
            add(name, data.get('aliases', []), data.get('native_name', ''))
    resolved = []
    for token in tokens:
        labels = candidates.get(_key(token), set())
        label = next(iter(labels)) if len(labels) == 1 else token
        if label not in resolved:
            resolved.append(label)
    return ','.join(resolved)
