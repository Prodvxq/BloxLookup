import asyncio
import os

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


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


@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user}")


@tree.command(name="robloxuser", description="Look up a Roblox player's public profile")
@app_commands.describe(username="The Roblox username to look up")
async def robloxuser(interaction: discord.Interaction, username: str):
    await interaction.response.defer()

    async with aiohttp.ClientSession() as session:
        # Step 1: turn the username into a Roblox user ID.
        async with session.post(
            "https://users.roblox.com/v1/usernames/users",
            json={"usernames": [username], "excludeBannedUsers": False},
        ) as resp:
            data = await resp.json()

        if not data.get("data"):
            await interaction.followup.send(f"I couldn't find a player named **{username}**.")
            return

        user_id = data["data"][0]["id"]

        # Step 2: grab everything else at the same time (much faster than one by one).
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
    print("AVATAR:", avatar)
    if not profile:
        await interaction.followup.send("Roblox didn't respond. Try again in a moment.")
        return

    # Step 3: build the profile card.
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


client.run(os.environ["DISCORD_TOKEN"])