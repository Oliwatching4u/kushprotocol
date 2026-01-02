import asyncio
import json
import random
import time
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from holders_snapshot import (
    get_program_accounts_slice,
    parse_accounts_to_holders,
    get_mint_decimals_via_accountinfo,
    TOKEN_PROGRAM,
    TOKEN_2022_PROGRAM,
)

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
EXCLUDE_PATH = BASE_DIR / "exclude_addresses.json"
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI()

@app.get("/")
async def root():
    return FileResponse(FRONTEND_DIR / "index.html")

clients: set[WebSocket] = set()

# ---- state ----
holders: list[dict] = []
winners: list[dict] = []
round_number = 1
time_left = 0
phase = "COLLECTING"

draw_id = 0
last_draw_addresses: list[str] = []

started = False
start_ts: float | None = None

cfg = {}
exclude_set: set[str] = set()
decimals = 0

ROUND_DURATION = 15
SNAPSHOT_INTERVAL = 0.2
START_DELAY_SECONDS = 60  # ⏱ отложенный старт


def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def load_config():
    global cfg, exclude_set, ROUND_DURATION, SNAPSHOT_INTERVAL

    cfg = load_json(CONFIG_PATH, {})
    ex = load_json(EXCLUDE_PATH, {"exclude": []})

    exclude_set = set(ex.get("exclude", []))
    if cfg.get("dev_wallet"):
        exclude_set.add(cfg["dev_wallet"])

    ROUND_DURATION = int(cfg.get("round_duration", 300))
    SNAPSHOT_INTERVAL = float(cfg.get("snapshot_interval_sec", 0.5))


def compute_holders_snapshot() -> list[dict]:
    mint = cfg["mint"]
    rpc_url = cfg["rpc_url"]

    spl_accounts = get_program_accounts_slice(
        rpc_url, TOKEN_PROGRAM, mint, use_datasize_165=True
    )
    spl_map = parse_accounts_to_holders(spl_accounts)

    t22_accounts = get_program_accounts_slice(
        rpc_url, TOKEN_2022_PROGRAM, mint, use_datasize_165=False
    )
    t22_map = parse_accounts_to_holders(t22_accounts)

    combined = {}
    for owner, amt in spl_map.items():
        combined[owner] = combined.get(owner, 0) + int(amt)
    for owner, amt in t22_map.items():
        combined[owner] = combined.get(owner, 0) + int(amt)

    out = []
    div = 10 ** decimals if decimals else 1

    for owner, raw_amt in combined.items():
        if owner in exclude_set:
            continue
        if raw_amt <= 0:
            continue
        out.append({
            "address": owner,
            "balance": int(raw_amt // div),
        })

    out.sort(key=lambda x: x["balance"], reverse=True)

    # ❌ убираем пул ликвидности (первый, самый большой)
    return out[1:] if len(out) > 1 else []


async def snapshot_loop():
    global holders
    while True:
        try:
            holders = compute_holders_snapshot()
        except Exception as e:
            print("[SNAPSHOT ERROR]", repr(e))
        await asyncio.sleep(SNAPSHOT_INTERVAL)


async def round_loop():
    global round_number, time_left, phase, winners, draw_id, last_draw_addresses

    while True:
        phase = "COLLECTING"
        time_left = ROUND_DURATION

        while time_left > 0:
            await asyncio.sleep(1)
            time_left -= 1

        phase = "DRAWING"
        await asyncio.sleep(0.25)

        draw_id += 1
        last_draw_addresses = []

        if holders:
            top400 = holders[:400]
            picked = random.sample(top400, min(2, len(top400)))
            last_draw_addresses = [p["address"] for p in picked]

            for addr in last_draw_addresses:
                winners.insert(0, {"round": round_number, "address": addr})
            winners[:] = winners[:60]

        await asyncio.sleep(1.0)
        round_number += 1


async def broadcast_loop():
    while True:
        payload = {
            "started": started,
            "start_in": int(start_ts - time.time()) if start_ts and not started else 0,
            "round": round_number,
            "time_left": time_left,
            "round_duration": ROUND_DURATION,
            "phase": phase,
            "holders": holders,
            "winners": winners,
            "dev_wallet": cfg.get("dev_wallet", ""),
            "ca": cfg.get("mint", ""),
            "draw_id": draw_id,
            "draw_addresses": last_draw_addresses,
        }

        msg = json.dumps(payload)
        dead = set()

        for ws in clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)

        for ws in dead:
            clients.discard(ws)

        await asyncio.sleep(0.2)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.discard(ws)


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(FRONTEND_DIR / "favicon.ico")


async def delayed_start():
    global started, start_ts
    start_ts = time.time() + START_DELAY_SECONDS
    await asyncio.sleep(START_DELAY_SECONDS)
    started = True
    asyncio.create_task(snapshot_loop())
    asyncio.create_task(round_loop())


@app.on_event("startup")
async def startup():
    global decimals, time_left

    load_config()
    time_left = ROUND_DURATION
    decimals = get_mint_decimals_via_accountinfo(cfg["rpc_url"], cfg["mint"])

    asyncio.create_task(broadcast_loop())
    asyncio.create_task(delayed_start())
