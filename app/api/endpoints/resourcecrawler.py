"""Native resource catalog and resumable crawler controls."""
from fastapi import APIRouter, Depends
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
