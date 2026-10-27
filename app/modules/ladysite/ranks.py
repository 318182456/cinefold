"""Shared ranking identities for fetching, subscriptions, UI and assistant tools."""
from typing import Literal

RankType = Literal['daily', 'weekly', 'monthly', 'uncensored_daily', 'uncensored_weekly', 'uncensored_monthly', 'fc2_daily', 'fc2_weekly', 'fc2_monthly', 'library_mostwanted', 'library_bestrated']
RANKS = {}
for section, category in [('censored', '有码'), ('uncensored', '无码'), ('fc2', 'FC2')]:
    for period, label in [('daily', '日榜'), ('weekly', '周榜'), ('monthly', '月榜')]:
        key = period if section == 'censored' else section+'_'+period
        RANKS[key] = dict(value=key, label=f'JavDB {category}{label}', source='javdb', period=period, section=section, requires_api=section=='fc2')
for rank, label in [('mostwanted', '最想看'), ('bestrated', '高评分')]:
    key='library_'+rank
    RANKS[key]=dict(value=key,label='JavLibrary '+label,source='javlibrary',rank=rank)


def spec(kind):
    key=(kind or 'daily').strip().lower()
    if key not in RANKS:raise ValueError('未知榜单')
    return RANKS[key]
