"""Forum parsers adapted to cinefold's shared HTTP/proxy/bypass client."""
import base64
import re
import time
from http.cookies import SimpleCookie
from html import unescape
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse

import httpx
from pyquery import PyQuery
from app.core.config import get_settings
from app.modules.ladysite.base import SiteClient
from app.modules.ladysite.transport import FetchBudget
from app.modules.ptsite import extract_torrent_size
from app.services.mihomo import proxy_lease

SOURCES = {
    'sehuatang': {'name': '色花堂', 'sections': {'2':'国产原创','36':'亚洲无码','38':'欧美无码','37':'亚洲有码',
        '103':'中文字幕','104':'素人有码','151':'4K','152':'韩国主播','160':'VR','39':'动漫','107':'写真','95':'综合','142':'转载'}},
    'x1080x': {'name': 'X1080X', 'sections': {'5212':'国产品牌','5206':'有码 HD','5207':'无码 HD','5208':'FC2',
        '5216':'MGS','5479':'中文字幕','5213':'探花','5217':'主播'}},
}


def magnets(text):
    found, seen = [], set()
    for value in re.findall(r'magnet:\?[^\s<>"\'\[\]]+', unescape(text or ''), re.I):
        pairs=parse_qsl(urlparse(value).query)
        info_hash=next((v[9:].lower() for k,v in pairs if k.lower()=='xt' and v.lower().startswith('urn:btih:')), '')
        if len(info_hash) == 32:
            try:
                info_hash = base64.b32decode(info_hash.upper()).hex()
            except ValueError:
                continue
        if not re.fullmatch('[a-f0-9]{40}', info_hash or '') or info_hash in seen:
            continue
        seen.add(info_hash)
        extra=urlencode([(k,v) for k,v in pairs if k.lower()!='xt'])
        found.append('magnet:?xt=urn:btih:'+info_hash+('&'+extra if extra else ''))
    return found


def parse_list(source, html):
    doc = PyQuery(html)
    if source == 'sehuatang':
        if not doc('#threadlist, #threadlisttableid, tbody[id^="normalthread_"]'):
            raise ValueError('未取得论坛列表，可能仍是验证页')
        ids = [int(node.attr('id').split('_')[-1]) for node in doc('tbody[id^="normalthread_"]').items()
               if node.attr('id').split('_')[-1].isdigit()]
    else:
        if not doc('#content'):
            raise ValueError('未取得论坛归档列表，可能仍是验证页')
        ids = []
        for link in doc('#content a[href]').items():
            value = parse_qs(urlparse(link.attr('href')).query).get('tid', [''])[0]
            if value.isdigit():
                ids.append(int(value))
    return list(dict.fromkeys(value for value in ids if 0 < value < 2**63))


def parse_detail(source, html, url):
    doc = PyQuery(html)
    if source == 'sehuatang':
        title = doc('#thread_subject').text().strip()
        content = doc('[id^="postmessage_"]').eq(0)
        date = doc('em[id^="authorposton"] span[title]').eq(0).attr('title') or doc('em[id^="authorposton"]').eq(0).text()
        category = doc('h1.ts a').eq(0).text().strip('[] ')
    else:
        content = doc('#content')
        title = (doc('#nav').text() or doc('h1').text()).split('›')[-1].strip()
        date = doc('p.author').text()
        category = ''
    if not title or not content:
        raise ValueError('未取得帖子正文，可能仍是登录或验证页')
    text = (content.html() or '')+' '+content.text()
    links = magnets(text)
    ed2k = list(dict.fromkeys(re.findall(r'ed2k://\|file\|[^|]+\|\d+\|[a-f0-9]{32}\|/', unescape(text), re.I)))
    images = [urljoin(url, node.attr('file') or node.attr('data-src') or node.attr('src') or '')
              for node in content('img[file],img[data-src],img[src]').items()]
    images += re.findall(r'\[img\](https?://[^\[\]\s]+)\[/img\]', unescape(text), re.I)
    attachments = [urljoin(url,node.attr('href')) for node in content('a[href]').items()
                   if '.torrent' in node.text().lower() or '.txt' in node.text().lower()]
    # Discuz attachments often live outside postmessage, in the attachment block.
    if source == 'sehuatang':
        attachments += [urljoin(url,node.attr('href')) for node in doc('.attnm a[href], .attl a[href]').items()
                        if '.torrent' in node.text().lower() or '.txt' in node.text().lower()]
    published = re.search(r'\d{4}-\d{2}-\d{2}', date or '')
    size = re.search(r'\d+(?:\.\d+)?\s*(?:TB|GB|MB|T|G|M)(?![a-z])',content.text(),re.I)
    return dict(title=re.sub(r'^\[[^\]]+\]\s*','',title), category=category, detail_url=url,
                published=published[0] if published else '', size_mb=extract_torrent_size(size[0]) if size else 0,
                magnets=links, ed2k=ed2k, previews=list(dict.fromkeys(u for u in images if u.startswith(('http://','https://')) and 'static/image' not in u))[:40],
                attachments=list(dict.fromkeys(attachments))[:10])


class ForumCrawler:
    def __init__(self, source):
        if source not in SOURCES:
            raise ValueError('未知资源爬虫')
        self.source = source
        self.client = SiteClient.from_source(source)
        if self.client is None:
            raise ValueError('请先在数据源设置启用资源爬虫并配置地址')

    def get(self, path, params=None):
        with FetchBudget(time.perf_counter()+90).activate():
            return self._get(path,params)

    def _get(self, path, params=None):
        html = self._fetch(path, params)
        if self.source == 'sehuatang':
            match = re.search(r'''safeid\s*=\s*['"]([a-zA-Z0-9_-]{1,128})['"]''', html)
            if match and not PyQuery(html)('#thread_subject, #threadlist, #threadlisttableid'):
                self.client.cookie = re.sub(r'(?:^|;)\s*_safe=[^;]*', '', self.client.cookie).strip('; ')+'; _safe='+match[1]
                html = self._fetch(path, params)
        return html

    def _fetch(self, path, params):
        for attempt in range(2):
            headers={'User-Agent':self.user_agent} if getattr(self,'user_agent','') else None
            html=self.client.get(path,params=params,timeout=40,headers=headers,
                                 bypass_first=True if attempt else None)
            if not html:raise RuntimeError('网页请求失败或过盾未完成')
            self._adopt(html)
            title=PyQuery(html)('title').text().lower()
            if not any(mark in title for mark in ('just a moment','attention required','checking your browser')):
                return html
            self.client.bypass_first=True
        raise RuntimeError('论坛仍在 Cloudflare 验证页')

    def _adopt(self, html):
        cookies=SimpleCookie()
        try:cookies.load(self.client.cookie)
        except Exception:pass
        host=urlparse(self.client.host).hostname or ''
        for cookie in getattr(html,'cookies',[]):
            domain=str(cookie.get('domain') or host).lstrip('.')
            if (host==domain or host.endswith('.'+domain)) and not re.search('[\r\n]',str(cookie.get('value',''))):
                cookies[cookie['name']]=cookie.get('value','')
        self.client.cookie='; '.join(f'{name}={value.value}' for name,value in cookies.items())
        self.attachment_proxy=getattr(html,'proxy',None)
        self.user_agent=getattr(html,'user_agent','') or getattr(self,'user_agent','')

    def listing(self, section, page):
        if self.source == 'sehuatang':
            path = f'/forum-{section}-{page}.html'
            params = None
            if section == '95':
                path, params = '/forum.php', dict(mod='forumdisplay', fid=95, filter='typeid', typeid=716, page=page)
        else:
            path, params = '/forum.php', dict(mod='forumdisplay', fid=244, archiver=1, page=page, filter='typeid', typeid=section)
        return parse_list(self.source, self.get(path, params))

    def binary(self, url, referer):
        if urlparse(url).hostname != urlparse(self.client.host).hostname:
            raise ValueError('不下载帖子指向站外的附件')
        settings = get_settings()
        self.client._throttle()
        explicit=getattr(self,'attachment_proxy',None) or self.client.proxy
        headers=self.client.headers({'Referer':referer})
        if getattr(self,'user_agent',''):headers['User-Agent']=self.user_agent
        with proxy_lease(settings.proxy or None, explicit, self.source, url) as proxy, httpx.Client(
            proxy=proxy, timeout=30, verify=False, trust_env=False, follow_redirects=False) as client:
            with client.stream('GET', url, headers=headers) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data)>20*1024**2:
                        raise ValueError('附件超过 20 MiB')
                return bytes(data)

    def detail(self, tid):
        path = f'/thread-{tid}-1-2.html' if self.source == 'sehuatang' else f'/forum.php?mod=viewthread&tid={tid}&archiver=1'
        url = urljoin(self.client.host+'/',path)
        row = parse_detail(self.source, self.get(path), url)
        if not row['magnets']:
            for attachment in row.pop('attachments'):
                data = self.binary(attachment,url)
                if not data:raise ValueError('附件内容为空')
                if data.startswith(b'd'):
                    from torrentool.api import Torrent
                    info_hash = Torrent.from_string(data).info_hash
                    row['magnets'].extend(magnets('magnet:?xt=urn:btih:'+info_hash))
                else:
                    text = data.decode('utf-8',errors='replace')
                    if re.search(r'(?i)<(?:!doctype|html|title)\b',text[:1000]):
                        raise RuntimeError('附件返回登录或验证页')
                    row['magnets'].extend(magnets(text))
                    row['ed2k'].extend(re.findall(r'ed2k://\|file\|[^|]+\|\d+\|[a-f0-9]{32}\|/',text,re.I))
        row.pop('attachments',None)
        row['magnets'] = magnets(' '.join(row['magnets']))
        return row
