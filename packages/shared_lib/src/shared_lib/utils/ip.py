import ipaddress
from pathlib import Path

import ip2region.searcher as xdb
import ip2region.util as ip2region_util
from loguru import logger
from shared_lib.config import settings


class IPResolver:
    _instance = None
    _searcher = None
    _content = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.load_db()

    def load_db(self):
        try:
            path = Path(settings.ip2region_path)
            if not path.is_absolute():
                # Try to resolve relative to cwd (project root)
                path = Path.cwd() / path

            if path.exists():
                ip2region_util.verify_from_file(str(path))
                self._content = ip2region_util.load_content_from_file(str(path))
                self._searcher = xdb.new_with_buffer(ip2region_util.IPv4, self._content)
                logger.info(f"已从 {path} 加载 ip2region 数据库")
            else:
                logger.warning(f"未在 {path} 找到 ip2region 数据库")
                self._searcher = None
                self._content = None
        except Exception as e:
            logger.error(f"加载 ip2region 数据库失败: {e}")
            self._searcher = None
            self._content = None

    def lookup(self, ip: str) -> tuple[str, str] | None:
        if not self._searcher:
            # Try reloading if it failed previously or wasn't found
            self.load_db()
            if not self._searcher:
                return None

        try:
            text = ip.strip()
            if text.startswith("["):
                closing_bracket = text.find("]")
                if closing_bracket < 0:
                    raise ValueError("missing closing bracket")
                suffix = text[closing_bracket + 1 :]
                if suffix and (not suffix.startswith(":") or not suffix[1:].isdigit()):
                    raise ValueError("invalid bracketed IP endpoint")
                text = text[1:closing_bracket]

            try:
                address = ipaddress.ip_address(text)
            except ValueError:
                host, separator, port = text.rpartition(":")
                if not separator or not port.isdigit():
                    raise
                address = ipaddress.ip_address(host)

            if isinstance(address, ipaddress.IPv6Address):
                if not address.ipv4_mapped:
                    return None
                address = address.ipv4_mapped

            location = self._searcher.search(str(address))
            if not location:
                return None

            parts = location.split("|")
            if len(parts) >= 7:
                country = parts[1]
                region = parts[2]
            else:
                country = parts[0] if len(parts) > 0 else ""
                region = parts[1] if len(parts) > 1 else ""
            return country, region
        except Exception as e:
            logger.debug(f"ip2region 解析 {ip} 失败: {e}")
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
        # logger.debug(f"Missing IPs after ip2region resolution: {missing_ips}")
        pass

    return results
