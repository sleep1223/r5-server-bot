from decimal import Decimal

from shared_lib.models import Donation


async def create_or_update_donation(
    *,
    donor_name: str | None,
    amount: Decimal,
    currency: str,
    message: str | None,
) -> tuple[Donation, bool]:
    donation, created = await Donation.update_or_create(
        donor_name=donor_name,
        defaults=dict(amount=amount, currency=currency, message=message),
    )
    return donation, created


async def list_donations(*, page_size: int = 1000, offset: int = 0) -> tuple[list, int]:
    total = await Donation.all().count()
    items = await Donation.all().order_by("-created_at").limit(page_size).offset(offset).values()
    return items, total


async def delete_donation(donation_id: int) -> bool:
    deleted = await Donation.filter(id=donation_id).delete()
    return deleted > 0
