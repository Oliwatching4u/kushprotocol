import asyncio
import json
import random
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

# === IMPORTS FROM YOUR holders_snapshot.py ===
from holders_snapshot import (
    get_program_accounts_slice,
    parse_accounts_to_holders,
    get_mint_decimals_via_accountinfo,
    TOKEN_PROGRAM,
    TOKEN_2022_PROGRAM,
)

CONFIG_PATH = Path("config.json")
EXCLUDE_PATH = Path("exclude_addresses.json")

app = FastAPI()
clients: set[WebSocket] = set()

# ---- state ----
holders: list[dict] = []
winners: list[dict] = []
round_number: int = 1
time_left: int = 0
phase: str = "COLLECTING"

draw_id: int = 0
last_draw_addresses: list[str] = []

cfg = {}
exclude_set: set[str] = set()
decimals: int = 0
ROUND_DURATION: int = 15
SNAPSHOT_INTERVAL: float = 0.2


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
    SNAPSHOT_INTERVAL = float(cfg.get("snapshot_interval_sec", 0.2))


def compute_holders_snapshot() -> list[dict]:
    mint = cfg["mint"]
    rpc_url = cfg["rpc_url"]

    # SPL token accounts (dataSize 165 ok)
    spl_accounts = get_program_accounts_slice(
        rpc_url, TOKEN_PROGRAM, mint, use_datasize_165=True
    )
    spl_map = parse_accounts_to_holders(spl_accounts)

    # Token-2022 accounts (NO dataSize filter)
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
            "balance": int(raw_amt // div),   # <-- integer balance
        })

    out.sort(key=lambda x: x["balance"], reverse=True)
    return out


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

        # ---- draw ----
        phase = "DRAWING"
        await asyncio.sleep(0.25)

        draw_id += 1
        last_draw_addresses = []

        if holders:
            picked = random.sample(holders, min(3, len(holders)))
            last_draw_addresses = [p["address"] for p in picked]

            # add to winners list
            for addr in last_draw_addresses:
                winners.insert(0, {"round": round_number, "address": addr})
            winners[:] = winners[:60]

        # small pause so front can show highlight once
        await asyncio.sleep(1.0)

        round_number += 1
        phase = "COLLECTING"


async def broadcast_loop():
    while True:
        payload = {
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

        dead = set()
        msg = json.dumps(payload)

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
            # we don't require client messages
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.discard(ws)
    except Exception:
        clients.discard(ws)


@app.on_event("startup")
async def startup():
    global decimals, time_left

    load_config()
    time_left = ROUND_DURATION

    # decimals once
    decimals = get_mint_decimals_via_accountinfo(cfg["rpc_url"], cfg["mint"])

    asyncio.create_task(snapshot_loop())
    asyncio.create_task(round_loop())
    asyncio.create_task(broadcast_loop())
