from .errors import ErrorCode


def success(data=None, msg: str = "OK", **extra) -> dict:
    result = {"code": ErrorCode.SUCCESS, "data": data, "msg": msg}
    result.update(extra)
    return result


def error(code: str, msg: str, data=None, **extra) -> dict:
    result = {"code": code, "data": data, "msg": msg}
    result.update(extra)
    return result


def paginated(data, total: int, msg: str = "OK", **extra) -> dict:
    result = {"code": ErrorCode.SUCCESS, "data": data, "total": total, "msg": msg}
    result.update(extra)
    return result
