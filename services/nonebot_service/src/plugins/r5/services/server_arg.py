import re

_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


def _count_chinese(text: str) -> int:
    return sum(1 for c in text if "\u4e00" <= c <= "\u9fff")


def pop_server_arg(text: str) -> tuple[str, str | None]:
    """把命令文本尾部的 `@<host-or-name>` 服务器标识剥离。

    规则:
    - 以最后一个 `@` 起到行尾为候选
    - 候选必须是 IPv4 或包含 >1 个中文字符才被采纳
    - 其它情况返回原文本 + None，由调用方按无 server 处理

    例:
        "/kd 今日 @北京二服"     -> ("/kd 今日", "北京二服")
        "/个人kd 玩家 @1.2.3.4" -> ("/个人kd 玩家", "1.2.3.4")
        "/kd 今日"               -> ("/kd 今日", None)
        "/kd 今日 @a"            -> ("/kd 今日 @a", None)   # 候选太短，忽略
    """
    text = (text or "").strip()
    idx = text.rfind("@")
    if idx == -1:
        return text, None

    candidate = text[idx + 1 :].strip()
    if not candidate:
        return text, None

    if _IPV4_RE.match(candidate) or _count_chinese(candidate) > 1:
        return text[:idx].strip(), candidate
    return text, None
