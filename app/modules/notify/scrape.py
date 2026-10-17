"""Rich import notifications use a snapshot of this scrape, never a later DB row."""
from html import escape
from io import BytesIO
from pathlib import Path
import re

from PIL import Image


def _short(value, limit):
    value = re.sub(r'\s+', ' ', str(value or '')).strip()
    encoded = value.encode('utf-16-le')
    return value if len(encoded)<=limit*2 else encoded[:(limit-1)*2].decode('utf-16-le', errors='ignore')+'…'


def snapshot(meta, items, links, elapsed):
    from app.modules.scrape.watermark import labels
    names = {'sub':'中字', 'crack':'破解', 'leak':'流出', 'uncensored':'无码', 'censored':'有码', '4K':'4K', '8K':'8K'}
    detected = list(dict.fromkeys(names[label] for item in items for label in labels(item)))
    for item in items:
        for resolution in ('1080P','720P'):
            if re.search(r'(?<![a-z0-9])'+resolution+r'(?![a-z0-9])',item.path.name,re.I) and resolution not in detected:
                detected.append(resolution)
    return {**{key:str(meta.get(key) or '') for key in
               ('title','cn_title','casts','publisher','producer','release_date','genres','outline','poster','banner')},
            'labels':detected, 'links':[str(link) for link in links], 'elapsed':elapsed}


def caption(code, title, count, detail):
    title = detail.get('cn_title') or detail.get('title') or title
    # A single preferred title avoids printing identical original/translated names.
    blocks = ['✅ 刮削完成', _short(code,64)+'\n'+_short(title,160)]
    facts = []
    for icon, value, limit in (('👤',detail.get('casts'),90),
            ('🏢',detail.get('publisher') or detail.get('producer'),70),('📅',detail.get('release_date'),32)):
        if value:
            facts.append(icon+' '+_short(value,limit))
    if detail.get('labels'):
        facts.append('🎞 文件标记：'+_short(' · '.join(detail['labels']),60))
    if facts:
        blocks.append('\n'.join(facts))
    tags = list(dict.fromkeys(tag.strip() for tag in re.split(r'[,，、|;；]+', detail.get('genres') or '')
        if tag.strip() and not re.fullmatch(r'(?:\d{3,4}p|[48]k|hd|fhd|uhd|高清|超高清)',tag.strip(),re.I)))
    if tags:
        blocks.append('🏷 '+_short('、'.join(tags),120))
    if detail.get('outline'):
        blocks.append('📝 '+_short(detail['outline'],250))
    footer = f'📁 {count} 个影片文件'
    if detail.get('elapsed') is not None:
        footer += f"\n⏱ 耗时 {max(0,round(float(detail['elapsed'])))}s"
    blocks.append(footer)
    plain = '\n\n'.join(blocks)
    html = escape(plain).replace('✅ 刮削完成','<b>✅ 刮削完成</b>',1)
    return plain, html


def local_photo(links):
    """Only read validated image sidecars of this task's committed video paths."""
    for raw in links:
        video = Path(raw)
        for kind in ('poster','thumb','fanart'):
            path = video.with_name(f'{video.stem}-{kind}.jpg')
            try:
                if any(parent.is_symlink() for parent in (path,*path.parents)) or not path.is_file() or not 0<path.stat().st_size<10*1024*1024:
                    continue
                data = path.read_bytes()
                with Image.open(BytesIO(data)) as image:
                    width,height = image.size
                    if width+height>10000 or max(width,height)/min(width,height)>20:
                        continue
                    image.verify()
                return data
            except (OSError,ValueError,ZeroDivisionError,Image.DecompressionBombError):
                continue
    return b''
