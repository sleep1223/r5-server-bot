import math
import re
from dataclasses import dataclass

from shared_lib.config import settings
from shared_lib.models import GameConfigPreset, UserBinding
from tortoise.expressions import Q

MOUSE_KEYS = {
    "mouse_sensitivity",
    "mouse_use_per_scope_sensitivity_scalars",
    *(f"mouse_zoomed_sensitivity_scalar_{index}" for index in range(8)),
}
FOV_KEYS = {"cl_fovScale"}
CONTROLLER_KEYS = {
    "gamepad_aim_speed",
    "gamepad_use_per_scope_sensitivity_scalars",
    "gamepad_use_per_scope_ads_settings",
    "gamepad_custom_enabled",
    "gamepad_custom_ads_pitch",
    "gamepad_custom_ads_yaw",
    "gamepad_custom_ads_turn_pitch",
    "gamepad_custom_ads_turn_yaw",
    "gamepad_custom_ads_turn_time",
    "gamepad_custom_ads_turn_delay",
    "gamepad_custom_hip_pitch",
    "gamepad_custom_hip_yaw",
    "gamepad_custom_hip_turn_pitch",
    "gamepad_custom_hip_turn_yaw",
    "gamepad_custom_hip_turn_time",
    "gamepad_custom_hip_turn_delay",
    "gamepad_custom_curve",
    "gamepad_custom_deadzone_in",
    "gamepad_custom_deadzone_out",
    "gamepad_deadzone_index_look",
    "gamepad_deadzone_index_move",
    "gamepad_look_curve",
    "gamepad_trigger_threshold",
    *(f"gamepad_aim_speed_ads_{index}" for index in range(8)),
    *(f"gamepad_ads_advanced_sensitivity_scalar_{index}" for index in range(8)),
}
ALLOWED_KEYS = MOUSE_KEYS | CONTROLLER_KEYS | FOV_KEYS

_LINE_RE = re.compile(r'^(?P<key>[A-Za-z0-9_]+)[ \t]+"(?P<value>[^"\r\n]+)"$')
_NUMBER_RE = re.compile(r"^[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?$")


class GameConfigValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedGameConfig:
    content: str
    keys: tuple[str, ...]
    has_mouse: bool
    has_controller: bool
    has_fov: bool


def parse_game_config_content(content: str) -> ParsedGameConfig:
    if len(content.encode("utf-8")) > settings.game_config_max_content_bytes:
        raise GameConfigValidationError("配置内容超过大小限制")

    parsed: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        match = _LINE_RE.fullmatch(line)
        if match is None:
            raise GameConfigValidationError(f"第 {line_number} 行格式无效")
        key = match.group("key")
        value = match.group("value")
        if key not in ALLOWED_KEYS:
            raise GameConfigValidationError(f"第 {line_number} 行包含未知配置项 {key}")
        if key in seen:
            raise GameConfigValidationError(f"配置项 {key} 重复")
        if _NUMBER_RE.fullmatch(value) is None:
            raise GameConfigValidationError(f"配置项 {key} 的值必须是有限数字")
        try:
            number = float(value)
        except ValueError as exc:
            raise GameConfigValidationError(f"配置项 {key} 的值必须是有限数字") from exc
        if not math.isfinite(number):
            raise GameConfigValidationError(f"配置项 {key} 的值必须是有限数字")
        seen.add(key)
        parsed.append((key, value))

    if not parsed:
        raise GameConfigValidationError("配置内容不能为空")

    keys = tuple(key for key, _ in parsed)
    return ParsedGameConfig(
        content="\n".join(f'{key} "{value}"' for key, value in parsed),
        keys=keys,
        has_mouse=any(key in MOUSE_KEYS for key in keys),
        has_controller=any(key in CONTROLLER_KEYS for key in keys),
        has_fov=any(key in FOV_KEYS for key in keys),
    )


def _serialize(preset: GameConfigPreset, *, include_content: bool) -> dict:
    creator = preset.creator
    player = creator.player
    result = {
        "id": preset.id,
        "creator_name": player.name,
        "name": preset.name,
        "remark": preset.remark,
        "source_game": preset.source_game,
        "has_mouse": preset.has_mouse,
        "has_controller": preset.has_controller,
        "has_fov": preset.has_fov,
        "schema_version": preset.schema_version,
        "created_at": preset.created_at.isoformat(),
        "updated_at": preset.updated_at.isoformat(),
    }
    if include_content:
        result["content"] = preset.content
    return result


async def list_presets(
    *,
    page_size: int,
    offset: int,
    q: str | None = None,
    input_device: str | None = None,
) -> tuple[list[dict], int]:
    query = GameConfigPreset.all()
    search = (q or "").strip()
    if search:
        query = query.filter(Q(name__icontains=search) | Q(creator__player__name__icontains=search))
    if input_device == "mouse_keyboard":
        query = query.filter(has_mouse=True)
    elif input_device == "controller":
        query = query.filter(has_controller=True)
    total = await query.count()
    presets = await query.prefetch_related("creator__player").order_by("-updated_at").offset(offset).limit(page_size)
    return [_serialize(preset, include_content=False) for preset in presets], total


async def get_preset(preset_id: int) -> dict | None:
    preset = await GameConfigPreset.filter(id=preset_id).prefetch_related("creator__player").first()
    return _serialize(preset, include_content=True) if preset else None


async def get_mine(binding_id: int) -> dict | None:
    preset = await GameConfigPreset.filter(creator_id=binding_id).prefetch_related("creator__player").first()
    return _serialize(preset, include_content=True) if preset else None


async def save_mine(
    binding: UserBinding,
    *,
    name: str,
    remark: str | None,
    source_game: str,
    content: str,
) -> dict:
    if source_game not in {"apex", "r5"}:
        raise GameConfigValidationError("来源游戏无效")
    normalized_name = name.strip()
    if not normalized_name:
        raise GameConfigValidationError("配置名称不能为空")
    if len(normalized_name) > 64:
        raise GameConfigValidationError("配置名称不能超过 64 个字符")
    normalized_remark = (remark or "").strip() or None
    if normalized_remark is not None and len(normalized_remark) > 500:
        raise GameConfigValidationError("配置备注不能超过 500 个字符")
    parsed = parse_game_config_content(content)
    defaults = {
        "name": normalized_name,
        "remark": normalized_remark,
        "source_game": source_game,
        "content": parsed.content,
        "has_mouse": parsed.has_mouse,
        "has_controller": parsed.has_controller,
        "has_fov": parsed.has_fov,
        "schema_version": 1,
    }
    preset, _ = await GameConfigPreset.update_or_create(defaults=defaults, creator_id=binding.id)
    await preset.fetch_related("creator__player")
    return _serialize(preset, include_content=True)


async def delete_mine(binding_id: int) -> bool:
    return bool(await GameConfigPreset.filter(creator_id=binding_id).delete())


async def delete_preset(preset_id: int) -> bool:
    return bool(await GameConfigPreset.filter(id=preset_id).delete())
