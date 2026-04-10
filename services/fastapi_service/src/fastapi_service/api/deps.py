from dataclasses import dataclass

from fastapi import Query


@dataclass
class Pagination:
    page_no: int
    page_size: int
    offset: int


def get_pagination(
    page_no: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
) -> Pagination:
    return Pagination(page_no=page_no, page_size=page_size, offset=(page_no - 1) * page_size)


def get_large_pagination(
    page_no: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(1000, ge=1, le=1000, description="Items per page"),
) -> Pagination:
    return Pagination(page_no=page_no, page_size=page_size, offset=(page_no - 1) * page_size)
