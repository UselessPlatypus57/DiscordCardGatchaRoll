"""
Card roll bot (Mudae-style) with an approval workflow
-----------------------------------------------------
Setup:
    pip install -U discord.py python-dotenv
    .env file:
        DISCORD_TOKEN=your-token-here
        APPROVER_ID=123456789012345678     (Discord server admin ID, recommended)
        Aprover_Username = aprove_username (pref admin in charge of bot)
    python card_bot.py

IMPORTANT: this bot uses !prefix commands, so you must enable
"Message Content Intent" in the Developer Portal -> Bot -> Privileged Gateway Intents.

Project layout:
    card_bot.py
    .env
    cards/        <- images live here (created automatically)
    cards.db      <- card info + claims (created automatically)

Commands:
    !addcard "Name" <rarity> <points>   (attach an image)   anyone; needs approval
    !pending                            list cards waiting for approval
    !removecard "Name"                                      approver only
    !reset [@user]                                          approver only; clears roll + claim timers
    !clearclaims                                            approver only; unclaims every card (asks to confirm)
    !roll  (or !r)       roll a random card; owned cards show their owner, unowned ones can be claimed
    !collection [@user]  (or !mm)  show cards someone owns
    !leaderboard         (or !lb)  who has the most points
"""
import os
import random
import sqlite3
import time
import uuid
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------- settings
IMAGE_DIR = Path("cards")
IMAGE_DIR.mkdir(exist_ok=True)
DB_PATH = "cards.db"

ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
CLAIM_WINDOW = 60          # seconds a rolled card can be claimed
ROLLS_PER_HOUR = 10        # per user, per server (resets if the bot restarts)
CLAIM_COOLDOWN = 60      # seconds a user must wait after a successful claim before claiming again

# Who approves new cards. An ID is safer than a username (usernames can change),
# so set APPROVER_ID in .env. If it's missing, the bot falls back to the username.
APPROVER_USERNAME = os.environ["Aprover_Username"]
APPROVER_ID = int(os.environ["APPROVER_ID"]) if os.environ.get("APPROVER_ID") else None

# rarity -> (roll weight, embed color, emoji). Higher weight = more common.
RARITIES = {
    "common":    (60, 0x95A5A6, "⚪"),
    "uncommon":  (25, 0x2ECC71, "🟢"),
    "rare":      (10, 0x3498DB, "🔵"),
    "epic":      (4,  0x9B59B6, "🟣"),
    "legendary": (1,  0xF1C40F, "🟡"),
}

# ---------------------------------------------------------------- database
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS cards (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL,
        rarity       TEXT NOT NULL,
        points       INTEGER NOT NULL,
        filename     TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'approved',   -- pending | approved
        submitted_by INTEGER
    );
    CREATE TABLE IF NOT EXISTS claims (
        guild_id   INTEGER NOT NULL,
        card_id    INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        claimed_at INTEGER NOT NULL,
        PRIMARY KEY (guild_id, card_id)
    );
    CREATE TABLE IF NOT EXISTS claim_timers (
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        last_claim INTEGER NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    );
    """
)
# Upgrade older databases created before the approval feature existed.
# Any cards that were already there stay 'approved'.
_cols = {r["name"] for r in db.execute("PRAGMA table_info(cards)")}
if "status" not in _cols:
    db.execute("ALTER TABLE cards ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'")
if "submitted_by" not in _cols:
    db.execute("ALTER TABLE cards ADD COLUMN submitted_by INTEGER")
db.commit()

roll_times: dict[tuple[int, int], list[float]] = {}


# ---------------------------------------------------------------- helpers
def is_approver(user: discord.abc.User) -> bool:
    if APPROVER_ID is not None:
        return user.id == APPROVER_ID
    return user.name.lower() == APPROVER_USERNAME.lower()


def approver_mention() -> str:
    return f"<@{APPROVER_ID}>" if APPROVER_ID else f"**@{APPROVER_USERNAME}**"


def approver_only():
    async def predicate(ctx: commands.Context) -> bool:
        if not is_approver(ctx.author):
            raise commands.CheckFailure("approver_only")
        return True

    return commands.check(predicate)


def card_embed(
    card,
    claimed_by: int | None = None,
    expired: bool = False,
    owned_by: int | None = None,
) -> discord.Embed:
    _, color, emoji = RARITIES[card["rarity"]]
    embed = discord.Embed(title=card["name"], color=color)
    embed.description = f"{emoji} **{card['rarity'].title()}**\n💎 **{card['points']}** points"
    if claimed_by:
        embed.description += f"\n\nClaimed by <@{claimed_by}>!"
    elif expired:
        embed.description += "\n\n*Nobody claimed this one in time.*"
    elif owned_by:
        embed.description += f"\n\n🔒 Owned by <@{owned_by}>"
    embed.set_image(url=f"attachment://{card['filename']}")
    return embed


def pick_card():
    """Weighted pick: choose a rarity first, then a random approved card of it.
    Claimed cards stay in the pool; roll() shows who owns them."""
    rows = db.execute("SELECT * FROM cards WHERE status = 'approved'").fetchall()
    rows = [r for r in rows if (IMAGE_DIR / r["filename"]).exists()]
    if not rows:
        return None
    by_rarity: dict[str, list] = {}
    for r in rows:
        by_rarity.setdefault(r["rarity"], []).append(r)
    rarities = list(by_rarity)
    weights = [RARITIES[r][0] for r in rarities]
    chosen = random.choices(rarities, weights=weights, k=1)[0]
    return random.choice(by_rarity[chosen])


def rolls_left(guild_id: int, user_id: int) -> tuple[int, int]:
    """Returns (rolls remaining, seconds until the oldest roll expires)."""
    now = time.time()
    times = [t for t in roll_times.get((guild_id, user_id), []) if now - t < 3600]
    roll_times[(guild_id, user_id)] = times
    wait = int(3600 - (now - times[0])) if times else 0
    return ROLLS_PER_HOUR - len(times), wait


def claim_wait(guild_id: int, user_id: int) -> int:
    """Seconds until this user may claim again (0 = free to claim). Stored in the DB,
    so it survives bot restarts."""
    row = db.execute(
        "SELECT last_claim FROM claim_timers WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    if not row:
        return 0
    return max(0, int(row["last_claim"] + CLAIM_COOLDOWN - time.time()))


def fmt_wait(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


# ---------------------------------------------------------------- approval buttons
# DynamicItems encode their data in the custom_id, so these buttons keep working
# even if the bot restarts while a submission is waiting.
class ApprovalButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"card:(?P<action>approve|reject):(?P<id>\d+)",
):
    def __init__(self, action: str, card_id: int):
        approve = action == "approve"
        super().__init__(
            discord.ui.Button(
                label="Approve" if approve else "Reject",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"card:{action}:{card_id}",
            )
        )
        self.action, self.card_id = action, card_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        if not is_approver(interaction.user):
            return await interaction.response.send_message(
                "Only the approver can approve or reject cards.", ephemeral=True
            )

        card = db.execute("SELECT * FROM cards WHERE id = ?", (self.card_id,)).fetchone()
        if card is None or card["status"] != "pending":
            return await interaction.response.send_message("This submission was already handled.", ephemeral=True)

        submitter = card["submitted_by"]
        notify = discord.AllowedMentions(users=[discord.Object(submitter)]) if submitter else None

        if self.action == "approve":
            db.execute("UPDATE cards SET status = 'approved' WHERE id = ?", (self.card_id,))
            db.commit()
            await interaction.response.edit_message(
                content=f"✅ **{card['name']}** was approved by {interaction.user.mention} and is now in the roll pool.",
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if submitter:
                await interaction.followup.send(
                    f"<@{submitter}>, your card **{card['name']}** was approved!", allowed_mentions=notify
                )
        else:
            db.execute("DELETE FROM cards WHERE id = ?", (self.card_id,))
            db.commit()
            (IMAGE_DIR / card["filename"]).unlink(missing_ok=True)
            await interaction.response.edit_message(
                content=f"❌ **{card['name']}** was rejected by {interaction.user.mention}.",
                embed=None,
                attachments=[],
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if submitter:
                await interaction.followup.send(
                    f"<@{submitter}>, your card **{card['name']}** was rejected.", allowed_mentions=notify
                )


def approval_view(card_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ApprovalButton("approve", card_id))
    view.add_item(ApprovalButton("reject", card_id))
    return view


# ---------------------------------------------------------------- claim button
class ClaimView(discord.ui.View):
    def __init__(self, card, guild_id: int):
        super().__init__(timeout=CLAIM_WINDOW)
        self.card = card
        self.guild_id = guild_id
        self.message: discord.Message | None = None
        self.claimed = False

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, emoji="💖")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        wait = claim_wait(self.guild_id, interaction.user.id)
        if wait > 0:
            return await interaction.response.send_message(
                f"You're on claim cooldown. Try again in **{fmt_wait(wait)}**.", ephemeral=True
            )

        now = int(time.time())
        try:
            db.execute(
                "INSERT INTO claims (guild_id, card_id, user_id, claimed_at) VALUES (?, ?, ?, ?)",
                (self.guild_id, self.card["id"], interaction.user.id, now),
            )
        except sqlite3.IntegrityError:
            return await interaction.response.send_message("Too slow, that card is already claimed.", ephemeral=True)

        db.execute(
            "INSERT INTO claim_timers (guild_id, user_id, last_claim) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET last_claim = excluded.last_claim",
            (self.guild_id, interaction.user.id, now),
        )
        db.commit()

        self.claimed = True
        button.disabled = True
        await interaction.response.edit_message(
            embed=card_embed(self.card, claimed_by=interaction.user.id),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.stop()

    async def on_timeout(self):
        if self.claimed or self.message is None:
            return
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(embed=card_embed(self.card, expired=True), view=self)
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------- bot
intents = discord.Intents.default()
intents.message_content = True   # needed for !prefix commands


class CardBot(commands.Bot):
    async def setup_hook(self):
        self.add_dynamic_items(ApprovalButton)


bot = CardBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} | {len(bot.guilds)} server(s)")
    if APPROVER_ID is None:
        print(f"Note: APPROVER_ID not set, approving by username '{APPROVER_USERNAME}' (no real ping).")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        return await ctx.send("Only the approver can do that.")
    if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        return await ctx.send(f"Usage: `!{ctx.command.qualified_name} {ctx.command.signature}`")
    if isinstance(error, commands.NoPrivateMessage):
        return await ctx.send("Use this in a server.")
    raise error


@bot.command(name="addcard", help='Submit a card: !addcard "Name" <rarity> <points> + attach an image')
@commands.guild_only()
async def addcard(ctx: commands.Context, name: str, rarity: str, points: int):
    rarity = rarity.lower()
    if rarity not in RARITIES:
        return await ctx.send(f"Rarity must be one of: {', '.join(RARITIES)}")
    if points < 0:
        return await ctx.send("Points can't be negative.")
    if not ctx.message.attachments:
        return await ctx.send("Attach an image to your message.")

    attachment = ctx.message.attachments[0]
    ext = Path(attachment.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        return await ctx.send(f"Unsupported file type. Use: {', '.join(sorted(ALLOWED_EXT))}")
    if db.execute("SELECT 1 FROM cards WHERE LOWER(name) = LOWER(?)", (name,)).fetchone():
        return await ctx.send("A card with that name already exists (or is waiting for approval).")

    filename = f"{uuid.uuid4().hex}{ext}"
    await attachment.save(IMAGE_DIR / filename)

    # The approver's own submissions skip the queue.
    status = "approved" if is_approver(ctx.author) else "pending"
    cur = db.execute(
        "INSERT INTO cards (name, rarity, points, filename, status, submitted_by) VALUES (?, ?, ?, ?, ?, ?)",
        (name, rarity, points, filename, status, ctx.author.id),
    )
    db.commit()

    if status == "approved":
        return await ctx.send(f"Added **{name}** ({rarity}, {points} pts) to the library.")

    card = db.execute("SELECT * FROM cards WHERE id = ?", (cur.lastrowid,)).fetchone()
    ping = discord.AllowedMentions(users=[discord.Object(APPROVER_ID)] if APPROVER_ID else [])
    await ctx.send(
        content=f"{approver_mention()}, {ctx.author.mention} submitted a new card for approval:",
        embed=card_embed(card),
        file=discord.File(IMAGE_DIR / filename, filename=filename),
        view=approval_view(card["id"]),
        allowed_mentions=ping,
    )


@bot.command(name="pending", help="List cards waiting for approval")
@commands.guild_only()
async def pending(ctx: commands.Context):
    rows = db.execute("SELECT * FROM cards WHERE status = 'pending' ORDER BY id").fetchall()
    if not rows:
        return await ctx.send("No cards are waiting for approval.")
    lines = [
        f"`#{r['id']}` **{r['name']}** · {RARITIES[r['rarity']][2]} {r['rarity']} · {r['points']} pts"
        + (f" · by <@{r['submitted_by']}>" if r["submitted_by"] else "")
        for r in rows[:25]
    ]
    embed = discord.Embed(title="Pending approval", description="\n".join(lines), color=0xE67E22)
    embed.set_footer(text="The approver can use the buttons on the original submission message.")
    await ctx.send(embed=embed)


@bot.command(name="removecard", help='Remove a card: !removecard "Name" (approver only)')
@commands.guild_only()
@approver_only()
async def removecard(ctx: commands.Context, *, name: str):
    row = db.execute("SELECT * FROM cards WHERE LOWER(name) = LOWER(?)", (name,)).fetchone()
    if not row:
        return await ctx.send("No card with that name.")
    db.execute("DELETE FROM claims WHERE card_id = ?", (row["id"],))
    db.execute("DELETE FROM cards WHERE id = ?", (row["id"],))
    db.commit()
    (IMAGE_DIR / row["filename"]).unlink(missing_ok=True)
    await ctx.send(f"Removed **{row['name']}**.")


@bot.command(name="roll", aliases=["r"], help="Roll a random card")
@commands.guild_only()
async def roll(ctx: commands.Context):
    left, wait = rolls_left(ctx.guild.id, ctx.author.id)
    if left <= 0:
        return await ctx.send(f"Out of rolls! Next one in about **{wait // 60 + 1} min**.")

    card = pick_card()
    if card is None:
        return await ctx.send("No approved cards yet. Use `!addcard` to submit some.")

    roll_times[(ctx.guild.id, ctx.author.id)].append(time.time())

    file = discord.File(IMAGE_DIR / card["filename"], filename=card["filename"])
    owner = db.execute(
        "SELECT user_id FROM claims WHERE guild_id = ? AND card_id = ?",
        (ctx.guild.id, card["id"]),
    ).fetchone()

    if owner:
        # Already owned: show it (with the owner) but no Claim button.
        return await ctx.send(embed=card_embed(card, owned_by=owner["user_id"]), file=file)

    view = ClaimView(card, ctx.guild.id)
    view.message = await ctx.send(embed=card_embed(card), file=file, view=view)


class ClearClaimsView(discord.ui.View):
    """Confirmation prompt so claims can't be wiped by accident."""

    def __init__(self, guild_id: int):
        super().__init__(timeout=30)
        self.guild_id = guild_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_approver(interaction.user):
            await interaction.response.send_message("Only the approver can confirm this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, clear all claims", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        cur = db.execute("DELETE FROM claims WHERE guild_id = ?", (self.guild_id,))
        db.commit()
        await interaction.response.edit_message(
            content=f"🧹 Cleared **{cur.rowcount}** claims. Every card is unclaimed again.",
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", view=None)
        self.stop()

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content="Timed out. Nothing was changed.", view=None)
            except discord.HTTPException:
                pass


@bot.command(name="clearclaims", help="Unclaim every card in this server (approver only)")
@commands.guild_only()
@approver_only()
async def clearclaims(ctx: commands.Context):
    count = db.execute(
        "SELECT COUNT(*) AS n FROM claims WHERE guild_id = ?", (ctx.guild.id,)
    ).fetchone()["n"]
    if count == 0:
        return await ctx.send("No cards are claimed in this server.")

    view = ClearClaimsView(ctx.guild.id)
    view.message = await ctx.send(
        f"⚠️ This will unclaim **{count}** cards and empty everyone's collection in this server. "
        "The cards themselves stay in the library. Continue?",
        view=view,
    )


@bot.command(name="reset", help="Reset roll and claim timers (approver only)")
@commands.guild_only()
@approver_only()
async def reset(ctx: commands.Context, user: discord.Member | None = None):
    if user:
        db.execute(
            "DELETE FROM claim_timers WHERE guild_id = ? AND user_id = ?",
            (ctx.guild.id, user.id),
        )
        roll_times.pop((ctx.guild.id, user.id), None)
        text = f"Reset roll and claim timers for {user.mention}."
    else:
        db.execute("DELETE FROM claim_timers WHERE guild_id = ?", (ctx.guild.id,))
        for key in [k for k in roll_times if k[0] == ctx.guild.id]:
            del roll_times[key]
        text = "Reset roll and claim timers for **everyone** in this server."
    db.commit()
    await ctx.send(text, allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="collection", aliases=["mm"], help="Show a user's cards")
@commands.guild_only()
async def collection(ctx: commands.Context, user: discord.Member | None = None):
    user = user or ctx.author
    rows = db.execute(
        "SELECT c.name, c.rarity, c.points FROM claims cl JOIN cards c ON c.id = cl.card_id "
        "WHERE cl.guild_id = ? AND cl.user_id = ? ORDER BY c.points DESC",
        (ctx.guild.id, user.id),
    ).fetchall()
    if not rows:
        return await ctx.send(f"{user.display_name} doesn't own any cards yet.")

    total = sum(r["points"] for r in rows)
    lines = [f"{RARITIES[r['rarity']][2]} **{r['name']}** · {r['points']} pts" for r in rows[:25]]
    if len(rows) > 25:
        lines.append(f"...and {len(rows) - 25} more")
    embed = discord.Embed(
        title=f"{user.display_name}'s collection",
        description="\n".join(lines),
        color=0xE91E63,
    )
    embed.set_footer(text=f"{len(rows)} cards · {total} total points")
    await ctx.send(embed=embed)


@bot.command(name="leaderboard", aliases=["lb"], help="Top collectors by points")
@commands.guild_only()
async def leaderboard(ctx: commands.Context):
    rows = db.execute(
        "SELECT cl.user_id, SUM(c.points) AS pts, COUNT(*) AS n FROM claims cl "
        "JOIN cards c ON c.id = cl.card_id WHERE cl.guild_id = ? "
        "GROUP BY cl.user_id ORDER BY pts DESC LIMIT 10",
        (ctx.guild.id,),
    ).fetchall()
    if not rows:
        return await ctx.send("Nobody has claimed anything yet. Try `!roll`!")
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{r['user_id']}> · **{r['pts']}** pts ({r['n']} cards)"
        for i, r in enumerate(rows)
    ]
    await ctx.send(embed=discord.Embed(title="Leaderboard", description="\n".join(lines), color=0xF1C40F))


if __name__ == "__main__":
    bot.run(os.environ["DISCORD_TOKEN"])