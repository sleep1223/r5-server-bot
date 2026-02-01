from shared_lib.models import Player, PlayerKilled
from tortoise.functions import Count


async def get_kd_leaderboard(player_name: str):
    """
    Query and print the KD ratio leaderboard for a specific player against other players.
    """
    # 1. Find the target player
    player = await Player.filter(name=player_name).first()
    if not player:
        print(f"Error: Player '{player_name}' not found in the database.")
        return

    print(f"Generating KD Leaderboard for: {player.name}")
    print("=" * 60)

    # 2. Get Kills: Where target player is the attacker
    # Group by victim
    kills_query = (
        PlayerKilled
        .filter(attacker=player)
        .annotate(count=Count("id"))
        .group_by("victim_id")  # Group by ID to be safe, though FK field works
        .values("victim__name", "count")
    )
    kills_data = await kills_query

    # 3. Get Deaths: Where target player is the victim
    # Group by attacker
    deaths_query = PlayerKilled.filter(victim=player).annotate(count=Count("id")).group_by("attacker_id").values("attacker__name", "count")
    deaths_data = await deaths_query

    # 4. Aggregate data
    stats = {}

    for k in kills_data:
        name = k["victim__name"]
        if not name:
            name = "Unknown"

        if name not in stats:
            stats[name] = {"kills": 0, "deaths": 0}
        stats[name]["kills"] = k["count"]

    for d in deaths_data:
        name = d["attacker__name"]
        if not name:
            name = "Unknown"

        if name not in stats:
            stats[name] = {"kills": 0, "deaths": 0}
        stats[name]["deaths"] = d["count"]

    # 5. Calculate KD and Format
    leaderboard = []
    for opponent, data in stats.items():
        k = data["kills"]
        d = data["deaths"]

        # Standard KD calculation: K / (D if D > 0 else 1)
        # This treats 0 deaths as 1 for the ratio, preventing division by zero
        # but maintaining a readable scale.
        kd_ratio = k / max(1, d)

        leaderboard.append({"opponent": opponent, "kills": k, "deaths": d, "kd": kd_ratio})

    # 6. Sort
    # Sort by:
    # 1. KD Ratio (descending)
    # 2. Deaths (ascending) - Fewer deaths is better for same KD
    # 3. Kills (descending) - More kills is better if KD and Deaths are same (unlikely if KD same and Deaths same, Kills must be same)
    leaderboard.sort(key=lambda x: (x["kd"], -x["deaths"], x["kills"]), reverse=True)

    # 7. Output
    header = f"{'Opponent':<25} | {'Kills':<6} | {'Deaths':<6} | {'KD Ratio':<8}"
    print(header)
    print("-" * len(header))

    for entry in leaderboard:
        print(f"{entry['opponent']:<25} | {entry['kills']:<6} | {entry['deaths']:<6} | {entry['kd']:.2f}")

    if not leaderboard:
        print("No PvP data found for this player.")


async def get_global_kill_leaderboard(limit: int = 20):
    """
    Query and print the global leaderboard of players with the most kills.
    """
    print(f"Generating Global Top {limit} Kill Leaderboard")
    print("=" * 60)

    # Group by attacker and count kills
    # We filter out null attackers just in case
    # Use attacker_id to check for null FK
    leaderboard_query = PlayerKilled.filter(attacker_id__isnull=False).annotate(total_kills=Count("id")).group_by("attacker_id").values("attacker__name", "total_kills")

    results = await leaderboard_query

    # Sort by total_kills descending
    results.sort(key=lambda x: x["total_kills"], reverse=True)

    # Take top N
    top_results = results[:limit]

    header = f"{'#':<4} {'Player':<30} | {'Total Kills':<10}"
    print(header)
    print("-" * len(header))

    for rank, entry in enumerate(top_results, 1):
        name = entry["attacker__name"] or "Unknown"
        kills = entry["total_kills"]
        print(f"{rank:<4} {name:<30} | {kills:<10}")

    if not results:
        print("No kill data found in the database.")
