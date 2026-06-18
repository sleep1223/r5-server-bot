from pathlib import Path

from loguru import logger
from qqwry import QQwry
from shared_lib.config import settings


class IPResolver:
    _instance = None
    _q = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.load_db()

    def load_db(self):
        try:
            path = Path(settings.qqwry_path)
            if not path.is_absolute():
                # Try to resolve relative to cwd (project root)
                path = Path.cwd() / path

            if path.exists():
                self._q = QQwry()
                self._q.load_file(str(path))
                logger.info(f"已从 {path} 加载 QQwry 数据库")
            else:
                logger.warning(f"未在 {path} 找到 QQwry 数据库")
                self._q = None
        except Exception as e:
            logger.error(f"加载 QQwry 数据库失败: {e}")
            self._q = None

    def lookup(self, ip: str) -> tuple[str, str] | None:
        if not self._q:
            # Try reloading if it failed previously or wasn't found
            self.load_db()
            if not self._q:
                return None

        try:
            res = self._q.lookup(ip)
            if not res:
                return None
            location, isp = res
            if not location:
                return None

            sep = "–" if "–" in location else "-"
            parts = location.split(sep)
            country = parts[0] if len(parts) > 0 else ""
            region = parts[1] if len(parts) > 1 else ""
            return country, region
        except Exception as e:
            logger.debug(f"QQwry 解析 {ip} 失败: {e}")
            return None


def resolve_ip(ip: str) -> dict:
    resolver = IPResolver.get_instance()
    res = resolver.lookup(ip)
    if res:
        return {"country": res[0], "region": res[1]}
    return {"country": "", "region": ""}


def resolve_ips_batch(ips: list[str]) -> dict[str, dict]:
    resolver = IPResolver.get_instance()
    results = {}
    for ip in ips:
        res = resolver.lookup(ip)
        if res:
            results[ip] = {"country": res[0], "region": res[1]}

    missing_ips = [ip for ip in ips if ip not in results]
    if missing_ips:
        # logger.debug(f"Missing IPs after QQwry resolution: {missing_ips}")
        pass

    return results
