import asyncio
import os
import platform
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from Cryptodome.Hash import SHA512
from loguru import logger
from qqwry import QQwry

CN_TZ = ZoneInfo("Asia/Shanghai")
QQWRY_DB_PATH = "data/qqwry.dat"


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
    results = {}
    try:
        if os.path.exists(QQWRY_DB_PATH):
            q = QQwry()
            q.load_file(QQWRY_DB_PATH)
            for ip in ips:
                try:
                    res = q.lookup(ip)
                    if not res:
                        continue
                    location, isp = res
                    if not location:
                        continue

                    sep = "–" if "–" in location else "-"
                    parts = location.split(sep)
                    country = parts[0] if len(parts) > 0 else ""
                    province = parts[1] if len(parts) > 1 else ""

                    results[ip] = {"country": country, "region": province}
                except Exception as e:
                    logger.debug(f"QQwry resolution failed for {ip}: {e}")
    except Exception as e:
        logger.error(f"Failed to open QQwry database: {e}")
    missing_ips = [ip for ip in ips if ip not in results]
    if missing_ips:
        api_url = "http://ip-api.com/batch"
        for i in range(0, len(missing_ips), 100):
            chunk = missing_ips[i : i + 100]
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(api_url, json=chunk, timeout=10.0)
                    if response.status_code == 200:
                        data = response.json()
                        for item in data:
                            if isinstance(item, dict) and item.get("status") == "success":
                                ip = item.get("query")
                                if ip:
                                    results[ip] = {"country": item.get("country"), "region": item.get("regionName")}
            except Exception as e:
                logger.error(f"ip-api.com batch resolution failed: {e}")
    return results


def get_date_range(range_type: str):
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
