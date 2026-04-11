ALLOWED_REASONS = ["NO_COVER", "BE_POLITE", "CHEAT", "RULES"]

# 允许 NO_COVER(撤回掩体) 行为的服务器。命中这些规则的服务器会跳过 NO_COVER 的 kick/ban。
NO_COVER_ALLOWED_SERVER_HOSTS = {"106.75.50.197"}
NO_COVER_ALLOWED_SERVER_NAME_MARKERS = ("[CN(Beijing)]",)


def is_no_cover_allowed_server(server_host: str | None, server_name: str | None) -> bool:
    """判断该服务器是否允许 NO_COVER(撤回掩体) 行为。"""
    if server_host and server_host in NO_COVER_ALLOWED_SERVER_HOSTS:
        return True
    if server_name:
        for marker in NO_COVER_ALLOWED_SERVER_NAME_MARKERS:
            if marker in server_name:
                return True
    return False

WEAPON_MAP: dict[str, str] = {
    "alternator": "mp_weapon_alternator_smg",
    "charge rifle": "mp_weapon_defender",
    "devotion": "mp_weapon_esaw",
    "epg": "mp_weapon_epg",
    "eva8": "mp_weapon_shotgun",
    "flatline": "mp_weapon_vinson",
    "g7": "mp_weapon_g2",
    "havoc": "mp_weapon_energy_ar",
    "hemlok": "mp_weapon_hemlok",
    "kraber": "mp_weapon_sniper",
    "longbow": "mp_weapon_dmr",
    "lstar": "mp_weapon_lstar",
    "mastiff": "mp_weapon_mastiff",
    "mozambique": "mp_weapon_shotgun_pistol",
    "p2020": "mp_weapon_semipistol",
    "peacekeeper": "mp_weapon_energy_shotgun",
    "prowler": "mp_weapon_pdw",
    "r301": "mp_weapon_rspn101",
    "r99": "mp_weapon_r97",
    "re45": "mp_weapon_autopistol",
    "smart pistol": "mp_weapon_smart_pistol",
    "spitfire": "mp_weapon_lmg",
    "triple take": "mp_weapon_doubletake",
    "wingman": "mp_weapon_wingman",
    "volt": "mp_weapon_volt_smg",
    "player": "player",
}

WEAPON_NAME_MAP: dict[str, str] = {v: k for k, v in WEAPON_MAP.items()}


def to_internal_weapon(w: str | None) -> str:
    s = (w or "").strip().lower()
    if not s:
        return s
    if s in WEAPON_MAP:
        return WEAPON_MAP[s]
    return s


def to_display_weapon(w: str | None) -> str:
    s = (w or "").strip().lower()
    if not s:
        return s
    return WEAPON_NAME_MAP.get(s, s)
