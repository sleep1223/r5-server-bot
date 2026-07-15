import asyncio

from shared_lib.database import close_db, init_db
from shared_lib.models import IpInfo, Player
from shared_lib.utils.ip import resolve_ips_batch


async def main() -> None:
    await init_db(generate_schemas=False)

    try:
        infos = await IpInfo.all()
        players = await Player.filter(ip__isnull=False).all()

        existing_by_ip = {info.ip: info for info in infos}
        all_ips = set(existing_by_ip)
        all_ips.update(player.ip for player in players if player.ip)

        print(f"准备重新解析 {len(all_ips)} 个 IP...")
        resolved = resolve_ips_batch(sorted(all_ips))

        updated_infos = []
        new_infos = []
        for ip in all_ips:
            data = resolved.get(ip)
            country = data.get("country", "") if data else ""
            region = data.get("region", "") if data else ""
            is_resolved = data is not None

            info = existing_by_ip.get(ip)
            if info:
                info.country = country
                info.region = region
                info.is_resolved = is_resolved
                updated_infos.append(info)
            else:
                new_infos.append(IpInfo(ip=ip, country=country, region=region, is_resolved=is_resolved))

        if updated_infos:
            await IpInfo.bulk_update(updated_infos, fields=["country", "region", "is_resolved"], batch_size=500)
        if new_infos:
            await IpInfo.bulk_create(new_infos, batch_size=500)

        for player in players:
            data = resolved.get(player.ip) if player.ip else None
            player.country = data.get("country", "") if data else ""
            player.region = data.get("region", "") if data else ""

        if players:
            await Player.bulk_update(players, fields=["country", "region"], batch_size=500)

        print(f"同步完成：成功解析 {len(resolved)}/{len(all_ips)}，更新 IpInfo {len(updated_infos)} 条，新增 {len(new_infos)} 条，更新玩家 {len(players)} 条。")
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
