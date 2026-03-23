class ErrorCode:
    """统一 4 位数业务错误码。"""

    # 0xxx — 成功
    SUCCESS = "0000"

    # 1xxx — 认证/授权
    AUTH_MISSING = "1001"
    AUTH_INVALID = "1002"

    # 2xxx — 玩家相关
    PLAYER_NOT_FOUND = "2001"
    PLAYER_NO_NUCLEUS_ID = "2002"
    PLAYER_NOT_ONLINE = "2003"

    # 3xxx — RCON / 服务器操作
    RCON_CONFIG_MISSING = "3001"
    RCON_OPERATION_FAILED = "3002"
    NO_ONLINE_SERVERS = "3003"

    # 4xxx — 资源未找到
    DONATION_NOT_FOUND = "4001"

    # 5xxx — 参数校验
    INVALID_REASON = "5001"
    INVALID_WEAPON = "5002"
