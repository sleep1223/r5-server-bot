import re

from shared_lib.models import Server

_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


def _count_chinese(text: str) -> int:
    return sum(1 for c in text if "\u4e00" <= c <= "\u9fff")


async def resolve_server(query: str) -> Server | None:
    """按查询串解析到唯一 Server。

    - 纯 IPv4 → 精确 host 匹配
    - 含 >1 个中文字符 → 依次尝试: short_name 精确 → short_name ILIKE → name ILIKE
      （同名时取 last_seen_at 最新的一条，避免命中僵尸服）
    - 其它（英文名、长度不足的中文）→ 返回 None，由调用方决定提示
    """
    query = (query or "").strip()
    if not query:
        return None

    if _IPV4_RE.match(query):
        return await Server.get_or_none(host=query)

    if _count_chinese(query) <= 1:
        return None

    s = await Server.get_or_none(short_name=query)
    if s:
        return s

    s = await Server.filter(short_name__icontains=query).order_by("-last_seen_at").first()
    if s:
        return s

    return await Server.filter(name__icontains=query).order_by("-last_seen_at").first()
