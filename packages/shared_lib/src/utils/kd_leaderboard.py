from tortoise import connections

from shared_lib.models import Player


async def get_kd_leaderboard(player_name: str):
    """Print the KD ratio leaderboard for one player from daily stats."""
    player = await Player.filter(name=player_name).first()
    if not player:
        print(f"Error: Player '{player_name}' not found in the database.")
        return

    print(f"Generating KD Leaderboard for: {player.name}")
    print("=" * 60)

    rows = await connections.get("default").execute_query_dict(
        """
        SELECT
            s.opponent_id,
            p.name AS opponent_name,
            SUM(s.kills)::int AS kills,
            SUM(s.deaths)::int AS deaths
        FROM player_kill_daily_weapon_opponent_stats s
        LEFT JOIN players p ON p.id = s.opponent_id
        WHERE s.player_id = $1
          AND s.opponent_id IS NOT NULL
        GROUP BY s.opponent_id, p.name
        HAVING SUM(s.kills) > 0 OR SUM(s.deaths) > 0
        """,
        [player.id],
    )

    leaderboard = []
    for row in rows:
        kills = row["kills"] or 0
        deaths = row["deaths"] or 0
        leaderboard.append({
            "opponent": row["opponent_name"] or f"Unknown ({row['opponent_id']})",
            "kills": kills,
            "deaths": deaths,
            "kd": kills / max(1, deaths),
        })

    leaderboard.sort(key=lambda x: (x["kd"], -x["deaths"], x["kills"]), reverse=True)

    header = f"{'Opponent':<25} | {'Kills':<6} | {'Deaths':<6} | {'KD Ratio':<8}"
    print(header)
    print("-" * len(header))

    for entry in leaderboard:
        print(f"{entry['opponent']:<25} | {entry['kills']:<6} | {entry['deaths']:<6} | {entry['kd']:.2f}")

    if not leaderboard:
        print("No PvP data found for this player.")


async def get_global_kill_leaderboard(limit: int = 20):
    """Print the global leaderboard of players with the most kills from daily stats."""
    print(f"Generating Global Top {limit} Kill Leaderboard")
    print("=" * 60)

    rows = await connections.get("default").execute_query_dict(
        """
        SELECT
            s.player_id,
            p.name,
            SUM(s.kills)::int AS total_kills
        FROM player_kill_daily_weapon_opponent_stats s
        LEFT JOIN players p ON p.id = s.player_id
        GROUP BY s.player_id, p.name
        HAVING SUM(s.kills) > 0
        ORDER BY total_kills DESC
        LIMIT $1
        """,
        [limit],
    )

    header = f"{'#':<4} {'Player':<30} | {'Total Kills':<10}"
    print(header)
    print("-" * len(header))

    for rank, row in enumerate(rows, 1):
        name = row["name"] or f"Unknown ({row['player_id']})"
        print(f"{rank:<4} {name:<30} | {row['total_kills']:<10}")

    if not rows:
        print("No kill data found in the database.")
