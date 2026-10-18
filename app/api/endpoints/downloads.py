from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.api.endpoints import get_current_user
from app.schemas.reponse import ResponseEntity
from app.services import downloads

router = APIRouter(tags=['downloads'], dependencies=[Depends(get_current_user)])


@router.get('/downloads')
def live(client: str = '', state: Literal['', 'downloading', 'seeding', 'paused', 'checking', 'error'] = '',
         keyword: str = '', code_status: int | None = Query(None, ge=0, le=5),
         page: int = Query(1, ge=1), size: int = Query(30, ge=1, le=100)):
    return ResponseEntity.ok(downloads.live(client=client, state=state, keyword=keyword, code_status=code_status, page=page, size=size))


@router.get('/downloads/history')
def history(client: str = '', keyword: str = '', code_status: int | None = Query(None, ge=0, le=5),
            page: int = Query(1, ge=1), size: int = Query(30, ge=1, le=100)):
    return ResponseEntity.ok(downloads.history(client=client, keyword=keyword, code_status=code_status, page=page, size=size))


@router.get('/downloads/transfer')
def transfer():
    from app.services.seedtransfer import is_available
    available, reason = is_available()
    return ResponseEntity.ok({'current': downloads.transfer_current(), 'history': downloads.transfer_history(),
                              'available': available, 'reason': reason})


class TransferRequest(BaseModel):
    hashes: list[str] = Field(default_factory=list, max_length=100)
    delete_source: bool = False
    resume: bool = False

    @field_validator('hashes')
    @classmethod
    def check_hashes(cls, value):
        import re
        if any(not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', h) for h in value):
            raise ValueError('无效的种子 hash')
        return value


@router.post('/downloads/transfer')
def start_transfer(body: TransferRequest):
    try:
        return ResponseEntity.ok(downloads.start_transfer(body.hashes, delete_source=body.delete_source, resume=body.resume))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
