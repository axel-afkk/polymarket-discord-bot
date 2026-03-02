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

LEADERBOARD_URL = "https://data-api.polymarket.com/v1/leaderboard"
TRADES_URL = "https://data-api.polymarket.com/trades"

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

top_wallets = []
last_trade_seen = {}

def short_wallet(w):
    return w[:6] + "…" + w[-4:] if w else w

async def fetch_leaderboard(session):
    params = {
        "category": CATEGORY,
        "timePeriod": TIMEPERIOD,
        "orderBy": ORDERBY,
        "limit": str(TOP_N_WALLETS),
    }
    async with session.get(LEADERBOARD_URL, params=params) as r:
        return await r.json()

async def fetch_latest_trade(session, wallet):
    params = {
        "user": wallet,
        "limit": 1,
        "offset": 0,
        "takerOnly": "true",
    }
    async with session.get(TRADES_URL, params=params) as r:
        data = await r.json()
        return data[0] if data else None

async def trade_loop(channel):
    global top_wallets
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                rows = await fetch_leaderboard(session)
                top_wallets = [row["proxyWallet"].lower() for row in rows if "proxyWallet" in row]

                for wallet in top_wallets:
                    trade = await fetch_latest_trade(session, wallet)
                    if not trade:
                        continue

                    tx = trade.get("transactionHash")
                    if not tx:
                        continue

                    if last_trade_seen.get(wallet) == tx:
                        continue

                    last_trade_seen[wallet] = tx

                    msg = (
                        f"🚨 **Top Trader Trade Detected**\n"
                        f"Wallet: `{short_wallet(wallet)}`\n"
                        f"Action: **{trade.get('side')}** {trade.get('outcome')}\n"
                        f"Market: {trade.get('title')}\n"
                        f"Size: {trade.get('size')} @ {trade.get('price')}\n"
                        f"Tx: {tx}"
                    )

                    await channel.send(msg)

            except Exception as e:
                await channel.send(f"Error: {e}")

            await asyncio.sleep(TRADE_POLL_SECONDS)

@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user}")
    channel = client.get_channel(ALERT_CHANNEL_ID)
    if channel:
        client.loop.create_task(trade_loop(channel))

client.run(DISCORD_TOKEN)
