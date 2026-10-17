from html import unescape
from io import BytesIO
import re
import threading

from PIL import Image

from app.core.config import Settings
from app.modules.notify import scrape as notification
from app.services import scrape, scrapefollowup
from app.utils.mediafile import parse
from tests.test_scrape_followup import background


def image():
    result=BytesIO()
    Image.new('RGB',(60,90),'blue').save(result,'JPEG')
    return result.getvalue()


def test_caption_prefers_one_title_escapes_html_and_fits_photo_limit():
    detail={key:'文本 & <测试> 😀'*100 for key in ('cn_title','title','casts','publisher','release_date','outline')}
    detail.update(labels=['有码','中字'],genres='纪录片,720p,4k,HD,粉丝感谢祭,纪录片',elapsed=42.2)
    plain,html=notification.caption('SONE-725','fallback',2,detail)
    assert len(plain.encode('utf-16-le'))//2<=1024
    assert unescape(re.sub(r'</?b>','',html))==plain
    assert '<测试>' not in html and '&lt;测试&gt;' in html
    assert '🏷 纪录片、粉丝感谢祭' in plain
    assert '720p' not in plain and '4k' not in plain
    assert '📁 2 个影片文件\n⏱ 耗时 42s' in plain
    assert 'fallback' not in plain
    plain,_=notification.caption('SONE-725','old',1,{'cn_title':'首选标题','title':'原始标题'})
    assert plain.count('首选标题')==1 and '原始标题' not in plain


def test_local_cover_missing_or_invalid_falls_through_to_other_sidecar(tmp_path):
    video=tmp_path/'SONE-725.mp4'
    poster=tmp_path/'SONE-725-poster.jpg'
    thumb=tmp_path/'SONE-725-thumb.jpg'
    poster.write_text('not an image')
    thumb.write_bytes(image())
    assert notification.local_photo([str(video)])==thumb.read_bytes()
    thumb.unlink()
    assert notification.local_photo([str(video)])==b''


def test_rich_notification_runs_after_commit_using_that_scrapes_metadata(background, monkeypatch, tmp_path):
    import app.modules.notify as notify
    from app.services import mediaduplicate
    entered=threading.Event()
    sent=[]
    class Notifier:
        def send_photo_bytes(self,photo,caption):
            sent.append((photo,caption))
            entered.set()
            return True
        def send_text_message(self,text):
            raise AssertionError('valid local artwork should use photo upload')
    monkeypatch.setattr(notify,'get_notifiers',lambda:[Notifier()])
    monkeypatch.setattr(mediaduplicate,'check_existing',lambda *args:({},[]))
    monkeypatch.setattr(scrape,'get_settings',lambda:Settings(scrape_notify=True,scrape_still_limit=0))
    monkeypatch.setattr(scrape,'load_group_meta',lambda *args,**kwargs:{'title':'原始标题','cn_title':'本次标题',
        'casts':'演员甲','publisher':'片商甲','release_date':'2025-05-23','outline':'本次简介'})
    monkeypatch.setattr(scrape,'_cover_bytes',lambda *args:b'')
    monkeypatch.setattr(scrape,'_poster_bytes',lambda *args:b'')
    target=tmp_path/'SONE-725-1080P.mp4'
    def commit(*args):
        result=args[-1]
        result.links.append(target)
        result.nfo_written=1
        target.with_name(target.stem+'-poster.jpg').write_bytes(image())
        return result,True
    monkeypatch.setattr(scrape,'_commit_group',commit)
    result=scrape.scrape_group('SONE-725',[parse(target)])
    assert result.ok and entered.wait(2)
    assert sent[0][0]==image()
    caption=sent[0][1]
    for text in ('本次标题','演员甲','片商甲','2025-05-23','本次简介','1080P','⏱ 耗时'):
        assert text in caption
    assert '原始标题' not in caption


def test_notification_payload_is_copied_before_background_execution(background, monkeypatch):
    _,release=background
    entered,finished=threading.Event(),threading.Event()
    captured=[]
    def run(*args):
        entered.set()
        assert release.wait(2)
        captured.append(args[-1])
        finished.set()
    monkeypatch.setattr(scrapefollowup,'_run',run)
    detail={'cn_title':'本次标题','links':['first.mp4']}
    assert scrapefollowup.schedule('SONE-725','title',1,notify=True,notification=detail)=='queued'
    assert entered.wait(1)
    detail['cn_title']='另一次标题'
    detail['links'].append('other.mp4')
    release.set()
    assert finished.wait(1)
    assert captured==[{'cn_title':'本次标题','links':['first.mp4']}]


def test_other_channels_use_plain_text_when_no_photo_available(monkeypatch):
    import app.modules.notify as notify
    texts=[]
    class Notifier:
        def send_text_message(self,text):
            texts.append(text)
            return True
    monkeypatch.setattr(notify,'get_notifiers',lambda:[Notifier()])
    scrapefollowup._notify('SONE-725','title < &',1)
    assert len(texts)==1 and 'title < &' in texts[0] and '&lt;' not in texts[0]
