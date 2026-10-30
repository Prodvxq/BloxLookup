import asyncio
import os
import re
import sqlite3
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

DB_PATH = "bloxlookup.db"   # the database file, created automatically next to this script
SAMPLE_MINUTES = 10         # how often tracked games get a new player-count reading
MAX_TRACKED = 25            # cap so a free host (and Roblox's API) isn't overloaded
KEEP_DAYS = 30              # readings older than this get deleted to keep the file small


# ---------- helpers ----------

async def get_json(session, url):
    """Fetch a URL and return its JSON, or None if anything goes wrong."""
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.json()
    except aiohttp.ClientError:
        pass
    return None


def fmt_count(data):
    """Turn {"count": 1234} into "1,234", or "N/A" if the lookup failed."""
    return f"{data['count']:,}" if data else "N/A"


def extract_id(text, pattern):
    """Pull an ID out of a link (using the pattern) or accept a plain number."""
    text = text.strip()
    match = re.search(pattern, text)
    if match:
        return int(match.group(1))
    if text.isdigit():
        return int(text)
    return None


def run_db(query, params=(), fetch=False):
    """Run one SQL command on the database. Returns rows if fetch=True."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(query, params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


def init_db():
    """Create the tables the first time the bot runs."""
    run_db(
        "CREATE TABLE IF NOT EXISTS tracked ("
        "universe_id INTEGER PRIMARY KEY, place_id INTEGER NOT NULL, name TEXT NOT NULL)"
    )
    run_db(
        "CREATE TABLE IF NOT EXISTS samples ("
        "universe_id INTEGER NOT NULL, ts INTEGER NOT NULL, "
        "playing INTEGER NOT NULL, visits INTEGER NOT NULL)"
    )
    run_db("CREATE INDEX IF NOT EXISTS idx_samples ON samples (universe_id, ts)")


def sparkline(values, width=24):
    """Turn a list of numbers into a tiny text graph like ▁▃▅█▆▃."""
    if len(values) > width:
        step = len(values) / width
        values = [values[int(i * step)] for i in range(width)]
    bars = "▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    if hi == lo:
        return bars[0] * len(values)
    return "".join(bars[int((v - lo) / (hi - lo) * (len(bars) - 1))] for v in values)


init_db()


# ---------- background task: record player counts ----------

@tasks.loop(minutes=SAMPLE_MINUTES)
async def sampler():
    try:
        rows = run_db("SELECT universe_id FROM tracked", fetch=True)
        if not rows:
            return
        ids = ",".join(str(r["universe_id"]) for r in rows)
        async with aiohttp.ClientSession() as session:
            info = await get_json(session, f"https://games.roblox.com/v1/games?universeIds={ids}")
        if not info or not info.get("data"):
            return
        now = int(time.time())
        for g in info["data"]:
            run_db(
                "INSERT INTO samples (universe_id, ts, playing, visits) VALUES (?, ?, ?, ?)",
                (g["id"], now, g.get("playing", 0), g.get("visits", 0)),
            )
        run_db("DELETE FROM samples WHERE ts < ?", (now - KEEP_DAYS * 86400,))
    except Exception as e:  # a crash here would silently stop the loop, so log it instead
        print("Sampler error:", e)


@client.event
async def on_ready():
    await tree.sync()
    if not sampler.is_running():
        sampler.start()
    print(f"Logged in as {client.user}")


# ---------- /robloxuser ----------

@tree.command(name="robloxuser", description="Look up a Roblox player's public profile")
@app_commands.describe(username="The Roblox username to look up")
async def robloxuser(interaction: discord.Interaction, username: str):
    await interaction.response.defer()

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://users.roblox.com/v1/usernames/users",
            json={"usernames": [username], "excludeBannedUsers": False},
        ) as resp:
            data = await resp.json()

        if not data.get("data"):
            await interaction.followup.send(f"I couldn't find a player named **{username}**.")
            return

        user_id = data["data"][0]["id"]

        profile, friends, followers, following, avatar, groups = await asyncio.gather(
            get_json(session, f"https://users.roblox.com/v1/users/{user_id}"),
            get_json(session, f"https://friends.roblox.com/v1/users/{user_id}/friends/count"),
            get_json(session, f"https://friends.roblox.com/v1/users/{user_id}/followers/count"),
            get_json(session, f"https://friends.roblox.com/v1/users/{user_id}/followings/count"),
            get_json(
                session,
                f"https://thumbnails.roblox.com/v1/users/avatar-headshot"
                f"?userIds={user_id}&size=420x420&format=Png&isCircular=false",
            ),
            get_json(session, f"https://groups.roblox.com/v2/users/{user_id}/groups/roles"),
        )

    if not profile:
        await interaction.followup.send("Roblox didn't respond. Try again in a moment.")
        return

    description = (profile.get("description") or "No description.")[:300]
    name = profile["displayName"]
    if profile.get("hasVerifiedBadge"):
        name += " ✔"
    if profile.get("isBanned"):
        name += " (banned)"

    embed = discord.Embed(
        title=name,
        url=f"https://www.roblox.com/users/{user_id}/profile",
        description=description,
    )
    embed.add_field(name="Username", value=profile["name"])
    embed.add_field(name="User ID", value=str(user_id))
    embed.add_field(name="Joined Roblox", value=profile["created"][:10])
    embed.add_field(name="Friends", value=fmt_count(friends))
    embed.add_field(name="Followers", value=fmt_count(followers))
    embed.add_field(name="Following", value=fmt_count(following))

    if groups and groups.get("data"):
        names = [g["group"]["name"] for g in groups["data"]]
        shown = ", ".join(names[:5])
        extra = f" (+{len(names) - 5} more)" if len(names) > 5 else ""
        embed.add_field(name=f"Groups ({len(names)})", value=shown + extra, inline=False)

    if avatar and avatar.get("data") and avatar["data"][0].get("imageUrl"):
        embed.set_thumbnail(url=avatar["data"][0]["imageUrl"])

    await interaction.followup.send(embed=embed)


# ---------- /game ----------

@tree.command(name="game", description="Look up a Roblox game's stats")
@app_commands.describe(game="A Roblox game link or its ID number")
async def game_lookup(interaction: discord.Interaction, game: str):
    await interaction.response.defer()

    place_id = extract_id(game, r"games/(\d+)")
    if place_id is None:
        await interaction.followup.send(
            "Give me a Roblox game link (like roblox.com/games/920587237) or its ID number."
        )
        return

    async with aiohttp.ClientSession() as session:
        # A game link has a "place" ID. Most game info is stored under a "universe" ID.
        universe = await get_json(
            session, f"https://apis.roblox.com/universes/v1/places/{place_id}/universe"
        )
        if not universe or not universe.get("universeId"):
            await interaction.followup.send("I couldn't find that game. Check the link or ID.")
            return

        uid = universe["universeId"]
        info, votes, icon = await asyncio.gather(
            get_json(session, f"https://games.roblox.com/v1/games?universeIds={uid}"),
            get_json(session, f"https://games.roblox.com/v1/games/votes?universeIds={uid}"),
            get_json(
                session,
                f"https://thumbnails.roblox.com/v1/games/icons"
                f"?universeIds={uid}&size=256x256&format=Png&isCircular=false",
            ),
        )

    if not info or not info.get("data"):
        await interaction.followup.send("Roblox didn't return that game's info. Try again in a moment.")
        return

    g = info["data"][0]
    creator = (g.get("creator") or {}).get("name", "Unknown")

    embed = discord.Embed(
        title=g.get("name", "Unknown game"),
        url=f"https://www.roblox.com/games/{place_id}",
        description=(g.get("description") or "No description.")[:300],
    )
    embed.add_field(name="Playing now", value=f"{g.get('playing', 0):,}")
    embed.add_field(name="Visits", value=f"{g.get('visits', 0):,}")
    embed.add_field(name="Favorites", value=f"{g.get('favoritedCount', 0):,}")

    if votes and votes.get("data"):
        up = votes["data"][0].get("upVotes", 0)
        down = votes["data"][0].get("downVotes", 0)
        embed.add_field(name="Votes", value=f"{up:,} 👍 / {down:,} 👎")

    embed.add_field(name="Creator", value=creator)
    embed.add_field(name="Created", value=(g.get("created") or "N/A")[:10])
    embed.add_field(name="Last updated", value=(g.get("updated") or "N/A")[:10])

    if icon and icon.get("data") and icon["data"][0].get("imageUrl"):
        embed.set_thumbnail(url=icon["data"][0]["imageUrl"])

    await interaction.followup.send(embed=embed)


# ---------- /group ----------

@tree.command(name="group", description="Look up a Roblox group (community)")
@app_commands.describe(group="A Roblox group link or its ID number")
async def group_lookup(interaction: discord.Interaction, group: str):
    await interaction.response.defer()

    group_id = extract_id(group, r"(?:groups|communities)/(\d+)")
    if group_id is None:
        await interaction.followup.send("Give me a Roblox group link or its ID number.")
        return

    async with aiohttp.ClientSession() as session:
        info, icon = await asyncio.gather(
            get_json(session, f"https://groups.roblox.com/v1/groups/{group_id}"),
            get_json(
                session,
                f"https://thumbnails.roblox.com/v1/groups/icons"
                f"?groupIds={group_id}&size=420x420&format=Png&isCircular=false",
            ),
        )

    if not info or not info.get("name"):
        await interaction.followup.send("I couldn't find that group. Check the link or ID.")
        return

    owner_data = info.get("owner") or {}
    owner = owner_data.get("displayName") or owner_data.get("username") or "No owner"

    name = info["name"]
    if info.get("hasVerifiedBadge"):
        name += " ✔"

    embed = discord.Embed(
        title=name,
        url=f"https://www.roblox.com/communities/{group_id}",
        description=(info.get("description") or "No description.")[:300],
    )
    embed.add_field(name="Members", value=f"{info.get('memberCount', 0):,}")
    embed.add_field(name="Owner", value=owner)
    embed.add_field(name="Group ID", value=str(group_id))
    embed.add_field(
        name="Joining",
        value="Open to everyone" if info.get("publicEntryAllowed") else "Approval required",
    )

    if icon and icon.get("data") and icon["data"][0].get("imageUrl"):
        embed.set_thumbnail(url=icon["data"][0]["imageUrl"])

    await interaction.followup.send(embed=embed)


# ---------- /track ----------

@tree.command(name="track", description="Start recording a game's player count over time")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
@app_commands.describe(game="A Roblox game link or its ID number")
async def track(interaction: discord.Interaction, game: str):
    await interaction.response.defer()

    place_id = extract_id(game, r"games/(\d+)")
    if place_id is None:
        await interaction.followup.send("Give me a Roblox game link or its ID number.")
        return

    async with aiohttp.ClientSession() as session:
        universe = await get_json(
            session, f"https://apis.roblox.com/universes/v1/places/{place_id}/universe"
        )
        if not universe or not universe.get("universeId"):
            await interaction.followup.send("I couldn't find that game. Check the link or ID.")
            return
        uid = universe["universeId"]
        info = await get_json(session, f"https://games.roblox.com/v1/games?universeIds={uid}")

    if not info or not info.get("data"):
        await interaction.followup.send("Roblox didn't return that game's info. Try again in a moment.")
        return

    g = info["data"][0]

    if run_db("SELECT 1 FROM tracked WHERE universe_id = ?", (uid,), fetch=True):
        await interaction.followup.send(f"I'm already tracking **{g['name']}**. Use `/stats` to see it.")
        return

    total = run_db("SELECT COUNT(*) AS n FROM tracked", fetch=True)[0]["n"]
    if total >= MAX_TRACKED:
        await interaction.followup.send(f"I can only track {MAX_TRACKED} games at the moment.")
        return

    run_db(
        "INSERT INTO tracked (universe_id, place_id, name) VALUES (?, ?, ?)",
        (uid, place_id, g["name"]),
    )
    run_db(
        "INSERT INTO samples (universe_id, ts, playing, visits) VALUES (?, ?, ?, ?)",
        (uid, int(time.time()), g.get("playing", 0), g.get("visits", 0)),
    )
    await interaction.followup.send(
        f"Now tracking **{g['name']}**. I record its player count every {SAMPLE_MINUTES} minutes. "
        f"Check back later and use `/stats` to see the trend."
    )


# ---------- /stats ----------

@tree.command(name="stats", description="See a tracked game's player count over the last 24 hours")
@app_commands.describe(game="The same game link or ID you used with /track")
async def stats(interaction: discord.Interaction, game: str):
    await interaction.response.defer()

    place_id = extract_id(game, r"games/(\d+)")
    if place_id is None:
        await interaction.followup.send("Give me a Roblox game link or its ID number.")
        return

    found = run_db("SELECT * FROM tracked WHERE place_id = ?", (place_id,), fetch=True)
    if not found:
        await interaction.followup.send("I'm not tracking that game yet. Use `/track` first.")
        return

    uid = found[0]["universe_id"]
    name = found[0]["name"]
    since = int(time.time()) - 86400
    rows = run_db(
        "SELECT ts, playing FROM samples WHERE universe_id = ? AND ts >= ? ORDER BY ts",
        (uid, since),
        fetch=True,
    )

    if len(rows) < 2:
        await interaction.followup.send(
            f"I only have {len(rows)} reading for **{name}** so far. "
            f"Check back in about {SAMPLE_MINUTES} minutes."
        )
        return

    values = [r["playing"] for r in rows]
    hours = (rows[-1]["ts"] - rows[0]["ts"]) / 3600

    embed = discord.Embed(
        title=f"{name}: player count",
        url=f"https://www.roblox.com/games/{place_id}",
        description=f"```{sparkline(values)}```",
    )
    embed.add_field(name="Now", value=f"{values[-1]:,}")
    embed.add_field(name="Peak", value=f"{max(values):,}")
    embed.add_field(name="Lowest", value=f"{min(values):,}")
    embed.add_field(name="Average", value=f"{sum(values) // len(values):,}")
    embed.add_field(name="Covers", value=f"{hours:.1f} hours")
    embed.add_field(name="Last reading", value=f"<t:{rows[-1]['ts']}:R>")
    embed.set_footer(text=f"{len(values)} readings, one every {SAMPLE_MINUTES} minutes")

    await interaction.followup.send(embed=embed)


client.run(os.environ["DISCORD_TOKEN"])
