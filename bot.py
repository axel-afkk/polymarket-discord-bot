import os
import asyncio
import aiohttp
import discord
from discord import app_commands

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ALERT_CHANNEL_ID = int(os.environ["ALERT_CHANNEL_ID"])

CATEGORY = os.environ.get("CATEGORY", "CRYPTO")
TIMEPERIOD = os.environ.get("TIMEPERIOD", "DAY")
ORDERBY = os.environ.get("ORDERBY", "PNL")
LIMIT = int(os.environ.get("LIMIT", "10"))

TOP_N_WALLETS = int(os.environ.get("TOP_N_WALLETS", "10"))
TRADE_POLL_SECONDS = int(os.environ.get("TRADE_POLL_SECONDS", "30"))

# Optional: only alert trades above this notional amount (size * price)
# Set to 0 to alert everything.
MIN_NOTIONAL_USD = float(os.environ.get("MIN_NOTIONAL_USD", "200"))

LEADERBOARD_URL = "https://data-api.polymarket.com/v1/leaderboard"
TRADES_URL = "https://data-api.polymarket.com/trades"

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

top_wallets = []
last_trade_seen = {}

def short_wallet(w: str) -> str:
    return w[:6] + "…" + w[-4:] if w and len(w) > 12 else w

async def fetch_leaderboard(session: aiohttp.ClientSession):
    params = {
        "category": CATEGORY,
        "timePeriod": TIMEPERIOD,
        "orderBy": ORDERBY,
        "limit": str(TOP_N_WALLETS),
    }
    async with session.get(LEADERBOARD_URL, params=params, timeout=20) as r:
        r.raise_for_status()
        return await r.json()

async def fetch_latest_trade(session: aiohttp.ClientSession, wallet: str):
    params = {
        "user": wallet,
        "limit": 1,
        "offset": 0,
        "takerOnly": "true",
    }
    async with session.get(TRADES_URL, params=params, timeout=20) as r:
        r.raise_for_status()
        data = await r.json()
        return data[0] if data else None

async def trade_loop(channel: discord.abc.Messageable):
    global top_wallets

    # backoff prevents hammering Discord or Polymarket when something goes wrong
    backoff = 5  # seconds, increases on errors up to 5 minutes

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # Pull current top wallets
                rows = await fetch_leaderboard(session)
                top_wallets = [row["proxyWallet"].lower() for row in rows if "proxyWallet" in row]

                # Check each wallet for a new trade
                for wallet in top_wallets:
                    trade = await fetch_latest_trade(session, wallet)
                    if not trade:
                        continue

                    # Unique id for trade (tx hash is best, timestamp fallback)
                    tx = trade.get("transactionHash") or str(trade.get("timestamp"))
                    if not tx:
                        continue

                    # Avoid duplicate alerts
                    if last_trade_seen.get(wallet) == tx:
                        continue

                    # Optional spam control: only alert bigger trades
                    try:
                        size = float(trade.get("size", 0) or 0)
                        price = float(trade.get("price", 0) or 0)
                        notional = size * price
                    except Exception:
                        notional = 0.0

                    if MIN_NOTIONAL_USD > 0 and notional < MIN_NOTIONAL_USD:
                        # Still record it so we don’t repeatedly re-check the same “small trade”
                        last_trade_seen[wallet] = tx
                        continue

                    last_trade_seen[wallet] = tx

                    msg = (
                        f"🚨 **Top Trader Trade Detected**\n"
                        f"Wallet: `{short_wallet(wallet)}`\n"
                        f"Action: **{trade.get('side')}** {trade.get('outcome')}\n"
                        f"Market: {trade.get('title')}\n"
                        f"Size: {trade.get('size')} @ {trade.get('price')}\n"
                        f"Notional (approx): `${notional:,.2f}`\n"
                        f"Tx: `{tx}`"
                    )

                    await channel.send(msg)

                    # Pace message sends to avoid Discord global rate limits
                    await asyncio.sleep(1.5)

                # success: reset backoff and sleep normally
                backoff = 5
                await asyncio.sleep(TRADE_POLL_SECONDS)

            except discord.errors.HTTPException as e:
                # If Discord rate-limits (429), slow down hard
                print("Discord HTTPException:", repr(e))
                await asyncio.sleep(60)
                backoff = min(backoff * 2, 300)

            except Exception as e:
                # IMPORTANT: do not send errors into Discord (can spam + trigger 429)
                print("trade_loop error:", repr(e))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user}")

    channel = client.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        print("ERROR: Could not find channel. Check ALERT_CHANNEL_ID and bot permissions.")
        return

    client.loop.create_task(trade_loop(channel))

client.run(DISCORD_TOKEN)
