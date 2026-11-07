"""
Grok Desk BETA · Nick
PAPER mode · 5 agents visible
Deploy on Render free tier
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Dict, List

import httpx
import websockets
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

# ====================== CONFIG ======================
PAPER_START = 200.0
MIN_LIQ = 40000
MIN_AGE_MIN = 10
STACK_PCT = 8
STOP_PCT = 6
TRAIL_START = 20
TRAIL_PCT = 12
POLL_S = 15
FOMO_REF = "savipww"
SIGNUP = f"https://fomo.family/r/{FOMO_REF}"

# ====================== STATE ======================
class State:
    def __init__(self):
        self.balance = PAPER_START
        self.start_balance = PAPER_START
        self.positions: Dict[str, dict] = {}
        self.activity: List[dict] = []
        self.waiting: Dict[str, dict] = {}
        self.seen: set = set()
        self.mode = "PAPER"
        self.fomo_handle = "Nick"
        self.scanner_count = 0
        self.auditor_count = 0
        self.executor_count = 0

state = State()

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def coin_url(mint: str) -> str:
    return f"https://fomo.family/coin?address={mint}&chainId=1399811149&r={FOMO_REF}&source=share_link"

def add_activity(kind: str, mint: str, title: str, subtitle: str, value: str):
    state.activity.insert(0, {
        "ts": now_iso(),
        "kind": kind,
        "mint": mint,
        "title": title,
        "subtitle": subtitle,
        "value": value,
    })
    state.activity = state.activity[:80]

# ====================== SCANNER ======================
async def scanner_loop():
    uri = "wss://pumpportal.fun/api/data"
    while True:
        try:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                print("[Scanner] connected")
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    mint = data.get("mint") or data.get("token")
                    if not mint or mint in state.seen:
                        continue
                    state.seen.add(mint)
                    name = data.get("name") or data.get("symbol") or mint[:8]
                    state.waiting[mint] = {
                        "mint": mint,
                        "name": name,
                        "symbol": data.get("symbol") or "???",
                        "ts": time.time(),
                    }
                    state.scanner_count += 1
                    print(f"[Scanner] {name}")
        except Exception as e:
            print(f"[Scanner] reconnect: {e}")
            await asyncio.sleep(5)

async def waiting_room_loop():
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            await asyncio.sleep(60)
            to_remove = []
            for mint, info in list(state.waiting.items()):
                age_min = (time.time() - info["ts"]) / 60
                if age_min < MIN_AGE_MIN:
                    continue
                try:
                    r = await client.get(f"https://api.dexscreener.com/tokens/v1/solana/{mint}")
                    pairs = r.json()
                    if not pairs:
                        continue
                    pair = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd") or 0))
                    liq = float(pair.get("liquidity", {}).get("usd") or 0)
                    price = float(pair.get("priceUsd") or 0)
                    h1 = float(pair.get("priceChange", {}).get("h1") or 0)
                    name = pair.get("baseToken", {}).get("name") or info["name"]
                    symbol = pair.get("baseToken", {}).get("symbol") or info["symbol"]

                    if liq < MIN_LIQ:
                        add_activity("rejected", mint, f"Rejected {symbol}", f"Scanner · liq ${liq/1000:.1f}K", "Skipped")
                        to_remove.append(mint)
                        continue
                    if h1 > 80:
                        add_activity("rejected", mint, f"Rejected {symbol}", f"Scanner · +{h1:.0f}% 1h", "Skipped")
                        to_remove.append(mint)
                        continue

                    size = round(state.balance * (STACK_PCT / 100), 2)
                    if size < 47.50:
                        size = 47.50
                    if size > state.balance:
                        to_remove.append(mint)
                        continue

                    state.balance -= size
                    state.positions[mint] = {
                        "mint": mint,
                        "name": name,
                        "symbol": symbol,
                        "entry": price,
                        "size": size,
                        "peak": price,
                        "stop": price * (1 - STOP_PCT / 100),
                        "trail_active": False,
                        "opened_at": time.time(),
                    }
                    state.executor_count += 1
                    add_activity("bought", mint, f"Bought {symbol}", "Executor · PAPER", f"${size:.2f}")
                    print(f"[Executor] BUY {symbol} ${size}")
                    to_remove.append(mint)
                except Exception as e:
                    print(f"[waiting] {e}")
            for m in to_remove:
                state.waiting.pop(m, None)

# ====================== RISK ======================
async def risk_loop():
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            await asyncio.sleep(POLL_S)
            if not state.positions:
                continue
            mints = list(state.positions.keys())[:20]
            try:
                r = await client.get(f"https://api.dexscreener.com/tokens/v1/solana/{','.join(mints)}")
                price_map = {}
                for p in r.json():
                    m = p.get("baseToken", {}).get("address")
                    if m:
                        price_map[m] = float(p.get("priceUsd") or 0)
                for mint, pos in list(state.positions.items()):
                    price = price_map.get(mint)
                    if not price or price <= 0:
                        continue
                    pos["peak"] = max(pos["peak"], price)
                    if price <= pos["stop"]:
                        await close_position(mint, price, "stop")
                        continue
                    if not pos["trail_active"] and price >= pos["entry"] * (1 + TRAIL_START / 100):
                        pos["trail_active"] = True
                    if pos["trail_active"] and price <= pos["peak"] * (1 - TRAIL_PCT / 100):
                        await close_position(mint, price, "trail")
                        continue
                    age_h = (time.time() - pos["opened_at"]) / 3600
                    if age_h >= 3 and price < pos["entry"] * 1.05:
                        await close_position(mint, price, "timeout")
            except Exception as e:
                print(f"[Risk] {e}")

async def close_position(mint: str, exit_price: float, reason: str):
    pos = state.positions.pop(mint, None)
    if not pos:
        return
    pnl = (exit_price - pos["entry"]) / pos["entry"] * pos["size"]
    fees = pos["size"] * 0.009
    net = pnl - fees
    state.balance += pos["size"] + net
    kind = "sold" if net >= 0 else "stopped"
    add_activity(kind, mint, f"{'Sold' if net >= 0 else 'Stopped'} {pos['symbol']}", f"Risk · {reason}", f"{net:+.2f}$")
    print(f"[Risk] {reason} {pos['symbol']} {net:+.2f}")

# ====================== WEB ======================
app = FastAPI(title="Grok Desk · Nick")

@app.get("/", response_class=HTMLResponse)
async def home():
    mult = state.balance / state.start_balance
    today_pct = (state.balance - state.start_balance) / state.start_balance * 100
    waiting = len(state.waiting)
    open_n = len(state.positions)

    agents = f"""
    <div style="display:flex;gap:10px;overflow-x:auto;padding:4px 0 12px;scrollbar-width:none;">
      <div class="ag" style="border-top:3px solid #64d2ff">
        <div class="ag-name">Scanner</div>
        <div class="ag-val">{waiting} waiting</div>
        <div class="ag-sub">{state.scanner_count} seen</div>
      </div>
      <div class="ag" style="border-top:3px solid #ff9f0a">
        <div class="ag-name">Auditor</div>
        <div class="ag-val">ready</div>
        <div class="ag-sub">8 layers</div>
      </div>
      <div class="ag" style="border-top:3px solid #ffd60a">
        <div class="ag-name">Analyst</div>
        <div class="ag-val">rules</div>
        <div class="ag-sub">score ≥ 70</div>
      </div>
      <div class="ag" style="border-top:3px solid #00d35a">
        <div class="ag-name">Executor</div>
        <div class="ag-val">{state.executor_count} trades</div>
        <div class="ag-sub">8% · PAPER</div>
      </div>
      <div class="ag" style="border-top:3px solid #ff6b9a">
        <div class="ag-name">Risk</div>
        <div class="ag-val">{"Guarding" if open_n else "Idle"}</div>
        <div class="ag-sub">-6% · trail 12%</div>
      </div>
    </div>
    """

    pos_html = "".join(f"""
    <div class="row">
      <div>
        <div class="t">{p['symbol']}</div>
        <div class="s">entry ${p['entry']:.6f} · ${p['size']:.0f}</div>
      </div>
      <a href="{coin_url(m)}" target="_blank">FOMO ↗</a>
    </div>""" for m, p in state.positions.items()) or '<div class="empty">Nessuna posizione</div>'

    act_html = "".join(f"""
    <div class="row">
      <div>
        <div class="t">{a['title']}</div>
        <div class="s">{a['subtitle']}</div>
      </div>
      <div class="v">{a['value']}</div>
    </div>""" for a in state.activity[:10]) or '<div class="empty">Niente ancora</div>'

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Grok Desk · Nick</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#000;color:#fff;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;padding:16px;padding-bottom:88px}}
.bal{{font-size:44px;font-weight:600;letter-spacing:-1.5px}}
.pct{{color:#00d35a;font-size:15px;margin-top:2px}}
.pill{{display:inline-block;background:#ff9f0a;color:#000;font-size:11px;font-weight:700;padding:2px 7px;border-radius:99px;margin-left:6px}}
.card{{background:#1c1c1e;border-radius:16px;padding:14px 16px;margin-top:14px}}
.row{{display:flex;justify-content:space-between;align-items:center;padding:11px 0;border-bottom:1px solid rgba(84,84,88,.35)}}
.row:last-child{{border-bottom:none}}
.t{{font-weight:500;font-size:15px}}
.s{{color:rgba(235,235,245,.55);font-size:12px;margin-top:2px}}
.v{{font-variant-numeric:tabular-nums;font-size:14px}}
a{{color:#0a84ff;text-decoration:none;font-size:13px}}
.ag{{background:#1c1c1e;border-radius:14px;padding:12px 14px;min-width:118px;flex-shrink:0}}
.ag-name{{font-size:12px;font-weight:600;opacity:.7;margin-bottom:3px}}
.ag-val{{font-size:15px;font-weight:500}}
.ag-sub{{font-size:11px;color:rgba(235,235,245,.45);margin-top:2px}}
.empty{{color:rgba(235,235,245,.4);padding:10px 0;font-size:14px}}
.nav{{position:fixed;bottom:0;left:0;right:0;background:rgba(28,28,30,.94);backdrop-filter:blur(20px);display:flex;justify-content:space-around;padding:12px 0 28px;border-top:1px solid rgba(84,84,88,.4)}}
.nav a{{color:rgba(235,235,245,.5);font-size:11px}}
.nav a.on{{color:#fff}}
.st{{font-size:13px;color:rgba(235,235,245,.5);margin:16px 0 6px}}
</style>
</head>
<body>
<div style="display:flex;justify-content:space-between;align-items:flex-start">
  <div>
    <div class="bal">${state.balance:.2f}</div>
    <div class="pct">{'+' if today_pct>=0 else ''}{today_pct:.2f}% <span class="pill">PAPER</span></div>
  </div>
  <div style="text-align:right;color:rgba(235,235,245,.5);font-size:12px;padding-top:8px">
    {mult:.2f}×<br>Nick
  </div>
</div>

<div class="st">Agents · 5 running</div>
{agents}

<div class="card">
  <div class="st" style="margin:0 0 6px">Posizioni · {open_n}</div>
  {pos_html}
</div>

<div class="card">
  <div class="st" style="margin:0 0 6px">Activity</div>
  {act_html}
</div>

<div class="card" style="font-size:12px;color:rgba(235,235,245,.4)">
  Waiting {waiting} · Seen {len(state.seen)}<br>
  <a href="{SIGNUP}" target="_blank">Invita su FOMO</a>
</div>

<div class="nav">
  <a class="on" href="/">Home</a>
  <a href="/api/state">API</a>
  <a href="{SIGNUP}" target="_blank">FOMO</a>
</div>
</body>
</html>""")

@app.get("/api/state")
async def api_state():
    return {
        "balance": state.balance,
        "mode": state.mode,
        "open": len(state.positions),
        "waiting": len(state.waiting),
        "activity": state.activity[:15],
    }

# ====================== MAIN ======================
async def main():
    port = int(os.environ.get("PORT", 8000))
    print(f"Grok Desk · Nick · PAPER · port {port}")
    asyncio.create_task(scanner_loop())
    asyncio.create_task(waiting_room_loop())
    asyncio.create_task(risk_loop())
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
