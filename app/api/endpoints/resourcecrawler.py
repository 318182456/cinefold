"""Native resource catalog and resumable crawler controls."""
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from app.api.endpoints import get_current_user
from app.schemas.reponse import ResponseEntity
from app.services import resourcecrawler, resourcecatalog

router=APIRouter(tags=['resource-crawler'])


class CrawlRequest(BaseModel):
    source:str
    sections:list[str] | None=None
    pages:int | None=Field(default=None,ge=1,le=100)
    limit:int | None=Field(default=None,ge=1,le=1000)
    resume:bool=False


@router.get('/resource-crawler')
def crawler_state(current_user:str=Depends(get_current_user)):
    return ResponseEntity.ok({'sources':resourcecrawler.sources()})


@router.post('/resource-crawler')
def crawler_start(body:CrawlRequest,current_user:str=Depends(get_current_user)):
    try:return ResponseEntity.ok(resourcecrawler.start(**body.model_dump()))
    except ValueError as exc:return ResponseEntity.fail(str(exc),code=400)


@router.post('/resource-crawler/{source}/stop')
def crawler_stop(source:str,current_user:str=Depends(get_current_user)):
    try:return ResponseEntity.ok(resourcecrawler.stop(source))
    except ValueError as exc:return ResponseEntity.fail(str(exc),code=400)


@router.get('/resources')
def resources(keyword:str='',source:str='',page:int=1,size:int=30,current_user:str=Depends(get_current_user)):
    return ResponseEntity.ok(resourcecatalog.browse(keyword,source,page,size))


@router.get('/resources/{key}/image')
def resource_image(key:str,index:int=0,current_user:str=Depends(get_current_user)):
    from app.database.models import ResourcePost
    from app.database.session import session_scope
    from app.services import resourceimages
    with session_scope() as db:
        row=db.get(ResourcePost,key)
        if row is None or index<0 or index>=len(row.previews):
            return Response(status_code=404)
        url,referer,source=row.previews[index],row.detail_url,row.source
    try:
        content,media_type=resourceimages.fetch(url,referer,source)
        return Response(content,media_type=media_type,headers={'Cache-Control':'private, max-age=3600','X-Content-Type-Options':'nosniff'})
    except TimeoutError:
        return Response(status_code=503)
    except Exception:
        return Response(status_code=404)
