# type: ignore[override]
from tortoise import fields, models


class Player(models.Model):
    id = fields.IntField(pk=True)
    nucleus_id = fields.BigIntField(null=True, unique=True)
    nucleus_hash = fields.CharField(max_length=100, null=True, unique=True)
    name = fields.CharField(max_length=255, db_index=True)
    ip = fields.CharField(max_length=50, null=True)
    country = fields.CharField(max_length=100, null=True)
    region = fields.CharField(max_length=100, null=True)
    ping = fields.IntField(default=0)
    loss = fields.IntField(default=0)
    status = fields.CharField(max_length=20, default="offline")  # online, offline, kicked, banned
    kick_count = fields.IntField(default=0)
    ban_count = fields.IntField(default=0)
    hardware_name = fields.CharField(max_length=100, null=True)
    online_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

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

    class Meta:
        table = "character_selected"


class GameStateChanged(BaseEvent):
    state = fields.CharField(max_length=100)

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
    server_id = fields.CharField(max_length=100)

    class Meta:
        table = "match_setup"


class PlayerConnected(BaseEvent):
    player = fields.ForeignKeyField("models.Player", related_name="connections")
    player_data = fields.JSONField()

    class Meta:
        table = "player_connected"


class PlayerDisconnected(BaseEvent):
    player = fields.ForeignKeyField("models.Player", related_name="disconnections")
    player_data = fields.JSONField()
    can_reconnect = fields.BooleanField(null=True)
    is_alive = fields.BooleanField(null=True)

    class Meta:
        table = "player_disconnected"


class PlayerKilled(BaseEvent):
    attacker = fields.ForeignKeyField("models.Player", related_name="kills", null=True)
    victim = fields.ForeignKeyField("models.Player", related_name="deaths", null=True)
    awarded_to = fields.ForeignKeyField("models.Player", related_name="awarded_kills", null=True)
    attacker_data = fields.JSONField(null=True)
    victim_data = fields.JSONField(null=True)
    awarded_to_data = fields.JSONField(null=True)
    weapon = fields.CharField(max_length=100)

    class Meta:
        table = "player_killed"
