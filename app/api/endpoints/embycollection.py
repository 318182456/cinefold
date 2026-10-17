"""User-triggered whole-library collection jobs and optional previews."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.endpoints import get_current_user
from app.schemas.reponse import ResponseEntity

router = APIRouter(tags=['emby-collections'])


class SuggestRequest(BaseModel):
    mode: str = 'all'
    limit: int = Field(default=0, ge=0, le=500)


class AutomaticRequest(BaseModel):
    mode: str = 'all'


class PublishGroup(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    codes: list[str] = Field(min_length=2, max_length=500)


class PublishRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    groups: list[PublishGroup] = Field(min_length=1, max_length=40)


@router.post('/emby/collections/suggest')
def suggest_collections(body: SuggestRequest, current_user: str = Depends(get_current_user)):
    from app.services.embycollections import suggest
    try:
        return ResponseEntity.ok(suggest(body.mode, body.limit))
    except ValueError as exc:
        return ResponseEntity.fail(str(exc), code=400)


@router.get('/emby/collections/current')
def collection_state(current_user: str = Depends(get_current_user)):
    from app.services.embycollections import current
    return ResponseEntity.ok(current())


@router.get('/emby/collections/catalog')
def collection_catalog(current_user: str = Depends(get_current_user)):
    from app.services.embycollections import catalog
    return ResponseEntity.ok(catalog())


@router.post('/emby/collections/auto')
def automatic_collections(body: AutomaticRequest, current_user: str = Depends(get_current_user)):
    from app.services.embycollections import automatic
    try:
        return ResponseEntity.ok(automatic(body.mode))
    except ValueError as exc:
        return ResponseEntity.fail(str(exc), code=400)


@router.post('/emby/collections/resume')
def resume_collections(current_user: str = Depends(get_current_user)):
    from app.services.embycollections import resume
    try:
        return ResponseEntity.ok(resume())
    except ValueError as exc:
        return ResponseEntity.fail(str(exc), code=400)


@router.post('/emby/collections/publish')
def publish_collections(body: PublishRequest, current_user: str = Depends(get_current_user)):
    from app.services.embycollections import publish
    try:
        return ResponseEntity.ok(publish(body.token, [group.model_dump() for group in body.groups]))
    except ValueError as exc:
        return ResponseEntity.fail(str(exc), code=400)
