import asyncio
import platform
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from Cryptodome.Hash import SHA512
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


def get_date_range(range_type: str) -> tuple[datetime, datetime]:
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
    elif range_type == "month":
        start_time = datetime.combine(now.date().replace(day=1), time.min, tzinfo=CN_TZ)
        end_time = now
    return start_time, end_time
