import os
import re
import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

import aiohttp
import discord


DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ALERT_CHANNEL_ID = int(os.environ["ALERT_CHANNEL_ID"])

CATEGORY = os.environ.get("CATEGORY", "CRYPTO")
TIMEPERIOD = os.environ.get("TIMEPERIOD", "DAY")
ORDERBY = os.environ.get("ORDERBY", "PNL")
TOP_N_WALLETS = int(os.environ.get("TOP_N_WALLETS", "10"))

TRADE_POLL_SECONDS = float(os.environ.get("TRADE_POLL_SECONDS", "5"))
LEADERBOARD_REFRESH_SECONDS = int(os.environ.get("LEADERBOARD_REFRESH_SECONDS", "300"))
MARKET_REFRESH_SECONDS = int(os.environ.get("MARKET_REFRESH_SECONDS", "60"))

MIN_NOTIONAL_USD = float(os.environ.get("MIN_NOTIONAL_USD", "200"))
TAKER_ONLY = os.environ.get("TAKER_ONLY", "true").lower() == "true"

TRACK_15M = os.environ.get("TRACK_15M", "true").lower() == "true"
TRACK_HOURLY = os.environ.get("TRACK_HOURLY", "true").lower() == "true"

# Discord send controls
DISCORD_SEND_DELAY_SECONDS = float(os.environ.get("DISCORD_SEND_DELAY_SECONDS", "1.2"))
DISCORD_BATCH_WINDOW_SECONDS = float(os.environ.get("DISCORD_BATCH_WINDOW_SECONDS", "2.0"))
DISCORD_MAX_ALERTS_PER_MESSAGE = int(os.environ.get("DISCORD_MAX_ALERTS_PER_MESSAGE", "5"))
STARTUP_GRACE_SECONDS = int(os.environ.get("STARTUP_GRACE_SECONDS", "15"))

LEADERBOARD_URL = "https://data-api.polymarket.com/v1/leaderboard"
TRADES_URL = "https://data-api.polymarket.com/trades"
SEARCH_URL = "https://gamma-api.polymarket.com/public-search"

intents = discord.Intents.default()
client = discord.Client(intents=intents)

top_wallets: List[str] = []
watched_markets: Dict[str, dict] = {}
seen_trade_keys: Set[str] = set()
seeded_wallets: Set[str] = set()

alert_queue: asyncio.Queue = asyncio.Queue()
startup_time = datetime.now(timezone.utc)


def short_wallet(w: str) -> str:
    return w[:6] + "…" + w[-4:] if w and len(w) > 12 else w


def parse_float(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def parse_trade_timestamp(trade: dict) -> Optional[datetime]:
    candidates = [
        trade.get("timestamp"),
        trade.get("createdAt"),
        trade.get("time"),
    ]

    for ts in candidates:
        if ts is None:
            continue

        if isinstance(ts, (int, float)):
            if ts > 10_000_000_000:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)

        if isinstance(ts, str) and ts.isdigit():
            n = int(ts)
            if n > 10_000_000_000:
                n = n / 1000.0
            return datetime.fromtimestamp(n, tz=timezone.utc)

        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                pass

    return None


def build_trade_key(trade: dict) -> str:
    parts = [
        str(trade.get("id") or ""),
        str(trade.get("transactionHash") or ""),
        str(trade.get("market") or trade.get("conditionId") or ""),
        str(trade.get("side") or ""),
        str(trade.get("outcome") or ""),
        str(trade.get("price") or ""),
        str(trade.get("size") or ""),
        str(trade.get("timestamp") or trade.get("createdAt") or ""),
        str((trade.get("user") or trade.get("proxyWallet") or "")).lower(),
    ]
    return "|".join(parts)


_TIME_RANGE_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?:AM|PM))-(?P<end>\d{1,2}:\d{2}(?:AM|PM))\s*ET",
    re.IGNORECASE,
)


def duration_minutes_from_title(title: str) -> Optional[int]:
    if not title:
        return None

    m = _TIME_RANGE_RE.search(title)
    if not m:
        return None

    try:
        start = datetime.strptime(m.group("start").upper(), "%I:%M%p")
        end = datetime.strptime(m.group("end").upper(), "%I:%M%p")
    except Exception:
        return None

    start_m = start.hour * 60 + start.minute
    end_m = end.hour * 60 + end.minute

    if end_m < start_m:
        end_m += 24 * 60

    return end_m - start_m


def market_matches_window(title: str) -> Tuple[bool, Optional[int]]:
    mins = duration_minutes_from_title(title)
    if mins is None:
        return False, None

    ok = (TRACK_15M and mins == 15) or (TRACK_HOURLY and mins == 60)
    return ok, mins


async def safe_get_json(session: aiohttp.ClientSession, url: str, **kwargs):
    async with session.get(url, **kwargs) as r:
        text = await r.text()
        if r.status >= 400:
            raise RuntimeError(f"GET {url} failed {r.status}: {text[:400]}")
        return await r.json()


async def fetch_leaderboard(session: aiohttp.ClientSession) -> List[str]:
    params = {
        "category": CATEGORY,
        "timePeriod": TIMEPERIOD,
        "orderBy": ORDERBY,
        "limit": str(TOP_N_WALLETS),
    }
    data = await safe_get_json(session, LEADERBOARD_URL, params=params, timeout=20)
    return [row["proxyWallet"].lower() for row in data if row.get("proxyWallet")]


async def fetch_btc_markets(session: aiohttp.ClientSession) -> Dict[str, dict]:
    params = {
        "q": "Bitcoin Up or Down",
        "limit_per_type": "25",
        "search_profiles": "false",
        "search_tags": "false",
        "events_status": "active",
        "keep_closed_markets": "0",
        "cache": "true",
    }
    data = await safe_get_json(session, SEARCH_URL, params=params, timeout=20)

    markets: Dict[str, dict] = {}

    for event in data.get("events", []) or []:
        for market in event.get("markets", []) or []:
            title = (
                market.get("question")
                or market.get("title")
                or event.get("title")
                or ""
            )

            if "bitcoin up or down" not in title.lower():
                continue

            match, mins = market_matches_window(title)
            if not match:
                continue

            condition_id = market.get("conditionId")
            if not condition_id:
                continue

            markets[condition_id] = {
                "conditionId": condition_id,
                "title": title,
                "minutes": mins,
                "slug": market.get("slug"),
            }

    return markets


async def fetch_recent_wallet_trades(
    session: aiohttp.ClientSession,
    wallet: str,
    condition_ids: List[str],
    limit: int = 20,
) -> List[dict]:
    if not condition_ids:
        return []

    params = {
        "user": wallet,
        "market": ",".join(condition_ids),
        "limit": str(limit),
        "offset": "0",
        "takerOnly": str(TAKER_ONLY).lower(),
    }
    data = await safe_get_json(session, TRADES_URL, params=params, timeout=20)
    return data if isinstance(data, list) else []


def format_alert(wallet: str, trade: dict) -> str:
    size = parse_float(trade.get("size"), 0.0)
    price = parse_float(trade.get("price"), 0.0)
    notional = size * price
    market_id = trade.get("market") or trade.get("conditionId") or ""
    minutes = watched_markets.get(market_id, {}).get("minutes", "?")
    dt = parse_trade_timestamp(trade)
    ts = dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else "unknown"

    return (
        f"🚨 **Top Trader Trade Detected**\n"
        f"Wallet: `{short_wallet(wallet)}`\n"
        f"Action: **{trade.get('side')}** {trade.get('outcome')}\n"
        f"Market: {trade.get('title')}\n"
        f"Window: {minutes} min\n"
        f"Size: {trade.get('size')} @ {trade.get('price')}\n"
        f"Notional (approx): `${notional:,.2f}`\n"
        f"Time: `{ts}`\n"
        f"Tx: `{trade.get('transactionHash') or 'n/a'}`"
    )


async def enqueue_alert(wallet: str, trade: dict):
    await alert_queue.put(format_alert(wallet, trade))


async def discord_sender_loop(channel: discord.abc.Messageable):
    """
    One sender only.
    Batches alerts to reduce message count.
    Retries politely on 429.
    """
    while True:
        first = await alert_queue.get()
        batch = [first]

        deadline = asyncio.get_running_loop().time() + DISCORD_BATCH_WINDOW_SECONDS
        while len(batch) < DISCORD_MAX_ALERTS_PER_MESSAGE:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                item = await asyncio.wait_for(alert_queue.get(), timeout=remaining)
                batch.append(item)
            except asyncio.TimeoutError:
                break

        message = "\n\n".join(batch)

        try:
            await channel.send(message[:1900])
        except discord.HTTPException as e:
            if getattr(e, "status", None) == 429:
                retry_after = getattr(e, "retry_after", None)
                wait_time = float(retry_after) if retry_after is not None else 10.0
                print(f"[discord] 429 hit, sleeping {wait_time:.2f}s")
                await asyncio.sleep(wait_time)
                try:
                    await channel.send(message[:1900])
                except Exception as e2:
                    print("[discord] retry failed:", repr(e2))
            else:
                print("[discord] send error:", repr(e))
        except Exception as e:
            print("[discord] unexpected send error:", repr(e))

        await asyncio.sleep(DISCORD_SEND_DELAY_SECONDS)


async def refresh_leaderboard_loop(session: aiohttp.ClientSession):
    global top_wallets
    while True:
        try:
            wallets = await fetch_leaderboard(session)
            if wallets:
                top_wallets = wallets
                print(f"[leaderboard] watching {len(wallets)} wallets")
        except Exception as e:
            print("[leaderboard] error:", repr(e))
        await asyncio.sleep(LEADERBOARD_REFRESH_SECONDS)


async def refresh_markets_loop(session: aiohttp.ClientSession):
    global watched_markets
    while True:
        try:
            markets = await fetch_btc_markets(session)
            if markets:
                watched_markets = markets
                print(f"[markets] watching {len(markets)} BTC markets")
            else:
                print("[markets] no matching BTC markets found")
        except Exception as e:
            print("[markets] error:", repr(e))
        await asyncio.sleep(MARKET_REFRESH_SECONDS)


async def seed_wallet_state(session: aiohttp.ClientSession, wallet: str):
    if wallet in seeded_wallets:
        return

    condition_ids = list(watched_markets.keys())
    if not condition_ids:
        return

    try:
        trades = await fetch_recent_wallet_trades(session, wallet, condition_ids, limit=20)
        for trade in trades:
            seen_trade_keys.add(build_trade_key(trade))
        seeded_wallets.add(wallet)
        print(f"[seed] {wallet} seeded with {len(trades)} trades")
    except Exception as e:
        print(f"[seed] error for {wallet}:", repr(e))


async def trade_loop(channel: discord.abc.Messageable):
    timeout = aiohttp.ClientTimeout(total=20)
    connector = aiohttp.TCPConnector(limit=50, ssl=False)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        try:
            global watched_markets, top_wallets
            watched_markets = await fetch_btc_markets(session)
            top_wallets = await fetch_leaderboard(session)
        except Exception as e:
            print("[startup] error:", repr(e))

        asyncio.create_task(refresh_markets_loop(session))
        asyncio.create_task(refresh_leaderboard_loop(session))
        asyncio.create_task(discord_sender_loop(channel))

        while True:
            try:
                condition_ids = list(watched_markets.keys())
                wallets = list(top_wallets)

                if not condition_ids or not wallets:
                    await asyncio.sleep(2)
                    continue

                for wallet in wallets:
                    if wallet not in seeded_wallets:
                        await seed_wallet_state(session, wallet)
                        await asyncio.sleep(0.1)

                for wallet in wallets:
                    trades = await fetch_recent_wallet_trades(session, wallet, condition_ids, limit=20)

                    new_trades = []
                    for trade in trades:
                        key = build_trade_key(trade)
                        if key in seen_trade_keys:
                            continue

                        seen_trade_keys.add(key)

                        size = parse_float(trade.get("size"), 0.0)
                        price = parse_float(trade.get("price"), 0.0)
                        notional = size * price
                        if MIN_NOTIONAL_USD > 0 and notional < MIN_NOTIONAL_USD:
                            continue

                        dt = parse_trade_timestamp(trade)
                        if dt:
                            age = (datetime.now(timezone.utc) - dt).total_seconds()
                            if age > 180:
                                # skip stale trades older than 3 minutes
                                continue

                            # startup grace: avoid a burst right after boot
                            uptime = (datetime.now(timezone.utc) - startup_time).total_seconds()
                            if uptime < STARTUP_GRACE_SECONDS:
                                continue

                        market_id = trade.get("market") or trade.get("conditionId")
                        if market_id not in watched_markets:
                            continue

                        new_trades.append(trade)

                    new_trades.sort(
                        key=lambda t: parse_trade_timestamp(t) or datetime.min.replace(tzinfo=timezone.utc)
                    )

                    for trade in new_trades:
                        await enqueue_alert(wallet, trade)

                    await asyncio.sleep(0.15)

                await asyncio.sleep(TRADE_POLL_SECONDS)

            except Exception as e:
                print("[trade_loop] error:", repr(e))
                await asyncio.sleep(5)


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

    channel = client.get_channel(ALERT_CHANNEL_ID)
    if not channel:
        print("ERROR: Could not find channel. Check ALERT_CHANNEL_ID and bot permissions.")
        return

    client.loop.create_task(trade_loop(channel))


client.run(DISCORD_TOKEN)
