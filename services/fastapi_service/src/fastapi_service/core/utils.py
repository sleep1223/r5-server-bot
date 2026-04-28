import asyncio
import platform
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from Cryptodome.Hash import SHA512
from fastapi.security import HTTPAuthorizationCredentials
from loguru import logger
from shared_lib.utils.ip import resolve_ips_batch as _resolve_ips_batch

CN_TZ = ZoneInfo("Asia/Shanghai")


def generate_hash(data: str) -> str:
    hash_obj = SHA512.new(data.encode("utf-8"))
    return hash_obj.hexdigest()[:32]


async def get_local_ping(ip: str) -> int:
    param = "-n" if platform.system().lower() == "windows" else "-c"
    timeout_param = ["-w", "1000"] if platform.system().lower() == "windows" else ["-W", "1"]
    command = ["ping", param, "1", *timeout_param, ip]
    try:
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await process.communicate()
        if process.returncode == 0:
            output = stdout.decode("utf-8", errors="ignore")
            match = re.search(r"time[=<](\d+)", output, re.IGNORECASE)
            if match:
                return int(match.group(1))
            if "time<1ms" in output.lower().replace(" ", ""):
                return 1
    except Exception as e:
        logger.warning(f"Ping failed for {ip}: {e}")
    return 0


async def resolve_ips_batch(ips: list[str]) -> dict[str, dict]:
    return _resolve_ips_batch(ips)


def get_date_range(range_type: str) -> tuple[datetime | None, datetime | None]:
    now = datetime.now(CN_TZ)
    start_time = None
    end_time = None
    if range_type == "today":
        start_time = datetime.combine(now.date(), time.min, tzinfo=CN_TZ)
        end_time = now
    elif range_type == "yesterday":
        yesterday = now - timedelta(days=1)
        start_time = datetime.combine(yesterday.date(), time.min, tzinfo=CN_TZ)
        end_time = datetime.combine(yesterday.date(), time.max, tzinfo=CN_TZ)
    elif range_type == "week":
        start_of_week = now.date() - timedelta(days=now.weekday())
        start_time = datetime.combine(start_of_week, time.min, tzinfo=CN_TZ)
        end_time = now
    elif range_type == "last_week":
        start_of_this_week = now.date() - timedelta(days=now.weekday())
        start_of_last_week = start_of_this_week - timedelta(days=7)
        end_of_last_week = start_of_this_week - timedelta(days=1)
        start_time = datetime.combine(start_of_last_week, time.min, tzinfo=CN_TZ)
        end_time = datetime.combine(end_of_last_week, time.max, tzinfo=CN_TZ)
    elif range_type == "month":
        start_time = datetime.combine(now.date().replace(day=1), time.min, tzinfo=CN_TZ)
        end_time = now
    return start_time, end_time


def calc_kd(kills: int, deaths: int) -> float:
    if deaths == 0:
        return float(kills)
    return round(kills / deaths, 2)


def parse_short_name(full_name: str) -> str:
    match = re.match(r"^(\[.*?\])", full_name)
    return match.group(1) if match else full_name


def check_is_admin(credentials: HTTPAuthorizationCredentials | None, access_tokens: list[str] | None) -> bool:
    """未配置 access_tokens 时 fail-closed 返回 False，避免在未配置环境暴露 admin 字段。"""
    if not access_tokens:
        return False
    if credentials and credentials.credentials in access_tokens:
        return True
    return False
