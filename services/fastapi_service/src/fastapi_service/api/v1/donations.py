from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from fastapi_service.core.auth import verify_token
from fastapi_service.core.errors import ErrorCode
from fastapi_service.core.response import error, paginated, success
from fastapi_service.services import donation_service

from ..deps import Pagination, get_large_pagination

router = APIRouter()


class DonationCreate(BaseModel):
    donor_name: str | None = None
    amount: Decimal = Field(..., ge=0)
    currency: Literal["CNY", "USD", "EUR", "GBP", "JPY", "HKD", "TWD", "AUD", "CAD", "SGD"] = "CNY"
    message: str | None = None


@router.post("/donations", dependencies=[Depends(verify_token)])
async def create_donation(payload: DonationCreate):
    donation, created = await donation_service.create_or_update_donation(
        donor_name=payload.donor_name,
        amount=payload.amount,
        currency=payload.currency,
        message=payload.message,
    )
    return success(data=donation, msg="Donation created" if created else "Donation updated")


@router.get("/donations")
async def list_donations(pg: Pagination = Depends(get_large_pagination)):
    items, total = await donation_service.list_donations(page_size=pg.page_size, offset=pg.offset)
    return paginated(data=items, total=total, msg="Donations retrieved")


@router.delete("/donations/{donation_id}", dependencies=[Depends(verify_token)])
async def delete_donation(donation_id: int):
    deleted = await donation_service.delete_donation(donation_id)
    if not deleted:
        return error(ErrorCode.DONATION_NOT_FOUND, msg="Donation not found")
    return success(msg="Donation deleted")
