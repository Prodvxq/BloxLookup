import asyncio
import os
import re

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


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


@client.event
async def on_ready():
    await tree.sync()
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


client.run(os.environ["DISCORD_TOKEN"])