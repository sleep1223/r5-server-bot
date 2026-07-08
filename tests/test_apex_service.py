import unittest

from fastapi_service.services import apex_service
from fastapi_service.services.apex_translations import apex_translations_payload


class ApexServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_predator_returns_only_score_and_population(self) -> None:
        original_fetch = apex_service._fetch
        called_endpoints: list[str] = []

        async def fake_fetch(endpoint: str, params: dict | None = None) -> dict:
            called_endpoints.append(endpoint)
            if endpoint == "predator":
                return {
                    "RP": {
                        "PC": {
                            "foundRank": 750,
                            "val": 12345,
                            "uid": "10001",
                            "totalMastersAndPreds": 900,
                        },
                        "SWITCH": {
                            "foundRank": 750,
                            "val": 12345,
                            "uid": "-1",
                            "totalMastersAndPreds": 900,
                        },
                    }
                }
            raise AssertionError(f"unexpected endpoint: {endpoint}")

        apex_service._fetch = fake_fetch
        try:
            payload = await apex_service.fetch_predator()
        finally:
            apex_service._fetch = original_fetch

        pc = payload["data"]["PC"]
        self.assertEqual(called_endpoints, ["predator"])
        self.assertIsNone(payload["raw"])
        self.assertEqual(pc, {"name": "PC 端", "val": 12345, "total_masters": 900})
        self.assertEqual(payload["data"]["SWITCH"], {"name": "Switch 端", "val": 12345, "total_masters": 900})

    async def test_fetch_server_status_keeps_english_names_and_adds_chinese_names(self) -> None:
        original_fetch = apex_service._fetch

        async def fake_fetch(endpoint: str, params: dict | None = None) -> dict:
            self.assertEqual(endpoint, "servers")
            return {
                "Origin_login": {
                    "EU-West": {"Status": "UP", "ResponseTime": 42},
                }
            }

        apex_service._fetch = fake_fetch
        try:
            payload = await apex_service.fetch_server_status()
        finally:
            apex_service._fetch = original_fetch

        section = payload["data"][0]
        self.assertEqual(section["section_name"], "Origin Login")
        self.assertEqual(section["section_name_zh"], "Origin 登录")
        row = section["rows"][0]
        self.assertEqual(row["name"], "EU West")
        self.assertEqual(row["name_zh"], "欧盟西部")
        self.assertEqual(row["status"], "UP")
        self.assertEqual(row["status_zh"], "在线")

    async def test_fetch_map_rotation_keeps_english_and_adds_chinese_labels(self) -> None:
        original_fetch = apex_service._fetch

        async def fake_fetch(endpoint: str, params: dict | None = None) -> dict:
            self.assertEqual(endpoint, "maprotation")
            return {
                "battle_royale": {
                    "current": {"map": "World's Edge", "remainingTimer": "01:23:45"},
                    "next": {"map": "Olympus"},
                },
                "ranked": {
                    "current": {"map": "Storm Point"},
                    "next": {"map": "Broken Moon"},
                },
                "ltm": {
                    "current": {"map": "TDM", "eventName": "Gun Run"},
                    "next": {"map": "Control"},
                },
            }

        apex_service._fetch = fake_fetch
        try:
            payload = await apex_service.fetch_map_rotation()
        finally:
            apex_service._fetch = original_fetch

        current = payload["data"]["battle_royale"]["current"]
        self.assertEqual(current["map"], "World's Edge")
        self.assertEqual(current["map_zh"], "世界尽头")
        self.assertEqual(payload["data"]["ltm"]["current"]["eventName_zh"], "军火赛")

    def test_translations_payload_contains_plugin_terms(self) -> None:
        payload = apex_translations_payload()
        self.assertEqual(payload["zh"]["Kings Canyon"], "诸王峡谷")
        self.assertEqual(payload["zh"]["Wraith"], "恶灵")

    async def test_get_cached_resource_does_not_refresh_empty_cache_by_default(self) -> None:
        original_cache = apex_service.apex_cache
        original_refresh = apex_service.refresh_cached_resource

        async def fail_refresh(resource: apex_service.CachedResource) -> apex_service.ApexCacheEntry:
            raise AssertionError(f"unexpected refresh: {resource}")

        apex_service.apex_cache = apex_service.ApexDataCache()
        apex_service.refresh_cached_resource = fail_refresh
        try:
            with self.assertRaises(apex_service.ApexServiceError) as ctx:
                await apex_service.get_cached_resource("predator")
        finally:
            apex_service.apex_cache = original_cache
            apex_service.refresh_cached_resource = original_refresh

        self.assertEqual(ctx.exception.status_code, 503)
