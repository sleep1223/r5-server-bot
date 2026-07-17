import unittest

from fastapi_service.api.v1 import game_configs
from fastapi_service.services import game_config_service
from shared_lib.models import GameConfigPreset, Player, UserBinding
from tortoise import Tortoise

TORTOISE_TEST_CONFIG = {
    "connections": {"default": "sqlite://:memory:"},
    "apps": {"models": {"models": ["shared_lib.models"], "default_connection": "default"}},
    "use_tz": False,
    "timezone": "Asia/Shanghai",
}


class GameConfigPresetTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await Tortoise.init(config=TORTOISE_TEST_CONFIG)
        await Tortoise.generate_schemas()
        player = await Player.create(nucleus_id=1001, name="Aero Mouse")
        self.owner = await UserBinding.create(
            platform="qq",
            platform_uid="2001",
            player=player,
            app_key="owner-key",
        )
        other_player = await Player.create(nucleus_id=1002, name="Controller Master")
        self.other = await UserBinding.create(
            platform="qq",
            platform_uid="2002",
            player=other_player,
            app_key="other-key",
        )

    async def asyncTearDown(self) -> None:
        await Tortoise.close_connections()

    async def test_save_is_one_preset_per_creator_and_second_save_updates(self) -> None:
        first = await game_config_service.save_mine(
            self.owner,
            name="First",
            remark=None,
            source_game="apex",
            content='mouse_sensitivity "1.2"',
        )
        second = await game_config_service.save_mine(
            self.owner,
            name="Updated",
            remark="new value",
            source_game="r5",
            content='mouse_sensitivity "1.4"\ncl_fovScale "1.55"',
        )

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(await GameConfigPreset.filter(creator_id=self.owner.id).count(), 1)
        self.assertEqual(second["name"], "Updated")
        self.assertTrue(second["has_mouse"])
        self.assertTrue(second["has_fov"])

    async def test_search_matches_name_and_creator_case_insensitively(self) -> None:
        await game_config_service.save_mine(
            self.owner,
            name="Precision Pack",
            remark=None,
            source_game="apex",
            content='mouse_sensitivity "1"',
        )
        await game_config_service.save_mine(
            self.other,
            name="Smooth sticks",
            remark=None,
            source_game="r5",
            content='gamepad_aim_speed "4"',
        )

        by_name, name_total = await game_config_service.list_presets(
            page_size=20,
            offset=0,
            q="PRECISION",
        )
        by_creator, creator_total = await game_config_service.list_presets(
            page_size=20,
            offset=0,
            q="controller master",
        )

        self.assertEqual(name_total, 1)
        self.assertEqual(by_name[0]["creator_name"], "Aero Mouse")
        self.assertEqual(creator_total, 1)
        self.assertEqual(by_creator[0]["name"], "Smooth sticks")

    async def test_device_filters_include_mixed_presets_in_both_results(self) -> None:
        await game_config_service.save_mine(
            self.owner,
            name="Mixed",
            remark=None,
            source_game="apex",
            content='mouse_sensitivity "1"\ngamepad_aim_speed "4"\ncl_fovScale "1.5"',
        )

        mouse, mouse_total = await game_config_service.list_presets(
            page_size=20,
            offset=0,
            input_device="mouse_keyboard",
        )
        controller, controller_total = await game_config_service.list_presets(
            page_size=20,
            offset=0,
            input_device="controller",
        )

        self.assertEqual(mouse_total, 1)
        self.assertEqual(controller_total, 1)
        self.assertEqual(mouse[0]["id"], controller[0]["id"])

    async def test_public_list_omits_content_and_private_creator_fields(self) -> None:
        saved = await game_config_service.save_mine(
            self.owner,
            name="Public",
            remark=None,
            source_game="apex",
            content='mouse_sensitivity "1"',
        )

        rows, total = await game_config_service.list_presets(page_size=20, offset=0)
        detail = await game_config_service.get_preset(saved["id"])

        self.assertEqual(total, 1)
        self.assertNotIn("content", rows[0])
        assert detail is not None
        self.assertEqual(detail["content"], 'mouse_sensitivity "1"')
        for private_key in ("app_key", "platform", "platform_uid", "creator_id"):
            self.assertNotIn(private_key, rows[0])
            self.assertNotIn(private_key, detail)

    async def test_delete_mine_cannot_delete_another_creator_preset(self) -> None:
        other_saved = await game_config_service.save_mine(
            self.other,
            name="Other",
            remark=None,
            source_game="r5",
            content='gamepad_aim_speed "3"',
        )

        self.assertFalse(await game_config_service.delete_mine(self.owner.id))
        self.assertIsNotNone(await game_config_service.get_preset(other_saved["id"]))
        self.assertTrue(await game_config_service.delete_preset(other_saved["id"]))
        self.assertIsNone(await game_config_service.get_preset(other_saved["id"]))

    def test_parser_normalizes_valid_lines_and_computes_groups(self) -> None:
        parsed = game_config_service.parse_game_config_content(
            'mouse_sensitivity   "0.83"\r\n\r\ngamepad_aim_speed "4"\r\ncl_fovScale "1.55"'
        )

        self.assertEqual(
            parsed.content,
            'mouse_sensitivity "0.83"\ngamepad_aim_speed "4"\ncl_fovScale "1.55"',
        )
        self.assertTrue(parsed.has_mouse)
        self.assertTrue(parsed.has_controller)
        self.assertTrue(parsed.has_fov)

    def test_parser_rejects_unknown_duplicate_and_malicious_lines(self) -> None:
        invalid_contents = (
            'fps_max "144"',
            'mouse_sensitivity "1"\nmouse_sensitivity "2"',
            'mouse_sensitivity "1"; quit',
            '// mouse_sensitivity "1"',
            'mouse_sensitivity "nan"',
            'mouse_sensitivity "1\nquit"',
        )

        for content in invalid_contents:
            with self.subTest(content=content), self.assertRaises(
                game_config_service.GameConfigValidationError
            ):
                game_config_service.parse_game_config_content(content)

    async def test_invalid_source_game_is_rejected_before_storage(self) -> None:
        with self.assertRaises(game_config_service.GameConfigValidationError):
            await game_config_service.save_mine(
                self.owner,
                name="Invalid source",
                remark=None,
                source_game="titanfall",
                content='mouse_sensitivity "1"',
            )
        self.assertEqual(await GameConfigPreset.all().count(), 0)

    def test_mine_routes_are_registered_before_dynamic_id_routes(self) -> None:
        paths = [getattr(route, "path", "") for route in game_configs.router.routes]
        mine_indexes = [index for index, path in enumerate(paths) if path.endswith("/mine")]
        dynamic_indexes = [index for index, path in enumerate(paths) if path.endswith("/{preset_id}")]

        self.assertEqual(len(mine_indexes), 3)
        self.assertEqual(len(dynamic_indexes), 2)
        self.assertLess(max(mine_indexes), min(dynamic_indexes))


if __name__ == "__main__":
    unittest.main()
