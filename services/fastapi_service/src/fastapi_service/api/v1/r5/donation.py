from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from shared_lib.models import Donation

from .api import verify_token


class DonationCreate(BaseModel):
    donor_name: str | None = None
    amount: Decimal = Field(..., gte=0)
    currency: Literal["CNY", "USD", "EUR", "GBP", "JPY", "HKD", "TWD", "AUD", "CAD", "SGD"] = "CNY"
    message: str | None = None


router = APIRouter()


@router.post("/donations", dependencies=[Depends(verify_token)])
async def create_donation(payload: DonationCreate):
    # 重复donor_name则更新金额和消息
    donation, created = await Donation.update_or_create(
        donor_name=payload.donor_name,
        defaults=dict(
            amount=payload.amount,
            currency=payload.currency,
            message=payload.message,
        ),
    )
    return {"code": "0000", "data": donation, "msg": "Donation created" if created else "Donation updated"}


@router.get("/donations")
async def list_donations(
    page_no: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(1000, ge=1, le=1000, description="Items per page"),
):
    total = await Donation.all().count()
    offset = (page_no - 1) * page_size
    items = await Donation.all().order_by("-created_at").limit(page_size).offset(offset).values()
    return {"code": "0000", "data": items, "total": total, "msg": "Donations retrieved"}


@router.delete("/donations/{donation_id}", dependencies=[Depends(verify_token)])
async def delete_donation(donation_id: int):
    deleted = await Donation.filter(id=donation_id).delete()
    if not deleted:
        return {"code": "4001", "data": None, "msg": "Donation not found"}
    return {"code": "0000", "data": None, "msg": "Donation deleted"}
