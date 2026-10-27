"""Article's metadata feeds reuse native providers; no legacy DB or browser process."""
import json
from loguru import logger
from app.core.config import get_settings
from app.modules.ladysite.base import SiteClient
from app.modules.ladysite.avbase import NEXT_DATA_RE, html_to_code
from app.modules.ladysite.sources import get_source
from app.services.resourcecatalog import cache_actor


def parse_avbase(html):
    match=NEXT_DATA_RE.search(html or '')
    if not match:raise ValueError('Avbase 列表没有作品数据，可能仍在验证页')
    payload=json.loads(match[1]);props=(payload.get('props') or {}).get('pageProps') or {}
    if not isinstance(props.get('works'),list):raise ValueError('Avbase 列表数据格式不匹配')
    movies,actors,seen=[],[],set()
    for work in props['works'][:100]:
        work=dict(work)
        work['title']=work.get('title') or next((product.get('title') for product in work.get('products') or [] if product.get('title')), '')
        if not work.get('casts'):work['casts']=[{'actor':a} for a in work.get('actors') or []]
        data=json.dumps({'props':{'pageProps':{'work':work}}},ensure_ascii=False).replace('</','<\\/')
        info=html_to_code('<script id="__NEXT_DATA__" type="application/json">'+data+'</script>')
        if info and info.code not in seen:
            movies.append(info.to_dict());seen.add(info.code)
        actors.extend({'name':a.get('name'),'photo':a.get('image_url')} for a in work.get('actors') or [] if a.get('name'))
    if props['works'] and not movies:raise ValueError('Avbase 列表没有可识别的影片')
    return movies,actors


def run():
    from app import services
    from app.services.taskruns import progress
    from app.modules import ladysite
    settings=get_settings();saved=0;errors=[]
    client=SiteClient.from_source('avbase')
    if client:
        try:
            for page in range(1,min(settings.resource_crawl_pages,10)+1):
                if not (get_source('avbase') or {}).get('enabled'):break
                progress('crawl_resource_metadata',f'Avbase 最近上架第 {page} 页',current='avbase',processed=saved)
                movies,actors=parse_avbase(client.get('/works/recent',params={'page':page},timeout=40))
                saved+=services.cache_remote_codes(movies)
                for actor in actors:cache_actor(actor)
                progress('crawl_resource_metadata',f'Avbase 第 {page} 页已保存',current='avbase',processed=saved)
                if not movies:break
        except Exception as exc:
            errors.append('avbase:'+type(exc).__name__)
            logger.warning(f'Avbase 情报抓取未完成: {type(exc).__name__}')
    if (get_source('javdb') or {}).get('enabled'):
        progress('crawl_resource_metadata','读取 JavDB 榜单',current='javdb',processed=saved)
        try:
            codes=ladysite.get_rank_codes('daily',1)
            if not codes:raise RuntimeError('榜单请求失败或未返回有效番号')
            saved+=ladysite._store_codes(codes)
        except Exception as exc:errors.append('javdb:'+type(exc).__name__)
    if (get_source('javlibrary') or {}).get('enabled'):
        progress('crawl_resource_metadata','读取 JavLibrary 榜单与演员',current='javlibrary',processed=saved)
        try:
            from app.modules.ladysite.library import Library
            provider=Library()
            codes=provider.crawling_top20()
            actors=provider.crawling_top20_actor()
            saved+=ladysite._store_codes(codes)
            for actor in actors:saved+=cache_actor(actor.to_dict())
            if not codes or not actors:raise RuntimeError('榜单或演员请求未取得有效内容')
        except Exception as exc:errors.append('javlibrary:'+type(exc).__name__)
    progress('crawl_resource_metadata','部分来源失败，已写入的数据保留' if errors else '影片与演员情报抓取完成',processed=saved)
    if errors:raise RuntimeError(f'已处理 {saved} 条，失败来源: '+','.join(errors))
    return saved
