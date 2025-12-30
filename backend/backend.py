import asyncio
import json
import random
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# === IMPORTS FROM YOUR holders_snapshot.py ===
from holders_snapshot import (
    get_program_accounts_slice,
    parse_accounts_to_holders,
    get_mint_decimals_via_accountinfo,
    TOKEN_PROGRAM,
    TOKEN_2022_PROGRAM,
)

# ---------------- Paths ----------------
BASE_DIR = Path(__file__).resolve().parent  # /backend
CONFIG_PATH = BASE_DIR / "config.json"
EXCLUDE_PATH = BASE_DIR / "exclude_addresses.json"

# фронт должен лежать тут: backend/frontend/index.html
FRONTEND_DIR = BASE_DIR / "frontend"
INDEX_HTML = FRONTEND_DIR / "index.html"

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

# анти-429/backoff
_snapshot_backoff_sec: float = 0.0
_snapshot_backoff_max: float = 30.0

# ---------------- Frontend serving ----------------
# отдаём index.html по /
@app.get("/")
async def root():
    if INDEX_HTML.exists():
        return FileResponse(INDEX_HTML)
    return JSONResponse(
        {"detail": "Frontend index.html not found. Put it in backend/frontend/index.html"},
        status_code=404,
    )

# (опционально) отдать любые файлы из frontend по /static/...
# если у тебя всё в одном index.html — всё равно можно оставить, не мешает
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/health")
async def health():
    return {
        "ok": True,
        "phase": phase,
        "round": round_number,
        "holders": len(holders),
        "time_left": time_left,
    }


# ---------------- Helpers ----------------
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

    # dev_wallet тоже исключаем
    if cfg.get("dev_wallet"):
        exclude_set.add(cfg["dev_wallet"])

    # длительность раунда и интервал снапшота из конфига
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

    combined: dict[str, int] = {}

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
        out.append(
            {
                "address": owner,
                "balance": int(raw_amt // div),  # integer balance
            }
        )

    out.sort(key=lambda x: x["balance"], reverse=True)
    return out


# ---------------- Loops ----------------
async def snapshot_loop():
    """
    Обновляем holders.
    Если Helius начинает 429 (Too Many Requests) — включаем backoff, чтобы сервис не умирал.
    """
    global holders, _snapshot_backoff_sec

    while True:
        try:
            # backoff если был 429 ранее
            if _snapshot_backoff_sec > 0:
                await asyncio.sleep(_snapshot_backoff_sec)

            holders = compute_holders_snapshot()

            # если всё ок — постепенно отпускаем backoff
            if _snapshot_backoff_sec > 0:
                _snapshot_backoff_sec = max(0.0, _snapshot_backoff_sec * 0.5)

        except Exception as e:
            msg = repr(e)
            print("[SNAPSHOT ERROR]", msg)

            # очень частая причина на проде — 429 от Helius
            if "429" in msg or "Too Many Requests" in msg:
                # экспоненциальный backoff: 1s -> 2s -> 4s -> 8s ...
                if _snapshot_backoff_sec <= 0:
                    _snapshot_backoff_sec = 1.0
                else:
                    _snapshot_backoff_sec = min(_snapshot_backoff_sec * 2.0, _snapshot_backoff_max)

                print(f"[SNAPSHOT] rate-limited, backoff={_snapshot_backoff_sec:.1f}s")
            else:
                # на любые другие ошибки — небольшой сон, чтобы не крутиться в 100% CPU
                await asyncio.sleep(0.5)

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

        for ws in list(clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)

        for ws in dead:
            clients.discard(ws)

        await asyncio.sleep(0.2)


# ---------------- WebSocket ----------------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)

    try:
        while True:
            # фронт шлёт ping — оставляем receive, чтобы соединение не висело "мертвым"
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.discard(ws)
    except Exception:
        clients.discard(ws)


# ---------------- Startup ----------------
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
