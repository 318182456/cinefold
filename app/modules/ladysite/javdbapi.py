"""Optional legacy JavDB app API, adapted from the supplied article source."""
import json
import time
from hashlib import md5
from urllib.parse import quote
from app.modules.ladysite.base import SiteClient, CodeInfo, join_list
from app.modules.ladysite.sources import get_source
from app.modules.ladysite.identity import same_code
from app.utils import get_true_code

def get_headers():
    timestamp = int(time.time())
    secret = '71cf27bb3c0bcdf207b64abecddc970098c7421ee7203b9cdae54478478a199e7d5a6e1a57691123c1a931c057842fb73ba3b3c83bcd69c17ccf174081e3d8aa'
    md5_str = md5(f'{timestamp}{secret}'.encode()).hexdigest()
    jdsignature = f'{timestamp}.lpw6vgqzsp.{md5_str}'
    return {
        'User-Agent': 'Dart/3.5 (dart:io)',
        'accept-language': 'zh-TW',
        'jdsignature': jdsignature,
    }

class JavdbAPI:
    def __init__(self):
        source=get_source('javdb') or {}
        options=source.get('options') or {}
        self.enabled=bool(source.get('enabled') and options.get('api_enabled'))
        self.host=(options.get('api_host') or 'https://api.btyjscl.com').rstrip('/')
        self.client=SiteClient(self.host,interval=source.get('interval') or 2)
        self.client.proxy=source.get('proxy') or None
        self.client.source_key='javdb'

    def request(self,path,params=None):
        if not self.enabled:return {}
        raw=self.client.get(path,params=params,headers=get_headers(),allow_bypass=False,timeout=20)
        if not raw:return {}
        payload=json.loads(raw)
        return payload.get('data') or {}

    def rank(self,period,section):
        kind={'censored':0,'uncensored':1,'fc2':3}[section]
        return [convert_movie(row) for row in self.request('/api/v1/rankings',{'type':kind,'period':period}).get('movies') or []]

    def detail(self,code):
        rows=self.request('/api/v2/search',{'q':code}).get('movies') or []
        selected=next((row for row in rows if same_code(row.get('number',''),code)),None)
        if not selected or not selected.get('id'):return None
        row=self.request('/api/v4/movies/'+quote(str(selected['id']),safe='')).get('movie') or {}
        return convert_movie(row) if same_code(row.get('number',''),code) else None


def convert_movie(row):
    code=get_true_code(row.get('number') or '')
    if not code:return None
    cover=row.get('cover_url') or ''
    return CodeInfo(code=code,title=row.get('title') or '',banner=cover,poster=cover,
        release_date=row.get('release_date') or '',duration=str(row.get('duration') or ''),
        producer=row.get('maker_name') or '',publisher=row.get('publisher_name') or '',series=row.get('series_name') or '',
        casts=join_list(a.get('name') for a in row.get('actors') or [] if a.get('gender')==0),
        genres=join_list(t.get('name') for t in row.get('tags') or []),
        still_photo=join_list(i.get('large_url') for i in row.get('preview_images') or []))
