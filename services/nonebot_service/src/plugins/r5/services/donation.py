import traceback

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message
from nonebot.exception import FinishedException
from nonebot.params import CommandArg

from ..api_client import api_client
from .common import r5_service

# Service definition
donation_service = r5_service.create_subservice("donation")
donation_add_service = donation_service.create_subservice("add")
donation_del_service = donation_service.create_subservice("delete")


# Commands
cmd_view = on_command("捐赠查看", aliases={"捐赠列表", "查看捐赠"}, priority=5, block=True)
cmd_add = on_command("捐赠新增", aliases={"新增捐赠", "添加捐赠"}, priority=5, block=True)
cmd_del = on_command("捐赠删除", aliases={"删除捐赠"}, priority=5, block=True)


@cmd_view.handle()
async def handle_view():
    try:
        resp = await api_client.get_donations()
        res = resp.json()

        if res.get("code") != "0000":
            await cmd_view.finish(f"❌ 获取失败: {res.get('msg')}")

        donations = res.get("data", [])
        if not donations:
            await cmd_view.finish("ℹ️ 暂无捐赠记录。")

        msg = "💰 捐赠列表\n❤️ 捐赠地址 https://afdian.com/a/Sleep1223\n"
        # API returns: id, donor_name, amount, currency, message, created_at
        for idx, d in enumerate(donations, 1):
            # Format date: 2023-10-27T10:00:00+08:00 -> 2023-10-27
            date_str = d.get("created_at", "")[:10]
            note = d.get("message") or "无"
            if len(note) > 20:
                note = note[:20] + "..."
            msg += f"{idx}. [{date_str}] {d['donor_name']} 捐赠了 {d['amount']} {d['currency']} (备注: {note})\n"

        await cmd_view.finish(msg.strip())

    except FinishedException:
        raise
    except Exception as e:
        traceback.print_exc()
        await cmd_view.finish(f"❌ 执行出错: {e}")


@cmd_add.handle()
@donation_add_service.patch_handler()
async def handle_add(args: Message = CommandArg()):
    content = args.extract_plain_text().strip()
    if not content:
        await cmd_add.finish("⚠️ 用法: /捐赠新增 <名字> <金额> [备注]")

    parts = content.split()
    if len(parts) < 2:
        await cmd_add.finish("⚠️ 参数不足。用法: /捐赠新增 <名字> <金额> [备注]")

    name = parts[0]
    amount_str = parts[1]
    note = parts[2] if len(parts) > 2 else None

    try:
        amount = float(amount_str)
    except ValueError:
        await cmd_add.finish("⚠️ 金额必须是数字。")

    try:
        resp = await api_client.create_donation(donor_name=name, amount=amount, message=note)
        res = resp.json()

        if res.get("code") == "0000":
            d = res.get("data")
            date_str = d.get("created_at", "")[:10]
            note_display = d.get("message") or "无"
            await cmd_add.finish(f"✅ 已添加捐赠记录：\n{date_str} {d['donor_name']} {d['amount']} {d['currency']} {note_display}")
        else:
            await cmd_add.finish(f"❌ 添加失败: {res.get('msg')}")

    except FinishedException:
        raise
    except Exception as e:
        traceback.print_exc()
        await cmd_add.finish(f"❌ 执行出错: {e}")


@cmd_del.handle()
@donation_del_service.patch_handler()
async def handle_del(args: Message = CommandArg()):
    content = args.extract_plain_text().strip()
    if not content or not content.isdigit():
        await cmd_del.finish("⚠️ 用法: /捐赠删除 <序号> (请先使用 /捐赠查看 获取序号)")

    idx = int(content)

    try:
        # Fetch list to map index to ID
        resp = await api_client.get_donations()
        res = resp.json()

        if res.get("code") != "0000":
            await cmd_del.finish(f"❌ 获取列表失败: {res.get('msg')}")

        donations = res.get("data", [])
        if idx < 1 or idx > len(donations):
            await cmd_del.finish("⚠️ 序号无效。")

        target = donations[idx - 1]
        donation_id = target["id"]

        # Delete
        del_resp = await api_client.delete_donation(donation_id)
        del_res = del_resp.json()

        if del_res.get("code") == "0000":
            await cmd_del.finish(f"✅ 已删除捐赠记录：{target['donor_name']} - {target['amount']}")
        else:
            await cmd_del.finish(f"❌ 删除失败: {del_res.get('msg')}")

    except FinishedException:
        raise
    except Exception as e:
        traceback.print_exc()
        await cmd_del.finish(f"❌ 执行出错: {e}")
