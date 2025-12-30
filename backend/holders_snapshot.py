import json
import time
import base64
import struct
import requests
from collections import defaultdict
from pathlib import Path

CONFIG_PATH = Path("config.json")
EXCLUDE_PATH = Path("exclude_addresses.json")
OUTPUT_PATH = Path("holders_snapshot.json")

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"     # SPL Token
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb" # Token-2022

# Token account layout (both SPL and Token-2022 base layout):
# offset 0..31  = mint
# offset 32..63 = owner
# offset 64..71 = amount (u64 LE)
OWNER_OFFSET = 32
AMOUNT_OFFSET = 64


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def rpc_call(rpc_url: str, method: str, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = requests.post(rpc_url, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["result"]


def b58_encode(data: bytes) -> str:
    # minimal base58 (no dependency)
    alphabet = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(data, "big")
    out = bytearray()
    while n > 0:
        n, rem = divmod(n, 58)
        out.append(alphabet[rem])
    # leading zeros
    pad = 0
    for b in data:
        if b == 0:
            pad += 1
        else:
            break
    return (alphabet[0:1] * pad + out[::-1]).decode("ascii") if out else (alphabet[0:1] * pad).decode("ascii")


def get_program_accounts_slice(rpc_url: str, program_id: str, mint: str, use_datasize_165: bool):
    # We fetch ONLY (owner + amount) = 40 bytes to avoid provider truncation.
    filters = [{"memcmp": {"offset": 0, "bytes": mint}}]
    if use_datasize_165:
        # SPL token accounts are fixed 165. Good optimization.
        filters.insert(0, {"dataSize": 165})

    return rpc_call(
        rpc_url,
        "getProgramAccounts",
        [
            program_id,
            {
                "encoding": "base64",
                "dataSlice": {"offset": OWNER_OFFSET, "length": 32 + 8},  # owner(32) + amount(8)
                "filters": filters,
            },
        ],
    )


def parse_accounts_to_holders(accounts) -> defaultdict:
    holders_raw = defaultdict(int)

    for acc in accounts:
        data_b64 = acc["account"]["data"][0]
        raw = base64.b64decode(data_b64)

        if len(raw) < 40:
            continue

        owner_bytes = raw[0:32]
        amount_bytes = raw[32:40]
        amount = struct.unpack("<Q", amount_bytes)[0]

        if amount == 0:
            continue

        owner = b58_encode(owner_bytes)
        holders_raw[owner] += amount

    return holders_raw


def get_mint_decimals_via_accountinfo(rpc_url: str, mint: str) -> int:
    # Mint layout:
    # decimals is at offset 44 (u8) in Mint account for SPL.
    # With jsonParsed easiest.
    info = rpc_call(rpc_url, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    if not info or not info.get("value"):
        return 0
    try:
        parsed = info["value"]["data"]["parsed"]["info"]
        return int(parsed.get("decimals", 0))
    except Exception:
        return 0


def snapshot_once():
    config = load_json(CONFIG_PATH, {})
    exclude_cfg = load_json(EXCLUDE_PATH, {})
    exclude = set(exclude_cfg.get("exclude", []))

    mint = config["mint"]
    rpc_url = config["rpc_url"]
    interval = int(config.get("snapshot_interval_sec", 10))

    dev_wallet = config.get("dev_wallet")
    if dev_wallet:
        exclude.add(dev_wallet)

    decimals = get_mint_decimals_via_accountinfo(rpc_url, mint)

    # 1) SPL token accounts (dataSize=165 OK)
    spl_accounts = get_program_accounts_slice(rpc_url, TOKEN_PROGRAM, mint, use_datasize_165=True)
    spl_holders_raw = parse_accounts_to_holders(spl_accounts)

    # 2) Token-2022 token accounts (NO dataSize filter!)
    t22_accounts = get_program_accounts_slice(rpc_url, TOKEN_2022_PROGRAM, mint, use_datasize_165=False)
    t22_holders_raw = parse_accounts_to_holders(t22_accounts)

    holders_raw = defaultdict(int)
    for k, v in spl_holders_raw.items():
        holders_raw[k] += v
    for k, v in t22_holders_raw.items():
        holders_raw[k] += v

    holders = []
    for owner, raw_amt in holders_raw.items():
        if owner in exclude:
            continue
        ui = raw_amt / (10 ** decimals) if decimals else float(raw_amt)
        holders.append({"owner": owner, "balance": ui})

    holders.sort(key=lambda x: x["balance"], reverse=True)

    for i, h in enumerate(holders):
        h["rank"] = i + 1
        h["eligible"] = i < 40

    snapshot = {
        "mint": mint,
        "updated_at": int(time.time()),
        "total_holders": len(holders),
        "eligible_count": min(40, len(holders)),
        "holders": holders,
        "meta": {
            "decimals": decimals,
            "token_accounts_spl": len(spl_accounts),
            "token_accounts_token2022": len(t22_accounts),
            "excluded_count": len(exclude),
            "interval_sec": interval,
        },
    }

    OUTPUT_PATH.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    print(
        f"[SNAPSHOT] holders={len(holders)} "
        f"eligible={min(40, len(holders))} "
        f"spl_acc={len(spl_accounts)} t22_acc={len(t22_accounts)} "
        f"updated"
    )

    return interval


def snapshot_loop():
    print("[START] holders snapshot started")
    while True:
        try:
            interval = snapshot_once()
        except Exception as e:
            print("[ERROR]", repr(e))
            interval = 10
        time.sleep(interval)


if __name__ == "__main__":
    snapshot_loop()
