from fastapi import APIRouter, Depends
from shared_lib.schemas.ingest import IngestBatch

from fastapi_service.core.auth import verify_token
from fastapi_service.core.response import success
from fastapi_service.services import ingest_service

router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post("/events", dependencies=[Depends(verify_token)])
async def ingest_events(batch: IngestBatch):
    """接收来自 ws_service 的批量事件上报。"""
    result = await ingest_service.process_batch(batch)
    return success(data=result.model_dump(), msg="ingested")
