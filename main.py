"""
Grok Desk BETA - versione semplificata PAPER
Un solo file. Copia e lancia con: python desk_beta.py

Cosa fa:
- Ascolta le monete che escono da pump.fun (PumpPortal)
- Filtra liquidità > $40k e età > 10 min
- Simula PAPER con stack iniziale $200
- API + pagina web base nera
- Nessun ordine reale, solo segnali

Avvio:
  pip install fastapi uvicorn httpx websockets
  python desk_beta.py

Poi apri http://localhost:8000
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
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
        self.positions: Dict[str, dict] = {}      # mint -> pos
        self.activity: List[dict] = []
        self.waiting: Dict[str, dict] = {}        # mint -> info
        self.seen: set = set()
        self.mode = "PAPER"
        self.fomo_handle = "ilvez"

state = State()

# ====================== HELPERS ======================
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
    state.activity = state.activity[:100]

# ====================== SCANNER (semplificato) ======================
async def scanner_loop():
    uri = "wss://pumpportal.fun/api/data"
    while True:
        try:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                print("[Scanner] collegato a PumpPortal")
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
                        "pool": data.get("pool") or data.get("bondingCurveKey"),
                        "ts": time.time(),
                        "creator": data.get("traderPublicKey") or data.get("creator"),
                    }
                    print(f"[Scanner] nuova: {name} ({mint[:8]}...)")
        except Exception as e:
            print(f"[Scanner] errore, riconnetto tra 5s: {e}")
            await asyncio.sleep(5)

async def waiting_room_loop():
    """Controlla le monete in attesa ogni 60s"""
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            await asyncio.sleep(60)
            to_remove = []
            for mint, info in list(state.waiting.items()):
                age_min = (time.time() - info["ts"]) / 60
                if age_min < MIN_AGE_MIN:
                    continue
                # chiedi DexScreener
                try:
                    r = await client.get(f"https://api.dexscreener.com/tokens/v1/solana/{mint}")
                    pairs = r.json()
                    if not pairs:
                        continue
                    # prendi la pair con più liquidità
                    pair = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd") or 0))
                    liq = float(pair.get("liquidity", {}).get("usd") or 0)
                    price = float(pair.get("priceUsd") or 0)
                    h1 = float(pair.get("priceChange", {}).get("h1") or 0)
                    name = pair.get("baseToken", {}).get("name") or info["name"]
                    symbol = pair.get("baseToken", {}).get("symbol") or info["symbol"]

                    if liq < MIN_LIQ:
                        reason = f"liq ${liq/1000:.1f}K"
                        add_activity("rejected", mint, f"Rejected {symbol}", f"Scanner · {reason}", "Skipped")
                        to_remove.append(mint)
                        print(f"[Scanner] kill {symbol}: {reason}")
                        continue

                    if h1 > 80:
                        reason = f"+{h1:.0f}% in 1h"
                        add_activity("rejected", mint, f"Rejected {symbol}", f"Scanner · {reason}", "Skipped")
                        to_remove.append(mint)
                        print(f"[Scanner] kill {symbol}: {reason}")
                        continue

                    # PASS → esegui PAPER
                    size = round(state.balance * (STACK_PCT / 100), 2)
                    if size < 47.50:
                        size = 47.50  # bump

                    if size > state.balance:
                        print(f"[Executor] skip {symbol}: no free stack")
                        to_remove.append(mint)
                        continue

                    # PAPER fill
                    state.balance -= size
                    pos = {
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
                    state.positions[mint] = pos
                    add_activity(
                        "bought", mint,
                        f"Bought {symbol}",
                        f"Executor · PAPER",
                        f"${size:.2f} · {STACK_PCT}% of stack"
                    )
                    print(f"[Executor] PAPER BUY {symbol} @ ${price:.6f} size ${size}")
                    to_remove.append(mint)

                except Exception as e:
                    print(f"[waiting] errore {mint[:8]}: {e}")

            for m in to_remove:
                state.waiting.pop(m, None)

# ====================== RISK ======================
async def risk_loop():
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            await asyncio.sleep(POLL_S)
            if not state.positions:
                continue
            mints = list(state.positions.keys())
            try:
                # batch DexScreener
                url = f"https://api.dexscreener.com/tokens/v1/solana/{','.join(mints[:20])}"
                r = await client.get(url)
                pairs_list = r.json()
                price_map = {}
                for p in pairs_list:
                    m = p.get("baseToken", {}).get("address")
                    if m:
                        price_map[m] = float(p.get("priceUsd") or 0)

                for mint, pos in list(state.positions.items()):
                    price = price_map.get(mint)
                    if not price or price <= 0:
                        continue
                    pos["peak"] = max(pos["peak"], price)

                    # stop
                    if price <= pos["stop"]:
                        await close_position(mint, price, "stop")
                        continue

                    # trail on
                    if not pos["trail_active"] and price >= pos["entry"] * (1 + TRAIL_START / 100):
                        pos["trail_active"] = True
                        print(f"[Risk] trail ON {pos['symbol']}")

                    # trail exit
                    if pos["trail_active"] and price <= pos["peak"] * (1 - TRAIL_PCT / 100):
                        await close_position(mint, price, "trail")
                        continue

                    # timeout 3h e < +5%
                    age_h = (time.time() - pos["opened_at"]) / 3600
                    if age_h >= 3 and price < pos["entry"] * 1.05:
                        await close_position(mint, price, "timeout")

            except Exception as e:
                print(f"[Risk] errore: {e}")

async def close_position(mint: str, exit_price: float, reason: str):
    pos = state.positions.pop(mint, None)
    if not pos:
        return
    pnl = (exit_price - pos["entry"]) / pos["entry"] * pos["size"]
    # fee semplificata ~0.9% round trip
    fees = pos["size"] * 0.009
    net = pnl - fees
    state.balance += pos["size"] + net

    kind = "sold" if net >= 0 else "stopped"
    title = f"{'Sold' if net >= 0 else 'Stopped'} {pos['symbol']}"
    subtitle = f"Risk · {reason}"
    value = f"{'+' if net >= 0 else ''}{net:.2f}$"
    add_activity(kind, mint, title, subtitle, value)
    print(f"[Risk] {reason.upper()} {pos['symbol']} PnL {net:.2f}$")

# ====================== API + WEB ======================
app = FastAPI(title="Grok Desk BETA")

@app.get("/", response_class=HTMLResponse)
async def home():
    mult = state.balance / state.start_balance
    today_pct = (state.balance - state.start_balance) / state.start_balance * 100
    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Grok Desk BETA</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #000; color: #fff; font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif; padding: 16px; padding-bottom: 80px; }}
  .bal {{ font-size: 48px; font-weight: 600; letter-spacing: -1px; }}
  .pct {{ color: #00d35a; font-size: 18px; margin-top: 4px; }}
  .pill {{ display: inline-block; background: #ff9f0a; color: #000; font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 99px; margin-left: 8px; }}
  .card {{ background: #1c1c1e; border-radius: 16px; padding: 16px; margin-top: 16px; }}
  .row {{ display: flex; justify-content: space-between; align-items: center; padding: 12px 0; border-bottom: 1px solid rgba(84,84,88,0.4); }}
  .row:last-child {{ border-bottom: none; }}
  .title {{ font-weight: 500; }}
  .sub {{ color: rgba(235,235,245,0.6); font-size: 13px; margin-top: 2px; }}
  .val {{ font-variant-numeric: tabular-nums; }}
  a {{ color: #0a84ff; text-decoration: none; }}
  .nav {{ position: fixed; bottom: 0; left: 0; right: 0; background: rgba(28,28,30,0.92); backdrop-filter: blur(20px); display: flex; justify-content: space-around; padding: 12px 0 28px; border-top: 1px solid rgba(84,84,88,0.5); }}
  .nav a {{ color: rgba(235,235,245,0.6); font-size: 11px; text-align: center; }}
  .nav a.active {{ color: #fff; }}
</style>
</head>
<body>
  <div style="display:flex;align-items:center;justify-content:space-between;">
    <div>
      <div class="bal">${state.balance:.2f}</div>
      <div class="pct">{'+' if today_pct >= 0 else ''}{today_pct:.2f}% <span class="pill">PAPER</span></div>
    </div>
    <div style="text-align:right;color:rgba(235,235,245,0.6);font-size:13px;">
      {mult:.2f}×<br>mode {state.mode}
    </div>
  </div>

  <div class="card">
    <div style="font-size:13px;color:rgba(235,235,245,0.6);margin-bottom:8px;">Posizioni aperte · {len(state.positions)}</div>
    {"".join(f'''
    <div class="row">
      <div>
        <div class="title">{p["symbol"]}</div>
        <div class="sub">entry ${p["entry"]:.6f} · size ${p["size"]:.0f}</div>
      </div>
      <div class="val"><a href="{coin_url(m)}" target="_blank">FOMO ↗</a></div>
    </div>''' for m, p in state.positions.items()) or '<div style="color:rgba(235,235,245,0.4);padding:12px 0;">Nessuna posizione</div>'}
  </div>

  <div class="card">
    <div style="font-size:13px;color:rgba(235,235,245,0.6);margin-bottom:8px;">Activity</div>
    {"".join(f'''
    <div class="row">
      <div>
        <div class="title">{a["title"]}</div>
        <div class="sub">{a["subtitle"]}</div>
      </div>
      <div class="val">{a["value"]}</div>
    </div>''' for a in state.activity[:15]) or '<div style="color:rgba(235,235,245,0.4);padding:12px 0;">Niente ancora</div>'}
  </div>

  <div class="card" style="font-size:13px;color:rgba(235,235,245,0.5);">
    Waiting room: {len(state.waiting)} · Seen: {len(state.seen)}<br>
    <a href="{SIGNUP}" target="_blank">Invita su FOMO (ref)</a>
  </div>

  <div class="nav">
    <a class="active" href="/">Home</a>
    <a href="/api/state">API</a>
    <a href="{SIGNUP}" target="_blank">FOMO</a>
  </div>
</body>
</html>"""
    return HTMLResponse(html)

@app.get("/api/state")
async def api_state():
    return {
        "balance": state.balance,
        "start": state.start_balance,
        "mode": state.mode,
        "open": len(state.positions),
        "waiting": len(state.waiting),
        "activity": state.activity[:20],
        "positions": [
            {
                "mint": m,
                "symbol": p["symbol"],
                "entry": p["entry"],
                "size": p["size"],
                "peak": p["peak"],
                "stop": p["stop"],
                "trail": p["trail_active"],
                "link": coin_url(m),
            }
            for m, p in state.positions.items()
        ],
    }

# ====================== MAIN ======================
async def main():
    import os
    port = int(os.environ.get("PORT", 8000))
    print("=" * 50)
    print("  Grok Desk BETA · PAPER")
    print(f"  Stack iniziale: ${PAPER_START}")
    print(f"  Porta: {port}")
    print("=" * 50)
    # avvia i loop
    asyncio.create_task(scanner_loop())
    asyncio.create_task(waiting_room_loop())
    asyncio.create_task(risk_loop())
    # avvia server
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
