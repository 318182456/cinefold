from contextlib import nullcontext
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import resourceimages as images, resourcecatalog as catalog
from tests.test_resource_crawler import native, post
from tests.test_scrape_reliability import db


def test_scoped_image_endpoint_uses_saved_url_and_referer(native, monkeypatch):
    from app.api.endpoints.resourcecrawler import router
    from app.api.endpoints import get_current_user
    key=catalog.put('sehuatang',1,'37',post())
    seen=[]
    monkeypatch.setattr(images,'fetch',lambda *args:(seen.append(args) or (b'jpeg','image/jpeg')))
    app=FastAPI();app.include_router(router)
    with TestClient(app) as api:
        assert api.get(f'/resources/{key}/image').status_code==401
        app.dependency_overrides[get_current_user]=lambda:'test'
        response=api.get(f'/resources/{key}/image?index=0')
        assert response.content==b'jpeg' and response.headers['content-type']=='image/jpeg'
        assert response.headers['cache-control'].startswith('private')
        assert seen==[('https://images/1.jpg','https://forum/thread-1.html','sehuatang')]
        assert api.get(f'/resources/{key}/image?index=-1').status_code==404
        assert api.get(f'/resources/{key}/image?index=1').status_code==404
        assert api.get('/resources/unknown/image').status_code==404
        assert len(seen)==1


def test_image_fetch_referer_redirect_guard_and_bounded_content(monkeypatch):
    buffer=BytesIO();Image.new('RGB',(16,16)).save(buffer,format='JPEG');jpeg=buffer.getvalue()
    monkeypatch.setattr(images,'get_settings',lambda:SimpleNamespace(proxy=''))
    monkeypatch.setattr(images,'get_source',lambda source:{})
    monkeypatch.setattr(images,'proxy_lease',lambda *args:nullcontext(None))
    monkeypatch.setattr(images.socket,'getaddrinfo',lambda host,*a,**k:[(2,1,6,'',(host if host=='127.0.0.1' else '93.184.216.34',443))])
    seen=[]
    def handler(request):
        seen.append(request)
        if request.url.path=='/private':return httpx.Response(302,headers={'Location':'http://127.0.0.1/secret'})
        if request.url.path=='/html':return httpx.Response(200,text='<html>challenge</html>')
        return httpx.Response(200,content=jpeg)
    real_client=httpx.Client
    monkeypatch.setattr(images.httpx,'Client',lambda **kwargs:real_client(transport=httpx.MockTransport(handler),**kwargs))
    assert images.fetch('https://images.test/photo','https://forum.test/thread-1.html','sehuatang')==(jpeg,'image/jpeg')
    assert seen[0].headers['referer']=='https://forum.test/thread-1.html'
    with pytest.raises(ValueError,match='内网'):images.fetch('https://images.test/private','https://forum.test/','sehuatang')
    assert all(request.url.host!='127.0.0.1' for request in seen)
    with pytest.raises(Exception):images.fetch('https://images.test/html','https://forum.test/','sehuatang')
    monkeypatch.setattr(images,'MAX_BYTES',10)
    with pytest.raises(ValueError,match='8 MiB'):images.fetch('https://images.test/large','https://forum.test/','sehuatang')
