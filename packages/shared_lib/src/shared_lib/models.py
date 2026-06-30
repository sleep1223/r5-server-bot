# type: ignore[override]
from tortoise import fields, models


class Player(models.Model):
    id = fields.IntField(pk=True)
    nucleus_id = fields.BigIntField(null=True, unique=True)
    nucleus_hash = fields.CharField(max_length=100, null=True, unique=True)
    name = fields.CharField(max_length=255, db_index=True)
    ip = fields.CharField(max_length=50, null=True, db_index=True)
    country = fields.CharField(max_length=100, null=True)
    region = fields.CharField(max_length=100, null=True)
    ping = fields.IntField(default=0)
    loss = fields.IntField(default=0)
    status = fields.CharField(max_length=20, default="offline")  # online, offline, kicked, banned
    kick_count = fields.IntField(default=0)
    ban_count = fields.IntField(default=0)
    hardware_name = fields.CharField(max_length=100, null=True)
    input_device = fields.CharField(max_length=50, null=True, db_index=True)
    is_admin = fields.BooleanField(default=False, db_index=True)
    total_playtime_seconds = fields.BigIntField(default=0)
    online_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    ip_infos: fields.ReverseRelation["IpInfo"]

    class Meta:
        table = "players"


class BaseEvent(models.Model):
    id = fields.IntField(pk=True)
    timestamp = fields.BigIntField()
    category = fields.CharField(max_length=50)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        abstract = True


class CharacterSelected(BaseEvent):
    player = fields.ForeignKeyField("models.Player", related_name="character_selections")
    player_data = fields.JSONField()  # Store snapshot of player state at this event
    match = fields.ForeignKeyField("models.Match", related_name="character_selections", null=True, db_index=True)
    server = fields.ForeignKeyField("models.Server", related_name="character_selections", null=True, db_index=True)

    class Meta:
        table = "character_selected"


class GameStateChanged(BaseEvent):
    state = fields.CharField(max_length=100)
    match = fields.ForeignKeyField("models.Match", related_name="game_state_events", null=True, db_index=True)
    server = fields.ForeignKeyField("models.Server", related_name="game_state_events", null=True, db_index=True)

    class Meta:
        table = "game_state_changed"


class InitEvent(BaseEvent):
    game_version = fields.CharField(max_length=100)
    api_version = fields.JSONField()
    platform = fields.CharField(max_length=50)

    class Meta:
        table = "init_events"


class MatchSetup(BaseEvent):
    map_name = fields.CharField(max_length=100)
    playlist_name = fields.CharField(max_length=100)
    playlist_desc = fields.CharField(max_length=100)
    datacenter = fields.JSONField()
    aim_assist_on = fields.BooleanField()
    # 上游服务器列表里的 server_id（字符串标识符，保留原值供审计）
    server_id = fields.CharField(max_length=100)
    # 业务 Match 聚合实体（server 可通过 match.server 反查）
    match = fields.ForeignKeyField("models.Match", related_name="setup_events", null=True, db_index=True)

    class Meta:
        table = "match_setup"


class MatchStateEnd(BaseEvent):
    """SDK 在 `GameStateChanged=WinnerDetermined` 时发的独立事件（带 winners）。

    注意：WinnerDetermined 状态**不会**以 GameStateChanged 的形式到达，SDK 把
    它转成了 matchStateEnd 事件发送。这是最可靠的"一局结束"信号。
    """

    state = fields.CharField(max_length=100)  # 通常是 "WinnerDetermined"
    winners = fields.JSONField(null=True)  # [{name, teamId, nucleusHash, ...}, ...]
    match = fields.ForeignKeyField("models.Match", related_name="state_end_events", null=True, db_index=True)
    server = fields.ForeignKeyField("models.Server", related_name="state_end_events", null=True, db_index=True)

    class Meta:
        table = "match_state_end"


class PlayerConnected(BaseEvent):
    player = fields.ForeignKeyField("models.Player", related_name="connections")
    player_data = fields.JSONField()
    match = fields.ForeignKeyField("models.Match", related_name="player_connections", null=True, db_index=True)
    server = fields.ForeignKeyField("models.Server", related_name="player_connections", null=True, db_index=True)

    class Meta:
        table = "player_connected"


class PlayerDisconnected(BaseEvent):
    player = fields.ForeignKeyField("models.Player", related_name="disconnections")
    player_data = fields.JSONField()
    can_reconnect = fields.BooleanField(null=True)
    is_alive = fields.BooleanField(null=True)
    match = fields.ForeignKeyField("models.Match", related_name="player_disconnections", null=True, db_index=True)
    server = fields.ForeignKeyField("models.Server", related_name="player_disconnections", null=True, db_index=True)

    class Meta:
        table = "player_disconnected"


class PlayerKilled(BaseEvent):
    weapon = fields.CharField(max_length=100)
    attacker = fields.ForeignKeyField("models.Player", related_name="kills", null=True)
    awarded_to = fields.ForeignKeyField("models.Player", related_name="awarded_kills", null=True)
    match = fields.ForeignKeyField("models.Match", related_name="kill_events", null=True)
    server = fields.ForeignKeyField("models.Server", related_name="kill_events", null=True)
    victim = fields.ForeignKeyField("models.Player", related_name="deaths", null=True)

    class Meta:
        table = "player_killed"


class PlayerMatchWeaponStat(models.Model):
    id = fields.IntField(pk=True)
    player = fields.ForeignKeyField("models.Player", related_name="match_weapon_stats")
    opponent = fields.ForeignKeyField("models.Player", related_name="opponent_match_weapon_stats", null=True)
    match = fields.ForeignKeyField("models.Match", related_name="weapon_stats")
    server = fields.ForeignKeyField("models.Server", related_name="match_weapon_stats")
    weapon = fields.CharField(max_length=100, db_index=True)
    shots = fields.IntField(default=0)
    hits = fields.IntField(default=0)
    bullets_hit = fields.FloatField(default=0)
    damage = fields.FloatField(default=0)
    headshots = fields.IntField(default=0)
    kills = fields.IntField(default=0)
    accuracy = fields.FloatField(default=0)
    accuracy_percent = fields.FloatField(default=0)
    input_device = fields.CharField(max_length=50, default="unknown", db_index=True)
    source = fields.CharField(max_length=30, default="sdk_match_end", db_index=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "player_match_weapon_stats"
        unique_together = (("match", "player", "opponent", "weapon", "source"),)
        indexes = (
            ("match_id", "player_id", "opponent_id"),
            ("player_id", "weapon"),
            ("opponent_id", "weapon"),
            ("server_id", "weapon"),
            ("input_device", "weapon"),
        )


class PlayerKillDailyWeaponOpponentStat(models.Model):
    id = fields.BigIntField(pk=True)
    stat_date = fields.DateField(db_index=True)
    player = fields.ForeignKeyField("models.Player", related_name="daily_weapon_opponent_stats")
    opponent = fields.ForeignKeyField("models.Player", related_name="daily_opponent_weapon_stats", null=True)
    server = fields.ForeignKeyField("models.Server", related_name="daily_weapon_opponent_stats")
    weapon = fields.CharField(max_length=100, db_index=True)
    kills = fields.IntField(default=0)
    deaths = fields.IntField(default=0)
    awarded_kills = fields.IntField(default=0)
    input_device = fields.CharField(max_length=50, default="unknown", db_index=True)
    refreshed_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "player_kill_daily_weapon_opponent_stats"
        unique_together = (("stat_date", "server", "player", "opponent", "weapon", "input_device"),)
        indexes = (
            ("player_id", "stat_date", "opponent_id"),
            ("player_id", "stat_date", "weapon"),
            ("stat_date", "weapon", "player_id"),
            ("stat_date", "input_device", "kills"),
            ("stat_date", "kills"),
        )


class PlayerKillDailyWeaponStat(models.Model):
    id = fields.BigIntField(pk=True)
    stat_date = fields.DateField(db_index=True)
    player = fields.ForeignKeyField("models.Player", related_name="daily_weapon_stats")
    server = fields.ForeignKeyField("models.Server", related_name="daily_weapon_stats")
    weapon = fields.CharField(max_length=100, db_index=True)
    kills = fields.IntField(default=0)
    deaths = fields.IntField(default=0)
    awarded_kills = fields.IntField(default=0)
    input_device = fields.CharField(max_length=50, default="unknown", db_index=True)
    refreshed_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "player_kill_daily_weapon_stats"
        unique_together = (("stat_date", "server", "player", "weapon", "input_device"),)
        indexes = (
            ("player_id", "stat_date", "weapon"),
            ("stat_date", "weapon", "player_id"),
            ("stat_date", "input_device", "kills"),
            ("stat_date", "kills"),
            ("server_id", "stat_date", "player_id"),
        )


class PlayerKillDailyOpponentStat(models.Model):
    id = fields.BigIntField(pk=True)
    stat_date = fields.DateField(db_index=True)
    player = fields.ForeignKeyField("models.Player", related_name="daily_opponent_stats")
    opponent = fields.ForeignKeyField("models.Player", related_name="daily_as_opponent_stats")
    server = fields.ForeignKeyField("models.Server", related_name="daily_opponent_stats")
    kills = fields.IntField(default=0)
    deaths = fields.IntField(default=0)
    refreshed_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "player_kill_daily_opponent_stats"
        unique_together = (("stat_date", "server", "player", "opponent"),)
        indexes = (
            ("player_id", "stat_date", "opponent_id"),
            ("server_id", "stat_date", "player_id"),
            ("stat_date", "player_id"),
        )


class Server(models.Model):
    id = fields.IntField(pk=True)
    server_id = fields.CharField(max_length=128, null=True, unique=True)
    host = fields.CharField(max_length=64)  # 公网 IP；实际服务器身份使用 host + port
    port = fields.IntField(default=37015)
    region = fields.CharField(max_length=50, null=True)
    netkey = fields.CharField(max_length=255, null=True)
    ping = fields.IntField(default=0)
    name = fields.CharField(max_length=255, db_index=True)
    short_name = fields.CharField(max_length=100, null=True, db_index=True)
    playlist = fields.CharField(max_length=100, null=True)
    map = fields.CharField(max_length=100, null=True)
    player_count = fields.IntField(default=0)
    max_players = fields.IntField(default=0)
    is_self_hosted = fields.BooleanField(default=False)
    has_status = fields.BooleanField(default=False, db_index=True)
    last_seen_at = fields.DatetimeField(null=True, db_index=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    matches: fields.ReverseRelation["Match"]

    class Meta:
        table = "servers"
        unique_together = (("host", "port"),)


class Match(models.Model):
    """一场对局的聚合实体。

    MatchSetup 事件触发创建；下一次对局的 Prematch(has_entered_playing=True 前提下)、
    新 MatchSetup、或 inactivity 超时触发关闭。
    status: active / completed / abandoned
    end_reason: prematch_cycle / new_match / inactivity
    """

    id = fields.IntField(pk=True)
    full_match_id = fields.CharField(max_length=128, unique=True)  # "{host}-{YYYYMMDD}-{HHMMSS}"
    server = fields.ForeignKeyField("models.Server", related_name="matches", db_index=True)
    server_id: int  # Tortoise 隐式 FK id 字段，给 Pyright 看
    map_name = fields.CharField(max_length=100)
    playlist_name = fields.CharField(max_length=100)
    playlist_desc = fields.CharField(max_length=100)
    datacenter = fields.JSONField(null=True)
    aim_assist_on = fields.BooleanField(default=False)
    started_at = fields.DatetimeField(db_index=True)
    ended_at = fields.DatetimeField(null=True, db_index=True)
    status = fields.CharField(max_length=20, default="active", db_index=True)
    end_reason = fields.CharField(max_length=30, null=True)
    current_state = fields.CharField(max_length=30, null=True)  # 最近一次 GameStateChanged.state
    has_entered_playing = fields.BooleanField(default=False)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    setup_events: fields.ReverseRelation["MatchSetup"]

    class Meta:
        table = "matches"


class SdkMatchEndReport(models.Model):
    id = fields.IntField(pk=True)
    server = fields.ForeignKeyField("models.Server", related_name="sdk_match_end_reports", null=True, on_delete=fields.SET_NULL)
    match = fields.ForeignKeyField("models.Match", related_name="sdk_end_reports", null=True, on_delete=fields.SET_NULL)
    server_identifier = fields.CharField(max_length=128, db_index=True)
    server_ip = fields.CharField(max_length=64, null=True, db_index=True)
    server_port = fields.IntField(null=True)
    map_name = fields.CharField(max_length=100, null=True)
    playlist_name = fields.CharField(max_length=100, null=True)
    sdk_version = fields.CharField(max_length=100, null=True)
    tick = fields.BigIntField(null=True)
    spawn_count = fields.IntField(default=0)
    ended_at = fields.DatetimeField(db_index=True)
    num_players = fields.IntField(default=0)
    max_players = fields.IntField(default=0)
    payload = fields.JSONField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "sdk_match_end_reports"
        indexes = (("server_identifier", "ended_at"), ("server_id", "ended_at"), ("match_id", "created_at"))


class IpInfo(models.Model):
    id = fields.IntField(pk=True)
    ip = fields.CharField(max_length=50, unique=True, db_index=True)
    country = fields.TextField(null=True)
    region = fields.TextField(null=True)
    ping = fields.IntField(default=0)
    is_resolved = fields.BooleanField(default=False)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    players = fields.ManyToManyField(model_name="models.Player", related_name="ip_infos", through="player_ip_links")

    class Meta:
        table = "ip_info"


class Donation(models.Model):
    id = fields.IntField(pk=True)
    donor_name = fields.CharField(max_length=255, null=True, db_index=True)
    amount = fields.DecimalField(max_digits=10, decimal_places=2)
    currency = fields.CharField(max_length=10, default="CNY")
    message = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "donations"


class BanRecord(models.Model):
    id = fields.IntField(pk=True)
    player = fields.ForeignKeyField("models.Player", related_name="ban_records")
    reason = fields.CharField(max_length=50)
    operator = fields.CharField(max_length=255, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "ban_records"


class PlayerAccessOperation(models.Model):
    id = fields.IntField(pk=True)
    action = fields.CharField(max_length=20, db_index=True)  # ban, kick, unban, ack, rule_create, admin_set
    target_type = fields.CharField(max_length=20, db_index=True)  # player, uid, ip, cidr, country, region
    target_value = fields.CharField(max_length=255, db_index=True)
    normalized_target = fields.CharField(max_length=255, null=True, db_index=True)
    server_scope = fields.CharField(max_length=20, default="global", db_index=True)
    server_id = fields.CharField(max_length=128, null=True, db_index=True)
    reason = fields.CharField(max_length=50, null=True)
    remark = fields.TextField(null=True)
    operator = fields.CharField(max_length=255, null=True)
    player = fields.ForeignKeyField(
        "models.Player",
        related_name="access_operations",
        null=True,
        on_delete=fields.SET_NULL,
    )
    result = fields.JSONField(null=True)
    linked_rule_ids = fields.JSONField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "player_access_operations"
        indexes = (("action", "target_type", "created_at"), ("server_scope", "server_id", "created_at"))


class PlayerAccessRule(models.Model):
    id = fields.IntField(pk=True)
    rule_type = fields.CharField(max_length=20, db_index=True)  # uid, ip, cidr, geo, country, region
    action = fields.CharField(max_length=10, db_index=True)  # allow, deny
    value = fields.CharField(max_length=255, db_index=True)
    server_scope = fields.CharField(max_length=20, default="global", db_index=True)  # global, server
    server_id = fields.CharField(max_length=128, null=True, db_index=True)
    reason = fields.CharField(max_length=255, null=True)
    remark = fields.TextField(null=True)
    rule_id = fields.CharField(max_length=100, null=True, unique=True)
    operator = fields.CharField(max_length=255, null=True)
    source_action = fields.CharField(max_length=20, null=True, db_index=True)
    source_operation = fields.ForeignKeyField(
        "models.PlayerAccessOperation",
        related_name="rules",
        null=True,
        on_delete=fields.SET_NULL,
    )
    expires_at = fields.DatetimeField(null=True, db_index=True)
    enabled = fields.BooleanField(default=True, db_index=True)
    priority = fields.IntField(default=100)
    player = fields.ForeignKeyField(
        "models.Player",
        related_name="access_rules",
        null=True,
        on_delete=fields.SET_NULL,
    )
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "player_access_rules"
        indexes = (("rule_type", "action", "value", "enabled"), ("server_scope", "server_id", "enabled"))


class PlayerAccessNotice(models.Model):
    id = fields.IntField(pk=True)
    player = fields.ForeignKeyField(
        "models.Player",
        related_name="access_notices",
        null=True,
        on_delete=fields.SET_NULL,
    )
    uid = fields.CharField(max_length=64, db_index=True)
    action = fields.CharField(max_length=20, default="kick", db_index=True)
    reason = fields.CharField(max_length=50, null=True)
    message = fields.TextField(null=True)
    message_context = fields.JSONField(null=True)
    server_scope = fields.CharField(max_length=20, default="global", db_index=True)
    server_id = fields.CharField(max_length=128, null=True, db_index=True)
    requires_ack = fields.BooleanField(default=True, db_index=True)
    acknowledged_at = fields.DatetimeField(null=True, db_index=True)
    expires_at = fields.DatetimeField(null=True, db_index=True)
    operation = fields.ForeignKeyField(
        "models.PlayerAccessOperation",
        related_name="notices",
        null=True,
        on_delete=fields.SET_NULL,
    )
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "player_access_notices"
        indexes = (("uid", "requires_ack", "acknowledged_at"), ("server_scope", "server_id", "requires_ack"))


class SteamAuthLog(models.Model):
    id = fields.IntField(pk=True)
    steam_id = fields.BigIntField(db_index=True)
    persona_name = fields.CharField(max_length=255, null=True)
    server_endpoint = fields.CharField(max_length=255, null=True)
    client_ip = fields.CharField(max_length=64, null=True, db_index=True)
    success = fields.BooleanField(db_index=True)
    error_code = fields.CharField(max_length=64, null=True)
    created_at = fields.DatetimeField(auto_now_add=True, db_index=True)

    class Meta:
        table = "steam_auth_log"


class UserBinding(models.Model):
    id = fields.IntField(pk=True)
    platform = fields.CharField(max_length=20)  # "qq" / "kaiheila"
    platform_uid = fields.CharField(max_length=64)  # 平台用户ID(如QQ号)
    player = fields.ForeignKeyField("models.Player", related_name="bindings")
    app_key = fields.CharField(max_length=64, unique=True)  # 前端认证用
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "user_binding"
        unique_together = (("platform", "platform_uid"),)


class TeamPost(models.Model):
    id = fields.IntField(pk=True)
    creator = fields.ForeignKeyField("models.UserBinding", related_name="created_teams")
    creator_id: int
    slots_needed = fields.IntField()  # 需要的队友数量: 1 或 2
    status = fields.CharField(max_length=16, default="open")  # open / full / cancelled / expired
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    members: fields.ReverseRelation["TeamMember"]

    class Meta:
        table = "team_post"


class TeamMember(models.Model):
    id = fields.IntField(pk=True)
    team = fields.ForeignKeyField("models.TeamPost", related_name="members")
    user_binding = fields.ForeignKeyField("models.UserBinding", related_name="team_memberships")
    role = fields.CharField(max_length=16)  # "creator" / "member"
    joined_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "team_member"
        unique_together = (("team", "user_binding"),)
