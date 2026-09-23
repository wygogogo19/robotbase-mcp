#!/usr/bin/env python3
"""RobotBase MCP Server — read-only multi-chain data tools (BTC / KAS / ZEC / RVN / DOGE / LTC)."""
import base64, hashlib, html, json, os, re, socket, sqlite3, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("RB_PORT", "8090"))
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "robotbase-mcp", "version": "0.5.2", "title": "RobotBase on-chain data MCP (six PoW chains)"}
# Optional env file holding BTC_RPC_URL / BTC_RPC_USER / BTC_RPC_PASS
BTC_ENV = os.environ.get("RB_BTC_ENV", "")
ZEC_BASE = os.environ.get("RB_ZEC_BASE", "http://127.0.0.1:8080")
DOGE_BASE = os.environ.get("RB_DOGE_BASE", "http://127.0.0.1:8080")
LTC_BASE = os.environ.get("RB_LTC_BASE", "http://127.0.0.1:8080")
# Gateway aggregating the six nodes: /api/nodes, /api/pools, /api/services, /api/node/kas
HOME_BASE = os.environ.get("RB_HOME_BASE", "http://127.0.0.1:8081")

_cache, _cache_lock = {}, threading.Lock()
# ---- BTC 地址摘要加固参数（2026-09-16）----
BTC_ADDR_BUDGET = 4.0          # 硬预算秒数（SOP：慢工具 <4000ms）
BTC_ADDR_CACHE_TTL = 60        # 正常结果缓存
BTC_ADDR_CACHE_TTL_BAD = 20    # 降级/超时结果短暂缓存，避免热门地址反复打 electrs
_addr_cache = {}
_btc_gate = threading.Semaphore(1)   # 同一时刻只允许 1 个地址查询打 electrs（防低效路径洪水压垮索引层）

_rl, _rl_lock = {}, threading.Lock()
RATE_LIMIT_PER_MIN = 120


def _cfg(path, key):
    try:
        for line in open(path):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _http_json(url, payload=None, timeout=12, headers=None, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    h = {"User-Agent": "robotbase-mcp/0.1"}
    if data is not None:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            d = json.loads(raw)
            if isinstance(d, dict) and d.get("error"):
                raise RuntimeError(str(d["error"])[:200])
        except json.JSONDecodeError:
            pass
        raise RuntimeError(f"HTTP {e.code}: {raw[:160]}")


def cached(key, ttl, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def btc_rpc(method, params=None):
    url = _cfg(BTC_ENV, "BTC_RPC_URL") or os.environ.get("RB_BTC_RPC_URL", "http://127.0.0.1:8332")
    user, pw = _cfg(BTC_ENV, "BTC_RPC_USER"), _cfg(BTC_ENV, "BTC_RPC_PASS")
    creds = base64.b64encode(f"{user}:{pw}".encode()).decode()
    d = _http_json(url, {"jsonrpc": "1.0", "id": "mcp", "method": method, "params": params or []},
                   headers={"Authorization": "Basic " + creds})
    if d.get("error"):
        raise RuntimeError(str(d["error"]))
    return d.get("result")


def svc_status(chain):
    base = {"zec": ZEC_BASE, "doge": DOGE_BASE, "ltc": LTC_BASE}.get(chain)
    if not base:
        raise ValueError(f"unknown chain: {chain}")
    d = _http_json(base + "/api/status")
    return {k: d.get(k) for k in ("chain", "blocks", "headers", "estimated_height", "verification_progress_pct",
                                  "initial_block_download", "connections", "mempool_txs", "size_on_disk_gb", "rpc_ok", "error")}


def truncate(obj, limit=3800):
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + '…(truncated)'


SIX_CHAINS = ("btc", "kas", "zec", "rvn", "doge", "ltc")


def t_list_chains():
    """The six chains this gateway serves, with live node state and height."""
    d = cached("nodes", 15, lambda: _http_json(HOME_BASE + "/api/nodes"))
    nodes = d.get("nodes") or {}
    out = [{"chain": c, "ok": bool((nodes.get(c) or {}).get("ok")),
            "synced": bool((nodes.get(c) or {}).get("synced")),
            "height": (nodes.get(c) or {}).get("height"),
            "state": (nodes.get(c) or {}).get("state")}
           for c in SIX_CHAINS]
    return {"chains": out, "updated_utc": d.get("updated_utc")}


def t_chain_status(chain):
    chain = (chain or "").lower()
    if chain == "btc":
        d = cached("btc_node", 10, lambda: _http_json("http://127.0.0.1:8600/api/node"))
        return {k: d.get(k) for k in ("chain", "blocks", "headers", "verification_progress_pct", "initial_block_download",
                                      "connections", "connections_out", "mempool_txs", "mempool_usage_mb",
                                      "size_on_disk_gb", "version", "rpc_ok")}
    if chain == "kas":
        d = cached("kas_node", 10, lambda: _http_json(HOME_BASE + "/api/node/kas"))
        n = d.get("node") or {}
        return {k: n.get(k) for k in ("chain", "client", "status", "network_height", "daa_score", "difficulty",
                                      "network_hashrate_hs", "dag_tips", "block_reward_kas", "block_time_s",
                                      "bps", "next_halving_utc", "next_halving_reward_kas")}
    if chain == "rvn":
        d = cached("nodes", 10, lambda: _http_json(HOME_BASE + "/api/nodes"))
        n = (d.get("nodes") or {}).get("rvn") or {}
        return {k: n.get(k) for k in ("height", "network", "difficulty", "client", "synced", "state", "ok")}
    if chain in ("zec", "doge", "ltc"):
        return svc_status(chain)
    if chain == "utxo":
        raise ValueError("use utxo_chain_status with doge or ltc")
    raise ValueError("chain must be one of: btc, kas, zec, rvn, doge, ltc")


def t_zec_chain_info():
    d = cached("zec_ci", 15, lambda: _http_json(ZEC_BASE + "/api/chaininfo"))
    r = d.get("result") or {}
    pools = {p.get("id"): {"chainValue": p.get("chainValue"), "monitored": p.get("monitored")}
             for p in (r.get("valuePools") or [])}
    return {"blocks": r.get("blocks"), "headers": r.get("headers"), "estimated_height": r.get("estimatedheight"),
            "chainSupply": r.get("chainSupply"), "valuePools": pools, "bestblockhash": r.get("bestblockhash"),
            "difficulty": r.get("difficulty"), "size_on_disk_gb": round((r.get("size_on_disk") or 0) / 1e9, 2)}


def t_zec_recent_blocks(n=5):
    d = cached(f"zec_blocks_{n}", 15, lambda: _http_json(ZEC_BASE + f"/api/blocks?n={int(n)}"))
    return d


def t_btc_fee_estimates():
    out = {}
    for target in (1, 2, 3, 6, 12, 24):
        try:
            r = btc_rpc("estimatesmartfee", [target])
            out[f"{target}_blocks"] = {"btc_per_kvb": r.get("feerate"), "blocks": r.get("blocks")}
        except Exception as e:  # noqa: BLE001
            out[f"{target}_blocks"] = {"error": str(e)[:80]}
    mp = btc_rpc("getmempoolinfo")
    return {"estimates": out, "mempool_min_fee_btc_kvb": mp.get("mempoolminfee"),
            "mempool_txs": mp.get("size"), "mempool_mb": round((mp.get("bytes") or 0) / 1e6, 1)}


def t_btc_mempool_summary():
    def build():
        mp = btc_rpc("getmempoolinfo")
        try:
            fees = btc_rpc("getmempoolancestors", []) if False else None
        except Exception:  # noqa: BLE001
            fees = None
        try:
            hist = btc_rpc("getmempoolentry", []) if False else None
        except Exception:  # noqa: BLE001
            hist = None
        return {"txs": mp.get("size"), "bytes": mp.get("bytes"), "usage_mb": round((mp.get("usage") or 0) / 1e6, 1),
                "min_fee_btc_kvb": mp.get("mempoolminfee"), "max_mempool_mb": round((mp.get("maxmempool") or 0) / 1e6, 1),
                "total_fee_btc": mp.get("total_fee")}
    return cached("btc_mp", 10, build)


def t_btc_tx_lookup(txid):
    txid = (txid or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", txid):
        raise ValueError("txid must be 64 hex chars")
    tx = btc_rpc("getrawtransaction", [txid, True])
    vout = [{"n": v["n"], "value": v["value"], "type": v["scriptPubKey"].get("type"),
             "address": (v["scriptPubKey"].get("address") or (v["scriptPubKey"].get("addresses") or [None])[0])}
            for v in (tx.get("vout") or [])][:12]
    return {"txid": tx.get("txid"), "blockhash": tx.get("blockhash"), "confirmations": tx.get("confirmations"),
            "time": tx.get("time"), "size": tx.get("size") or tx.get("vsize"), "vin_count": len(tx.get("vin") or []),
            "vout": vout, "value_out_btc": round(sum(v["value"] for v in (tx.get("vout") or [])), 8)}


def t_btc_block_summary(height=None, blockhash=None):
    if blockhash:
        bh = blockhash.strip()
    elif height is not None:
        bh = btc_rpc("getblockhash", [int(height)])
    else:
        bh = btc_rpc("getbestblockhash")
    hdr = btc_rpc("getblockheader", [bh, True])
    blk = btc_rpc("getblock", [bh, 1])
    return {"hash": bh, "height": hdr.get("height"), "time": hdr.get("time"), "txs": len(blk.get("tx") or []),
            "size": blk.get("size"), "weight": blk.get("weight"), "confirmations": hdr.get("confirmations"),
            "previousblockhash": hdr.get("previousblockhash")}


def t_utxo_chain_status(chain):
    chain = (chain or "").lower()
    if chain not in ("doge", "ltc"):
        raise ValueError("chain must be doge or ltc")
    return svc_status(chain)


def t_robotbase_services():
    d = cached("home", 15, lambda: _http_json(HOME_BASE + "/api/services"))
    return {"updated_utc": d.get("updated_utc"),
            "services": [{"id": s["id"], "ok": bool(s.get("ok")), "metric": s.get("metric") or s.get("error")}
                         for s in d.get("status", [])]}


# ---------------- KAS / RVN: node + hashport telemetry from the gateway ----------------
def t_kas_node_status():
    """Kaspa node state straight off the gateway's read-only KAS node API."""
    d = cached("kas_node_info", 10, lambda: _http_json(HOME_BASE + "/api/node/kas"))
    n = d.get("node") or {}
    pool = d.get("serves_pool") or {}
    return {"updated_utc": d.get("updated_utc"), "chain": n.get("chain"), "client": n.get("client"),
            "status": n.get("status"), "network_height": n.get("network_height"), "daa_score": n.get("daa_score"),
            "difficulty": n.get("difficulty"), "network_hashrate_hs": n.get("network_hashrate_hs"),
            "dag_tips": n.get("dag_tips"), "block_reward_kas": n.get("block_reward_kas"),
            "block_time_s": n.get("block_time_s"), "bps": n.get("bps"),
            "next_halving_utc": n.get("next_halving_utc"), "next_halving_reward_kas": n.get("next_halving_reward_kas"),
            "hashport": {"ports": pool.get("ports"), "tiers_online": pool.get("tiers_online"),
                         "dashboard": pool.get("dashboard")}}


def t_kas_pool_status():
    """Our own Kaspa solo hashport: fleet state plus the on-chain block it found."""
    d = cached("pools", 10, lambda: _http_json(HOME_BASE + "/api/pools"))
    p = (d.get("pools") or {}).get("kaspool") or {}
    return {"updated_utc": d.get("updated_utc"), "pool": "kaspool", "state": p.get("state"),
            "height": p.get("height"), "network": p.get("network"), "pool_hashrate": p.get("hashrate"),
            "miners": p.get("workers"), "shares": p.get("shares"), "blocks_found": p.get("blocks"),
            "uptime": p.get("uptime"), "share_difficulty": p.get("difficulty"),
            "last_block": {"hash": p.get("block_hash"), "short": p.get("block_short"),
                           "blue_score": p.get("block_bluescore"), "age": p.get("block_age")},
            "stale": p.get("stale"), "invalid": p.get("invalid")}


def t_rvn_node_status():
    """Ravencoin node state (height, network hashrate, difficulty, sync)."""
    d = cached("nodes", 10, lambda: _http_json(HOME_BASE + "/api/nodes"))
    n = (d.get("nodes") or {}).get("rvn") or {}
    return {"updated_utc": d.get("updated_utc"), "chain": "ravencoin", "client": n.get("client"),
            "synced": bool(n.get("synced")), "state": n.get("state"), "height": n.get("height"),
            "network_hashrate": n.get("network"), "difficulty": n.get("difficulty")}


def t_rvn_pool_status():
    """Our own Ravencoin solo hashport: engine state, per-miner coinbase, endpoint."""
    d = cached("pools", 10, lambda: _http_json(HOME_BASE + "/api/pools"))
    p = (d.get("pools") or {}).get("rvnpool") or {}
    return {"updated_utc": d.get("updated_utc"), "pool": "rvnpool", "state": p.get("state"),
            "height": p.get("height"), "network": p.get("network"), "pool_hashrate": p.get("hashrate"),
            "miners": p.get("workers"), "shares": p.get("shares"), "share_difficulty": p.get("difficulty"),
            "sync": p.get("sync"), "stratum": p.get("endpoint"), "fee": p.get("fee"),
            "payout": "per-miner independent coinbase"}


# ---------------- BTC: electrs (Electrum protocol) + address decoding ----------------
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def b58decode(s):
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + raw


def bech32_polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def bech32_decode(addr):
    pos = addr.rfind("1")
    hrp, data = addr[:pos], addr[pos + 1:]
    values = [CHARSET.find(c) for c in data]
    if -1 in values or bech32_polymod([ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + values) != 1:
        raise ValueError("bad bech32 checksum")
    return hrp, values[:-6]


def convertbits(data, frombits, tobits, pad=True):
    acc = bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def address_to_script(addr):
    addr = addr.strip()
    if addr.lower().startswith("bc1"):
        hrp, data = bech32_decode(addr.lower())
        witver = data[0]
        prog = bytes(convertbits(data[1:], 5, 8, False))
        if witver == 0:
            if len(prog) == 20:
                return b"\x00\x14" + prog
            if len(prog) == 32:
                return b"\x00\x20" + prog
        elif witver == 1 and len(prog) == 32:
            return b"\x51\x20" + prog
        raise ValueError("unsupported witness program")
    raw = b58decode(addr)
    if len(raw) != 25:
        raise ValueError("bad base58 address length")
    ver, h160 = raw[0], raw[1:21]
    if ver == 0:
        return b"\x76\xa9\x14" + h160 + b"\x88\xac"
    if ver == 5:
        return b"\xa9\x14" + h160 + b"\x87"
    raise ValueError("unsupported base58 version")


def address_to_scripthash(addr):
    script = address_to_script(addr)
    return hashlib.sha256(script).digest()[::-1].hex()


def electrum_call(method, params, host=None, port=None, timeout=10):
    host = host or os.environ.get("RB_ELECTRS_HOST", "127.0.0.1")
    port = port or int(os.environ.get("RB_ELECTRS_PORT", "50001"))
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall((json.dumps({"id": 1, "method": method, "params": params}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.split(b"\n")[0].decode())


def t_btc_address_summary(address):
    """BTC 地址摘要（2026-09-16 加固：4s 硬预算 + 分步降级 + 独立缓存）

    背景：SOP 要求慢工具 <4000ms；原实现三步各给 8/8/6s 超时（最坏 22s），
    且失败不缓存 → 热门地址会反复打 electrs 并触发 LLM 超时。
    """
    sh = address_to_scripthash(address)
    key = "btcaddr_" + sh
    now = time.time()
    with _cache_lock:
        hit = _addr_cache.get(key)
        if hit and now - hit[0] < hit[2]:
            out = dict(hit[1]); out["cached"] = True; out["cache_age_sec"] = int(now - hit[0])
            return out

    t0 = time.monotonic()

    def left(reserve=0.15):
        return BTC_ADDR_BUDGET - (time.monotonic() - t0) - reserve

    def build():
        notes, degraded = [], False
        # ① 余额（必需，占预算大头）
        bal = None
        try:
            bal = electrum_call("blockchain.scripthash.get_balance", [sh],
                                timeout=max(0.8, min(2.5, left()))).get("result") or {}
        except Exception:  # noqa: BLE001
            bal = None
        if bal is None:
            return {"address": address, "degraded": True, "error": "electrs_timeout",
                    "note": "4s 硬预算内未取到余额：地址过于活跃或 electrs 忙，请稍后重试",
                    "elapsed_ms": int((time.monotonic() - t0) * 1000)}
        # ② UTXO 列表（可选）
        utxos = []
        if left() < 0.5:
            degraded = True; notes.append("预算不足，跳过 UTXO 列表")
        else:
            try:
                utxos = electrum_call("blockchain.scripthash.listunspent", [sh],
                                      timeout=max(0.6, min(1.2, left()))).get("result") or []
            except Exception:  # noqa: BLE001
                degraded = True; notes.append("UTXO 列表未取（活跃地址）")
        # ③ 交易历史（可选，最慢的一步：大户动辄几十万条）
        hist = None
        if left() < 0.6:
            degraded = True; notes.append("预算不足，跳过交易历史")
        else:
            try:
                hist = electrum_call("blockchain.scripthash.get_history", [sh],
                                     timeout=max(0.6, min(1.6, left()))).get("result") or []
            except Exception:  # noqa: BLE001
                degraded = True; notes.append("交易历史未取（地址过于活跃）")
        out = {"address": address,
               "balance_btc": round((bal.get("confirmed") or 0) / 1e8, 8),
               "unconfirmed_btc": round((bal.get("unconfirmed") or 0) / 1e8, 8),
               "utxo_count": len(utxos),
               "utxo_value_btc": round(sum(u.get("value", 0) for u in utxos) / 1e8, 8),
               "degraded": degraded,
               "elapsed_ms": int((time.monotonic() - t0) * 1000)}
        if hist is not None:
            out["tx_count"] = len(hist)
            out["confirmed_tx_count"] = sum(1 for h in hist if h.get("height", 0) > 0)
            out["recent_txs"] = [{"txid": h.get("tx_hash"), "height": h.get("height")} for h in hist[-10:]]
        else:
            out["tx_count"] = None
        if notes:
            out["note"] = "; ".join(notes)
        return out

    with _btc_gate:          # 串行化，避免并发低效查询把 electrs 线程池占满
        val = build()
    ttl = BTC_ADDR_CACHE_TTL_BAD if val.get("degraded") else BTC_ADDR_CACHE_TTL
    with _cache_lock:
        _addr_cache[key] = (now, val, ttl)
    return val




USAGE_DB = os.environ.get("RB_USAGE_DB", "/opt/mcp/usage.db")
BILLING_DB = os.environ.get("RB_BILLING_DB", "/opt/billing/billing.db")


def _db(path, timeout=10):
    con = sqlite3.connect(path, timeout=timeout)
    con.row_factory = sqlite3.Row
    return con


def init_usage():
    con = _db(USAGE_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS calls(
        ts INTEGER, tool TEXT, ok INTEGER, ms INTEGER, ip_hash TEXT, plan TEXT)""")
    con.commit()
    con.close()


def log_call(tool, ok, ms, ip, plan):
    try:
        con = _db(USAGE_DB)
        con.execute("INSERT INTO calls VALUES(?,?,?,?,?,?)",
                    (int(time.time()), tool, 1 if ok else 0, int(ms),
                     hashlib.sha256((ip or "").encode()).hexdigest()[:16], plan))
        con.commit()
        con.close()
    except Exception:
        pass


def _has_billing():
    try:
        open(BILLING_DB).close()
        return True
    except OSError:
        return False


def usage_stats(hours=24):
    since = int(time.time()) - hours * 3600
    con = _db(USAGE_DB)
    total = con.execute("SELECT COUNT(*) c FROM calls WHERE ts>=?", (since,)).fetchone()["c"]
    errs = con.execute("SELECT COUNT(*) c FROM calls WHERE ts>=? AND ok=0", (since,)).fetchone()["c"]
    top = con.execute("SELECT tool, COUNT(*) c, AVG(ms) avg_ms FROM calls WHERE ts>=? GROUP BY tool ORDER BY c DESC LIMIT 15",
                      (since,)).fetchall()
    clients = con.execute("SELECT COUNT(DISTINCT ip_hash) c FROM calls WHERE ts>=?", (since,)).fetchone()["c"]
    keyed = con.execute("SELECT COUNT(*) c FROM calls WHERE ts>=? AND plan='key'", (since,)).fetchone()["c"]
    con.close()
    active_keys = 0
    if _has_billing():
        try:
            bcon = _db(BILLING_DB)
            active_keys = bcon.execute("SELECT COUNT(*) c FROM api_keys WHERE active=1").fetchone()["c"]
            bcon.close()
        except Exception:
            active_keys = 0
    return {"window_hours": hours, "total_calls": total, "errors": errs, "unique_clients": clients,
            "keyed_calls": keyed, "active_keys": active_keys,
            "top_tools": [{"tool": r["tool"], "calls": r["c"], "avg_ms": round(r["avg_ms"] or 0)} for r in top]}


def usage_hourly(hours=24):
    since = int(time.time()) - hours * 3600
    con = _db(USAGE_DB)
    rows = con.execute("SELECT ts FROM calls WHERE ts>=?", (since,)).fetchall()
    con.close()
    buckets = {}
    for r in rows:
        key = r["ts"] // 3600
        buckets[key] = buckets.get(key, 0) + 1
    now_h = int(time.time()) // 3600
    series = []
    for i in range(hours - 1, -1, -1):
        h = now_h - i
        series.append({"hour": time.strftime("%m-%d %H:00", time.localtime(h * 3600)), "calls": buckets.get(h, 0)})
    return series


def verify_key(key):
    if not key or not _has_billing():
        return "anon"
    try:
        con = _db(BILLING_DB)
        h = hashlib.sha256(key.encode()).hexdigest()
        row = con.execute("SELECT active, expires_at FROM api_keys WHERE key_hash=?", (h,)).fetchone()
        con.close()
        if not row or not row["active"]:
            return "anon"
        if row["expires_at"] and row["expires_at"] < time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()):
            return "anon"
        return "key"
    except Exception:
        return "anon"


# =================== v0.5.0: mining / fee / attribution intelligence =============
# Every datum here comes from infrastructure we run ourselves. Chains where we have
# no source are reported as unavailable rather than filled with a guess.

KAS_BLOCK_DB = os.environ.get("RB_KAS_BLOCK_DB", "/var/lib/robotbase-home/kas_blocks.db")
# KAS stratum bridges (one HTTP stats endpoint per tier). The host comes from a single
# constant so the public build can swap it for RB_KAS_BRIDGE_BASE.
KAS_BRIDGE_BASE = os.environ.get("RB_KAS_BRIDGE_BASE", "http://127.0.0.1")
HOME = HOME_BASE

# Halving schedules (public consensus rules) + the live height comes from our nodes.
HALVING = {
    "btc":  {"next_height": 1050000, "interval": 210000, "reward": "3.125", "next_reward": "1.5625",
             "block_time_s": 600, "unit": "BTC", "note": "4-year epoch, 210,000 blocks"},
    "zec":  {"next_height": 4406400, "interval": 1680000, "reward": "1.5625", "next_reward": "0.78125",
             "block_time_s": 75, "unit": "ZEC", "note": "post-Blossom 1,680,000-block interval"},
    "ltc":  {"next_height": 3360000, "interval": 840000, "reward": "6.25", "next_reward": "3.125",
             "block_time_s": 150, "unit": "LTC", "note": "840,000-block interval"},
    "rvn":  {"next_height": 6300000, "interval": 2100000, "reward": "2500", "next_reward": "1250",
             "block_time_s": 60, "unit": "RVN", "note": "2,100,000-block interval"},
    "doge": {"next_height": None, "interval": None, "reward": "10000", "next_reward": "10000",
             "block_time_s": 60, "unit": "DOGE",
             "note": "no further halvings: flat 10,000 DOGE per block since block 600,000"},
}

ZIP317_FEE = 0.00001          # Zcash conventional fee per logical action (ZIP-317)


def _heights():
    """Live heights from the gateway (our own nodes)."""
    d = cached("nodes", 15, lambda: _http_json(HOME + "/api/nodes"))
    out = {}
    for cid, node in (d.get("nodes") or {}).items():
        h = str(node.get("height") or "").replace("#", "").replace(",", "")
        try:
            out[cid] = int(float(h))
        except ValueError:
            out[cid] = None
    return out


def t_pow_halving_oracle():
    heights = _heights()
    out = []
    for cid, spec in HALVING.items():
        cur = heights.get(cid)
        if spec["next_height"] is None:
            out.append({"chain": cid.upper(), "next_halving": None,
                        "reason": spec["note"], "current_reward": spec["reward"] + " " + spec["unit"]})
            continue
        remaining = None if cur is None else max(0, spec["next_height"] - cur)
        item = {"chain": cid.upper(), "height": cur, "next_halving_height": spec["next_height"],
                "blocks_remaining": remaining,
                "eta_days": round(remaining * spec["block_time_s"] / 86400, 1) if remaining is not None else None,
                "reward_now": spec["reward"] + " " + spec["unit"],
                "reward_after": spec["next_reward"] + " " + spec["unit"],
                "schedule": spec["note"]}
        out.append(item)
    # Kaspa: epoch-based reduction, read from our own node
    try:
        k = (_http_json(HOME + "/api/node/kas").get("node") or {})
        out.append({"chain": "KAS", "height": k.get("network_height"),
                    "next_halving_utc": k.get("next_halving_utc"),
                    "reward_now": f"{k.get('block_reward_kas')} KAS" if k.get("block_reward_kas") else None,
                    "reward_after": f"{k.get('next_halving_reward_kas')} KAS" if k.get("next_halving_reward_kas") else None,
                    "schedule": "deflationary epoch reduction (monthly, 2^(1/12) per epoch)", "source": "our kaspad"})
    except Exception:  # noqa: BLE001
        out.append({"chain": "KAS", "next_halving_utc": None, "reason": "kas node unreachable"})
    return {"chains": out, "source": "our own full nodes + published consensus schedules",
            "note": "heights are read live; schedules are protocol constants, not estimates"}


def t_pow_network_mining_intel():
    """Network hashrate / difficulty per chain, with the method stated per chain."""
    out = {}
    try:                                                        # BTC: real node RPC
        out["BTC"] = {"difficulty": btc_rpc("getdifficulty"),
                      "network_hashrate_hs": btc_rpc("getnetworkhashps"),
                      "source": "our bitcoind getnetworkhashps"}
    except Exception as exc:  # noqa: BLE001
        out["BTC"] = {"error": type(exc).__name__}
    try:                                                        # KAS: our node API
        n = (_http_json(HOME + "/api/node/kas").get("node") or {})
        out["KAS"] = {"difficulty": n.get("difficulty"), "network_hashrate_hs": n.get("network_hashrate_hs"),
                      "height": n.get("network_height"), "bps": n.get("bps"), "source": "our kaspad"}
    except Exception as exc:  # noqa: BLE001
        out["KAS"] = {"error": type(exc).__name__}
    try:                                                        # RVN: our node stats API
        n = (_http_json(HOME + "/api/pools").get("pools", {}).get("rvnpool") or {})
        out["RVN"] = {"difficulty": n.get("difficulty"), "network_hashrate": n.get("network"),
                      "height": n.get("height"), "source": "our ravend"}
    except Exception as exc:  # noqa: BLE001
        out["RVN"] = {"error": type(exc).__name__}
    for cid, key, base, factor, block_s in (("ZEC", "zec", ZEC_BASE, 8192, 75),
                                            ("LTC", "ltc", LTC_BASE, 65536, 150),
                                            ("DOGE", "doge", DOGE_BASE, 65536, 60)):
        try:
            d = _http_json(base + "/api/status")
            diff = d.get("difficulty")
            out[cid] = {"difficulty": diff, "height": d.get("blocks"),
                        "network_hashrate_hs": (float(diff) * factor / block_s) if diff else None,
                        "hashrate_method": "difficulty-derived (difficulty * %d / %ds)" % (factor, block_s),
                        "source": "our %s node status API" % key}
        except Exception as exc:  # noqa: BLE001
            out[cid] = {"error": type(exc).__name__}
    return {"chains": out,
            "note": "BTC/KAS/RVN are node-reported; ZEC/LTC/DOGE hashrate is derived from difficulty with the "
                    "factor printed next to it. No third-party API is involved."}


def t_get_recommended_fee_rate(chain=None):
    cid = (chain or "btc").lower()
    if cid == "btc":
        try:
            fast = btc_rpc("estimatesmartfee", [1]) or {}
            med = btc_rpc("estimatesmartfee", [3]) or {}
            slow = btc_rpc("estimatesmartfee", [10]) or {}
            mi = btc_rpc("getmempoolinfo") or {}
            to_sat = lambda f: round(float(f) * 1e8 / 1000, 2) if f else None
            return {"chain": "BTC", "unit": "sat/vB",
                    "fast": {"target_blocks": 1, "sat_vb": to_sat(fast.get("feerate"))},
                    "medium": {"target_blocks": 3, "sat_vb": to_sat(med.get("feerate"))},
                    "slow": {"target_blocks": 10, "sat_vb": to_sat(slow.get("feerate"))},
                    "mempool_min_sat_vb": to_sat(mi.get("mempoolminfee")),
                    "source": "our bitcoind estimatesmartfee + getmempoolinfo",
                    "note": "agent can pick a tier and broadcast with broadcast_raw_transaction"}
        except Exception as exc:  # noqa: BLE001
            return {"chain": "BTC", "error": type(exc).__name__}
    if cid == "zec":
        return {"chain": "ZEC", "unit": "ZEC per logical action",
                "conventional_fee": ZIP317_FEE,
                "source": "ZIP-317 conventional fee (protocol rule)",
                "note": "Zcash has no mempool fee auction like Bitcoin — ZIP-317 defines a fixed "
                        "conventional fee; we report the protocol value, not a market estimate."}
    return {"chain": cid.upper(), "available": False,
            "reason": "we do not run a fee estimator for this chain yet (no node RPC exposed to the gateway)",
            "what_we_do_have": ["chain_status", "utxo_chain_status", "mempool_congestion_status"]}


def t_mempool_congestion_status(chain=None):
    cid = (chain or "btc").lower()
    if cid == "btc":
        try:
            mi = btc_rpc("getmempoolinfo") or {}
            return {"chain": "BTC", "txs": mi.get("size"), "bytes": mi.get("bytes"),
                    "usage_bytes": mi.get("usage"), "max_mempool_bytes": mi.get("maxmempool"),
                    "min_fee_btc_per_kvb": mi.get("mempoolminfee"),
                    "total_fees_btc": mi.get("total_fee"),
                    "load_pct": round(100 * (mi.get("usage") or 0) / (mi.get("maxmempool") or 1), 1),
                    "verdict": "busy" if (mi.get("usage") or 0) > 0.5 * (mi.get("maxmempool") or 1) else "normal",
                    "source": "our bitcoind getmempoolinfo"}
        except Exception as exc:  # noqa: BLE001
            return {"chain": "BTC", "error": type(exc).__name__}
    if cid in ("ltc", "doge"):
        try:
            d = svc_status(cid)
            return {"chain": cid.upper(), "txs": d.get("mempool_txs"),
                    "bytes": None, "min_fee": None,
                    "source": "our %s node status API" % cid,
                    "note": "tx count is live; byte size and min-fee are not exposed by that node API yet"}
        except Exception as exc:  # noqa: BLE001
            return {"chain": cid.upper(), "error": type(exc).__name__}
    return {"chain": cid.upper(), "available": False,
            "reason": "no mempool endpoint exposed for this chain on our nodes yet"}


def t_zec_shielded_pools_metrics():
    """Six Zcash value pools with share-of-supply and 1h/24h deltas."""
    d = cached("factors", 60, lambda: _http_json(HOME + "/api/factors"))
    g = ((d.get("groups") or {}).get("zec") or {})
    meta = ((d.get("groups") or {}).get("meta") or {})
    if not g:
        return {"available": False, "reason": "ZEC factor sampler unavailable"}
    has24 = str(meta.get("has_24h", "")).lower() not in ("", "no", "false")
    pools = []
    for pid in ("transparent", "sprout", "sapling", "orchard", "ironwood", "lockbox"):
        bal = g.get(pid)
        if not bal or bal == "—":
            continue
        try:
            num = float(str(bal).replace(",", "").split()[0])
        except ValueError:
            num = None
        entry = {"pool": pid, "balance": bal,
                 "share_of_supply_pct": (round(num / float(str(g.get("supply", "0")).replace(",", "").split()[0]) * 100, 2)
                                         if num and g.get("supply") else None),
                 "delta_24h": g.get(pid + "_delta") if has24 else None,
                 "delta_1h": g.get(pid + "_delta_1h")}
        pools.append(entry)
    return {"pools": pools, "shielded_total": g.get("shielded"), "supply": g.get("supply"),
            "shielded_pct": g.get("shielded_pct"),
            "shielded_delta_24h": g.get("shielded_delta_24h") if has24 else None,
            "window": "24h" if has24 else "1h",
            "source": "our Zebra node value-pool RPC, sampled by our own factor index",
            "why_it_matters": "pool-by-pool capital allocation across privacy pools is not exposed by "
                              "ordinary block explorers or by any other MCP server we know of"}


def t_kas_pool_attribution_intel(hours=24):
    """Local Kaspa block->pool attribution index (we index every block ourselves)."""
    hours = max(1, min(24 * 30, int(hours or 24)))
    since = int(time.time()) - hours * 3600
    try:
        con = sqlite3.connect("file:" + KAS_BLOCK_DB + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        total = con.execute("SELECT COUNT(*) c FROM blocks WHERE ts>=?", (since,)).fetchone()["c"]
        rows = con.execute("SELECT pool, COUNT(*) c FROM blocks WHERE ts>=? GROUP BY pool ORDER BY c DESC",
                           (since,)).fetchall()
        overall = con.execute("SELECT COUNT(*) c, MIN(ts) a, MAX(ts) b FROM blocks").fetchone()
        latest = [dict(r) for r in con.execute("SELECT hash, ts, blue, pool FROM blocks ORDER BY ts DESC LIMIT 3")]
        con.close()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"{type(exc).__name__}: {str(exc)[:80]}"}
    pools = [{"pool": r["pool"], "blocks": r["c"],
              "share_pct": round(100.0 * r["c"] / total, 2) if total else None} for r in rows]
    return {"window_hours": hours, "blocks_in_window": total,
            "pool_distribution": pools,
            "index": {"blocks_indexed": overall["c"],
                      "from": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(overall["a"])),
                      "to": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(overall["b"]))},
            "latest_blocks": latest,
            "source": "our own Kaspa block index (every block we sample from our kaspad is attributed to the "
                      "pool that found it)",
            "note": "this is chain-wide pool attribution computed locally — not a third-party API"}


def t_robotbase_pool_worker_query(chain=None, wallet=None, worker=None):
    """Per-worker stats for a miner, straight from our pool engines."""
    cid = (chain or "").lower()
    if not cid or not (wallet or worker):
        return {"error": "pass chain (kas|zec|rvn) plus wallet or worker"}
    wallet = (wallet or "").strip()
    worker = (worker or "").strip()
    if cid == "kas":
        found = []
        for spec in ((KAS_BRIDGE_BASE + ":2114", 5555), (KAS_BRIDGE_BASE + ":2115", 5558),
                     (KAS_BRIDGE_BASE + ":2116", 5559)):
            try:
                d = _http_json(spec[0] + "/api/stats", timeout=8)
            except Exception:  # noqa: BLE001
                continue
            for w in d.get("workers") or []:
                if (wallet and str(w.get("wallet")) == wallet) or (worker and str(w.get("worker")) == worker):
                    found.append({"port": spec[1], "worker": w.get("worker"),
                                  "wallet": _mask(w.get("wallet")),
                                  "status": w.get("status"), "hashrate_ghs": w.get("hashrate"),
                                  "shares": w.get("shares"), "stale": w.get("stale"),
                                  "invalid": w.get("invalid"), "blocks": w.get("blocks"),
                                  "current_difficulty": w.get("currentDifficulty"),
                                  "last_seen": w.get("lastSeen")})
        if not found:
            return {"chain": "KAS", "found": False,
                    "reason": "no worker on our KAS hashport matches that wallet/worker right now "
                              "(miners are only listed while connected)"}
        return {"chain": "KAS", "found": True, "workers": found,
                "source": "our Kaspa stratum bridges (ports 5555/5558/5559)"}
    if cid in ("zec", "rvn"):
        return {"chain": cid.upper(), "found": False, "available": False,
                "reason": "our %s pool engine does not expose a per-worker query endpoint to the gateway yet "
                          "(pool-level telemetry is available via %s_pool_status)"
                          % (cid.upper(), cid)}
    return {"error": "chain must be kas, zec or rvn"}


def t_broadcast_raw_transaction(chain=None, signed_raw_tx_hex=None):
    """Relay an already-signed transaction to the network through our own node.

    Non-custodial by construction: we never see a key. Disabled unless the operator
    sets RB_ENABLE_BROADCAST=1, and every relay is validated with testmempoolaccept
    first so our nodes are not used as a blind spam relay.
    """
    if os.environ.get("RB_ENABLE_BROADCAST", "0") != "1":
        return {"enabled": False,
                "reason": "broadcast is switched off on this deployment (RB_ENABLE_BROADCAST=0)",
                "policy": "sign locally, then ask the operator to enable relaying — we never take custody"}
    cid = (chain or "").lower()
    raw = (signed_raw_tx_hex or "").strip()
    if not raw:
        return {"error": "signed_raw_tx_hex is required"}
    if cid != "btc":
        return {"chain": cid.upper() or None, "available": False,
                "reason": "relay is wired for BTC only right now"}
    try:
        check = btc_rpc("testmempoolaccept", [[raw]]) or []
        verdict = check[0] if check else {}
        if not verdict.get("allowed"):
            return {"chain": "BTC", "accepted": False,
                    "reject_reason": verdict.get("reject-reason") or "rejected by our node",
                    "note": "nothing was relayed"}
        txid = btc_rpc("sendrawtransaction", [raw])
        return {"chain": "BTC", "accepted": True, "txid": txid,
                "source": "relayed through our own bitcoind", "vsize": verdict.get("vsize")}
    except Exception as exc:  # noqa: BLE001
        return {"chain": "BTC", "accepted": False, "error": type(exc).__name__,
                "detail": str(exc)[:160]}


def _mask(addr):
    a = str(addr or "")
    return a[:10] + "…" + a[-6:] if len(a) > 20 else a




# ============ v0.5.1/v0.5.2: ZEC coinbase attribution + RVN assets =============
# Both endpoints live on our own nodes and are read-only by construction.
RVN_ASSET_BASE = os.environ.get("RB_RVN_ASSET_BASE", "http://127.0.0.1:18081")


def t_zec_block_attribution_intel(blocks=200):
    """MVP attribution: shielded coinbases, output roles and readable pool tags.

    Everything here is provable from the chain itself: (a) whether a coinbase
    carries a shielded output, (b) which coinbase outputs are consensus funding
    streams (the lockbox, seen in ~every block) versus real miner payouts, and
    (c) the pool tag a miner printed into its own coinbase text. No third-party
    label list is applied, so we only ever repeat what a miner wrote itself.
    """
    try:
        blocks = max(10, min(500, int(blocks or 200)))
    except (TypeError, ValueError):
        blocks = 200
    try:
        d = _http_json(ZEC_BASE + "/api/coinbase-scan?blocks=%d" % blocks, timeout=90)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": "%s: %s" % (type(exc).__name__, str(exc)[:100])}
    return {
        "window": d.get("window"),
        "scanned_blocks": d.get("scanned_blocks"),
        "rpc_errors": d.get("rpc_errors"),
        "shielded_coinbase_blocks": d.get("shielded_coinbase_blocks"),
        "shielded_coinbase_pct": d.get("shielded_coinbase_pct"),
        "recurring_funding_outputs": d.get("recurring_funding_outputs"),
        "miner_payout_addresses_top": d.get("miner_payout_addresses_top"),
        "coinbase_tags_top": d.get("coinbase_tags_top"),
        "tag_named_blocks": d.get("tag_named_blocks"),
        "tag_named_pct": d.get("tag_named_pct"),
        "tagged_blocks": d.get("tagged_blocks"),
        "tag_matched": d.get("tag_matched"),
        "shielded_heights_sample": d.get("shielded_heights_sample"),
        "source": "our Zebra node, getblock verbosity 2 (coinbase script + vout + vShieldedOutput)",
        "scope": "chain facts only — no third-party pool label list is applied",
        "reading_the_data": {
            "recurring_funding_outputs": "an address paying out in >=60% of blocks is a consensus "
                                         "funding stream (lockbox), NOT a miner payout",
            "miner_payout_addresses_top": "coinbase outputs with those funding streams excluded",
            "coinbase_tags_top": "the pool name each miner printed into its own coinbase text; "
                                 "'no readable tag' means the miner embedded none",
        },
        "why_it_matters": "the share of block rewards that land in a shielded pool is a privacy metric "
                          "the usual block explorers do not expose to models at all",
    }


def t_rvn_asset_lookup(asset=None, limit=20):
    """Ravencoin native assets: one asset by name/id, or a listing."""
    name = (asset or "").strip()
    try:
        if name:
            d = _http_json(RVN_ASSET_BASE + "/api/asset/" + urllib.parse.quote(name), timeout=30)
            if not d.get("ok"):
                return {"found": False, "asset": name, "reason": d.get("error") or "not found"}
            return {"found": True, "asset": d.get("result"),
                    "source": "our ravend getassetdata",
                    "note": "Ravencoin assets are UTXO-native: amount, units, reissuable and IPFS metadata"}
        limit = max(1, min(200, int(limit or 20)))
        d = _http_json(RVN_ASSET_BASE + "/api/assets?limit=%d" % limit, timeout=30)
        return {"count": d.get("count"), "assets": d.get("assets"),
                "source": "our ravend listassets", "hint": "pass asset=<name or id> for full details"}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": "%s: %s" % (type(exc).__name__, str(exc)[:100])}



TOOLS = [
    ("list_chains",
     "Every chain this service supports (BTC/KAS/ZEC/RVN/DOGE/LTC) with live availability and block height. "
     "When to use: the user asks which chains you support, which nodes are online, or how high each chain is. "
     "Do not use: for detail on one chain, use chain_status.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_list_chains()),
    ("chain_status",
     "Run-time status of one chain's node: block height, sync progress, connected peers, mempool tx count, client version. "
     "When to use: whether a given chain's node is synced, healthy or lagging. "
     "Do not use: fees → btc_fee_estimates; address balance → btc_address_summary; pool detail → kas_pool_status / rvn_pool_status.",
     {"type": "object", "properties": {"chain": {"type": "string",
                                                 "enum": ["btc", "kas", "zec", "rvn", "doge", "ltc"],
                                                 "description": "Chain id: btc / kas / zec / rvn / doge / ltc"}},
      "required": ["chain"], "additionalProperties": False}, lambda a: t_chain_status(a.get("chain"))),
    ("zec_chain_info",
     "Zcash mainnet info including the supply of all six value pools (transparent/sprout/sapling/orchard/lockbox/ironwood) — i.e. shielded-pool state. "
     "When to use: how much ZEC sits in the shielded/sapling/orchard pools, or privacy-pool size. "
     "Do not use: whether the ZEC node is synced → chain_status(zec).",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_zec_chain_info()),
    ("zec_recent_blocks",
     "Height, hash, block time and difficulty of the most recent N Zcash blocks (N ≤ 20). "
     "When to use: check whether ZEC is producing blocks normally, and at what interval.",
     {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5,
                                             "description": "How many recent blocks to return; default 5"}},
      "additionalProperties": False}, lambda a: t_zec_recent_blocks(a.get("n", 5))),
    ("btc_fee_estimates",
     "Recommended Bitcoin fees: rates (BTC/kvB) for 1/2/3/6/12/24-block confirmation targets, plus the mempool minimum fee. "
     "When to use: how much fee to pay, or what gets a fast confirmation. "
     "Do not use: overall congestion level → btc_mempool_summary.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_btc_fee_estimates()),
    ("btc_mempool_summary",
     "Bitcoin mempool overview: pending tx count, bytes used, minimum fee, total fees, capacity limit. "
     "When to use: is the network congested right now, or how big the backlog is.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_btc_mempool_summary()),
    ("btc_tx_lookup",
     "Look up a Bitcoin transaction by txid: confirmed or not, block height, confirmations, size, input/output summary. "
     "When to use: the user gives a 64-hex BTC txid and asks whether it confirmed or which block it is in. "
     "Do not use: other chains → chain_status.",
     {"type": "object", "properties": {"txid": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$",
                                                "description": "Bitcoin transaction hash (64 hex chars)"}},
      "required": ["txid"], "additionalProperties": False}, lambda a: t_btc_tx_lookup(a.get("txid"))),
    ("btc_block_summary",
     "Bitcoin block summary: tx count, size, weight, block time, confirmations; omit parameters for the current chain tip. "
     "When to use: how many transactions the latest block holds, or an overview of a given height or block hash.",
     {"type": "object", "properties": {"height": {"type": "integer", "description": "Block height (optional)"},
                                       "blockhash": {"type": "string", "description": "Block hash (optional; use instead of height)"}},
      "additionalProperties": False}, lambda a: t_btc_block_summary(a.get("height"), a.get("blockhash"))),
    ("btc_address_summary",
     "Balance and activity of a Bitcoin address: confirmed/unconfirmed balance, UTXO count and total, tx count, last 10 transactions. "
     "Supports P2PKH (1…), P2SH (3…), bech32 (bc1q…), bech32m (bc1p…). "
     "When to use: how much BTC this address holds, whether it received funds, how active it is. "
     "Note: ultra-active addresses such as exchange cold wallets may return a degraded response under index load.",
     {"type": "object", "properties": {"address": {"type": "string", "description": "Bitcoin mainnet address"}},
      "required": ["address"], "additionalProperties": False}, lambda a: t_btc_address_summary(a.get("address"))),
    ("kas_node_status",
     "Kaspa node status: network height, DAA score, difficulty, network hashrate, DAG tips, block reward, 10 BPS cadence, next halving and the hashport ports this node serves. "
     "When to use: how high and how healthy the Kaspa node is, or the halving schedule. "
     "Do not use: our own pool's miners/blocks → kas_pool_status.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_kas_node_status()),
    ("kas_pool_status",
     "RobotBase Kaspa solo hashport: tier state, pool hashrate, connected miners, accepted shares, blocks found, uptime, share difficulty, the last block found (hash, blue score, age) and stale/invalid counts. "
     "When to use: is the KAS hashport live, who is mining on it, did it ever find a real mainnet block. "
     "Do not use: chain-level hashrate → kas_node_status.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_kas_pool_status()),
    ("rvn_node_status",
     "Ravencoin node status: height, sync state, network hashrate, difficulty and client version. "
     "When to use: whether the RVN node is synced and how big the network is right now.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_rvn_node_status()),
    ("rvn_pool_status",
     "RobotBase Ravencoin solo hashport: engine state, pool hashrate, connected miners, shares, share difficulty, the Stratum endpoint, fee and the per-miner independent coinbase payout mode. "
     "When to use: is the RVN hashport open, is anyone mining, what endpoint do I point a GPU rig at. "
     "Do not use: node-level data → rvn_node_status.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_rvn_pool_status()),
    ("pow_halving_oracle",
     "Halving countdown for every chain we run: height, next halving height, blocks remaining, ETA in days, and the reward before/after. "
     "When to use: an agent needs a precise schedule anchor (emissions, mining economics, long-horizon planning). "
     "Note: heights are read live from our own nodes, schedules are protocol constants. DOGE has no further halvings (flat subsidy).",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_pow_halving_oracle()),
    ("pow_network_mining_intel",
     "Network hashrate and difficulty for BTC/KAS/ZEC/RVN/LTC/DOGE, with the method stated per chain (node-reported vs difficulty-derived). "
     "When to use: mining economics, security budget, or comparing chain weight. "
     "Do not use: pool-level stats → kas_pool_status / rvn_pool_status.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_pow_network_mining_intel()),
    ("get_recommended_fee_rate",
     "Fee recommendation tiers for a chain: fast / medium / slow plus the mempool minimum. "
     "BTC is computed from our own node's fee estimator; ZEC returns the ZIP-317 conventional fee (protocol rule, not a market estimate); "
     "chains without an estimator say so instead of guessing. "
     "When to use: an autonomous agent is about to send a transaction and must pick a fee.",
     {"type": "object", "properties": {"chain": {"type": "string", "enum": ["btc", "zec", "ltc", "doge", "rvn"],
                                                 "description": "btc / zec / ltc / doge / rvn"}},
      "required": ["chain"], "additionalProperties": False}, lambda a: t_get_recommended_fee_rate(a.get("chain"))),
    ("mempool_congestion_status",
     "Mempool congestion for BTC (txs, bytes, usage vs capacity, min fee, total fees, busy/normal verdict) and tx counts for LTC/DOGE where our node API exposes them. "
     "When to use: decide whether now is a good moment for an on-chain settlement.",
     {"type": "object", "properties": {"chain": {"type": "string", "enum": ["btc", "ltc", "doge"],
                                                 "description": "btc / ltc / doge"}},
      "required": ["chain"], "additionalProperties": False}, lambda a: t_mempool_congestion_status(a.get("chain"))),
    ("zec_shielded_pools_metrics",
     "All six Zcash value pools (transparent, sprout, sapling, orchard, ironwood, lockbox) with balances, share of supply and 1h/24h deltas. "
     "When to use: privacy-pool capital allocation, shielded-supply trends, ZEC macro flows. "
     "Do not use: node health → chain_status(zec).",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_zec_shielded_pools_metrics()),
    ("kas_pool_attribution_intel",
     "Kaspa chain-wide pool attribution computed locally: which pool found how many blocks, with percentage share, over a window of 1-720 hours, plus the newest attributed blocks. "
     "When to use: Kaspa mining decentralisation, competitor share, or checking how a pool performs. "
     "Powered by our own block index (hundreds of thousands of blocks attributed), not a third-party API.",
     {"type": "object", "properties": {"hours": {"type": "integer", "minimum": 1, "maximum": 720, "default": 24,
                                                 "description": "Look-back window in hours (default 24)"}},
      "additionalProperties": False}, lambda a: t_kas_pool_attribution_intel(a.get("hours", 24))),
    ("robotbase_pool_worker_query",
     "Look up one miner on our own hashports: hashrate, shares, stale/invalid, current difficulty and last-seen, by wallet address or worker name. "
     "When to use: a miner asks their agent \"how is my rig doing on robotbase?\". "
     "Privacy: the address is masked in the reply, and only an exact wallet/worker match returns data (no listings).",
     {"type": "object", "properties": {"chain": {"type": "string", "enum": ["kas", "zec", "rvn"]},
                                       "wallet": {"type": "string", "description": "Miner payout address (optional)"},
                                       "worker": {"type": "string", "description": "Worker name (optional)"}},
      "required": ["chain"], "additionalProperties": False},
     lambda a: t_robotbase_pool_worker_query(a.get("chain"), a.get("wallet"), a.get("worker"))),
    ("broadcast_raw_transaction",
     "Relay an already-signed raw transaction to the network through our own full node (non-custodial: we never see a private key). "
     "The transaction is first validated with testmempoolaccept; rejected transactions are never relayed. "
     "Disabled by default on this deployment and enabled per-operator with RB_ENABLE_BROADCAST=1. "
     "When to use: an agent signed locally and wants a high-availability broadcast path.",
     {"type": "object", "properties": {"chain": {"type": "string", "enum": ["btc"]},
                                       "signed_raw_tx_hex": {"type": "string", "description": "Raw signed transaction hex"}},
      "required": ["chain", "signed_raw_tx_hex"], "additionalProperties": False},
     lambda a: t_broadcast_raw_transaction(a.get("chain"), a.get("signed_raw_tx_hex"))),
    ("zec_block_attribution_intel",
     "Zcash coinbase attribution over the last 10-500 blocks: the shielded-pool share of block rewards (privacy mining), coinbase outputs split into consensus funding streams (lockbox) vs real miner payout addresses, and the pool tags miners printed into their own coinbase text plus our own /RobotBase/ tag. "
     "When to use: privacy-pool mining trends, shielded adoption, or checking whether our own pool found blocks. "
     "Scope: chain facts only — no third-party pool label list is applied.",
     {"type": "object", "properties": {"blocks": {"type": "integer", "minimum": 10, "maximum": 500, "default": 200,
                                                  "description": "How many recent blocks to classify (default 200)"}},
      "additionalProperties": False}, lambda a: t_zec_block_attribution_intel(a.get("blocks", 200))),
    ("rvn_asset_lookup",
     "Ravencoin native assets straight from our ravend: pass an asset name or id for amount, units, reissuable flag and IPFS metadata, or omit it to list recent assets. "
     "When to use: token/asset checks on Ravencoin, where assets are UTXO-native rather than smart contracts.",
     {"type": "object", "properties": {"asset": {"type": "string", "description": "Asset name or id (optional)"},
                                       "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20,
                                                 "description": "How many assets to list when no name is given"}},
      "additionalProperties": False}, lambda a: t_rvn_asset_lookup(a.get("asset"), a.get("limit", 20))),
    ("utxo_chain_status",
     "Node status for DOGE or LTC (height, sync progress, peers, mempool tx count). "
     "When to use: Dogecoin or Litecoin node progress. For BTC/KAS/ZEC/RVN use chain_status (any chain in one call).",
     {"type": "object", "properties": {"chain": {"type": "string", "enum": ["doge", "ltc"],
                                                 "description": "doge or ltc"}},
      "required": ["chain"], "additionalProperties": False}, lambda a: t_utxo_chain_status(a.get("chain"))),
    ("robotbase_services",
     "Live availability of every service behind the RobotBase gateway: the chain nodes, hashport engines, Web3 Agent Hub, AITOKENS and MCP itself. "
     "When to use: an overall health check, or which services are down.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_robotbase_services()),
]


def tools_list():
    return [{"name": n, "description": d, "inputSchema": s} for n, d, s, _ in TOOLS]


def call_tool(name, args):
    for n, _d, _s, fn in TOOLS:
        if n == name:
            return fn(args or {})
    raise ValueError(f"unknown tool: {name}")


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": ("RobotBase read-only on-chain data service: BTC/KAS/ZEC/RVN/DOGE/LTC. "
                             "Every tool is a read-only query; none of them execute trades or touch funds.")}}
    if method == "notifications/initialized" or mid is None:
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools_list()}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}}
    if method == "tools/call":
        name = params.get("name")
        try:
            out = call_tool(name, params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": truncate(out)}],
                                                            "isError": False}}
        except Exception as exc:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"error: {exc}"[:300]}],
                                                            "isError": True}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}


DOC_TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RobotBase MCP · read-only data API for AI agents on six PoW chains</title>
<meta name="description" content="Read-only MCP server covering the classic non-EVM PoW chains (BTC/KAS/ZEC/RVN/DOGE/LTC). No API key, no tracking.">
<style>
:root{--bg:#070a0f;--panel:#0f141c;--line:#1f2733;--text:#e8eef6;--muted:#8b98a9;--brand:#ff7a2f;--brand2:#ffc46b}
*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;
background:radial-gradient(900px 420px at 15% -10%,rgba(255,122,47,.16),transparent 60%),radial-gradient(700px 380px at 90% 0%,rgba(124,92,255,.14),transparent 55%),var(--bg);
color:var(--text);margin:0;padding:44px 20px;line-height:1.6}.wrap{max-width:1000px;margin:0 auto}
h1{font-size:clamp(26px,4vw,38px);margin:0 0 8px;background:linear-gradient(100deg,#fff,#ffd9b8 55%,var(--brand));
-webkit-background-clip:text;background-clip:text;color:transparent}
.tag{color:var(--brand2);font-size:15px;margin-bottom:6px}
.sub{color:var(--muted);font-size:13.5px;margin-bottom:28px}
h2{font-size:16px;margin:34px 0 12px;color:var(--brand2)}
.card{background:linear-gradient(160deg,rgba(23,30,42,.8),rgba(15,20,28,.85));border:1px solid var(--line);border-radius:16px;padding:18px;margin-bottom:14px}
pre{background:#0d1219;border:1px solid var(--line);border-radius:12px;padding:14px;overflow:auto;font-size:13px;margin:0}
code{font-family:ui-monospace,Menlo,monospace;background:#0d1219;padding:2px 6px;border-radius:6px;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top}
th{background:var(--panel);color:var(--brand2)}tr.grp td{background:#111823;color:var(--brand2);font-weight:600;letter-spacing:.06em}
.when{color:var(--muted);font-size:12.5px;margin-top:4px}
.btn{display:inline-block;background:linear-gradient(120deg,var(--brand2),var(--brand));color:#0a0d12;text-decoration:none;
border:0;border-radius:10px;padding:9px 15px;font-weight:700;font-size:13.5px;cursor:pointer;margin-right:8px}
.btn.ghost{background:transparent;color:var(--brand2);border:1px solid rgba(255,196,107,.35)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
a{color:var(--brand2)}.muted{color:var(--muted);font-size:12.5px}.ok{color:#2ee6a8}
.toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:#16202b;border:1px solid var(--line);
border-radius:10px;padding:9px 14px;font-size:13px;opacity:0;transition:.25s;pointer-events:none}.toast.on{opacity:1}
</style></head><body><div class="wrap">
<h1>RobotBase MCP Server</h1>
<div class="tag">Read-only MCP server for the six classic PoW chains — BTC / KAS / ZEC / RVN / DOGE / LTC · no API key · no tracking</div>
<div class="sub">Let Claude, Cursor, Codex, VS Code or any MCP-capable agent query Bitcoin mempool fees, Zcash shielded-pool supply, Kaspa and Ravencoin node + hashport state and more — straight from our own bare-metal nodes. Every tool is read-only; none touch trading or funds.</div>

<div class="card">
  <div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center">
    <a class="btn" href="#connect">Get connected</a>
    <a class="btn ghost" href="/mcp/stats?hours=24">Live usage stats</a>
    <a class="btn ghost" href="/mcp/tools">Tool list (JSON)</a>
    <a class="btn ghost" href="/mcp/server.json">Server Card</a>
  </div>
</div>

<h2 id="connect">One-line setup</h2>
<div class="card">
  <p class="muted">Claude Desktop / Cursor / Codex / VS Code (remote MCP) — copy and go:</p>
<pre id="cfg">{
  "mcpServers": {
    "robotbase": {
      "url": "https://robotbase.cc/mcp"
    }
  }
}</pre>
  <p style="margin-top:12px"><button class="btn" onclick="copyCfg()">Copy config JSON</button>
  <button class="btn ghost" onclick="copyText('https://robotbase.cc/mcp')">Copy endpoint URL</button></p>
  <p class="muted">Verify from the command line:</p>
<pre>curl -s https://robotbase.cc/mcp -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"zec_chain_info","arguments":{}}}'</pre>
</div>

<h2>All 16 read-only tools</h2>
<div class="card" style="padding:0;overflow:hidden">
<table><tr><th style="width:230px">Tool</th><th>What it returns</th></tr>__TOOLS_ROWS__</table>
</div>

<h2>Architecture</h2>
<div class="card"><pre>External AI agent ──HTTPS (Streamable HTTP)──► https://robotbase.cc/mcp
                                            │ Cloudflare Tunnel
                                            ▼
                      9108 gateway (/mcp reverse proxy) ──► MCP service (:8090)
                        ├── BTC  9382   full node RPC + electrs (address/tx index)
                        ├── ZEC  9308   Zebra full node (incl. value-pool / shielded supply)
                        ├── DOGE 9309   full node
                        ├── LTC  9310   full node
                        └── gateway service aggregation (status pages / dashboards)</pre></div>

<h2>Rate limits and privacy</h2>
<div class="card"><div class="grid">
<div><div class="muted">Anonymous</div><div><b>120 req/min/IP</b></div></div>
<div><div class="muted">With API key</div><div><b>600 req/min/IP</b> (<code>X-API-Key</code>)</div></div>
<div><div class="muted">Privacy</div><div>Audit keeps only the first 16 chars of the IP SHA256 — <b>query arguments are never logged</b></div></div>
<div><div class="muted">Data</div><div>Read-only on-chain data; no trading, signing or custody</div></div>
</div></div>

<h2>Example prompts</h2>
<div class="card"><ul style="margin:0;padding-left:20px">
<li>"What is the cheapest BTC fee right now?" → <code>btc_fee_estimates</code></li>
<li>"How much ZEC is in the Zcash shielded pools?" → <code>zec_chain_info</code></li>
<li>"Did our Kaspa hashport ever find a real mainnet block?" → <code>kas_pool_status</code></li>
<li>"What is the balance of this Bitcoin address?" → <code>btc_address_summary</code></li>
<li>"How far along is the Dogecoin node?" → <code>utxo_chain_status</code></li>
</ul></div>

<p class="muted" style="margin-top:26px">RobotBase · read-only on-chain data infrastructure · <a href="https://robotbase.cc/">robotbase.cc</a> ·
endpoint <code>/mcp</code> · tool list <code>/mcp/tools</code> · audit <code>/mcp/stats</code></p>
</div>
<script>
function copyText(s){navigator.clipboard&&navigator.clipboard.writeText(s).then(toast).catch(fallback);function fallback(){var t=document.createElement("textarea");t.value=s;document.body.appendChild(t);t.select();document.execCommand("copy");t.remove();toast()}}
function copyCfg(){copyText(document.getElementById("cfg").innerText)}
function toast(){var el=document.getElementById("t")||Object.assign(document.body.appendChild(document.createElement("div")),{id:"t",className:"toast"});el.textContent="Copied to clipboard";el.classList.add("on");setTimeout(function(){el.classList.remove("on")},1600)}
</script>
</body></html>"""


def docs_html():
    groups = [
        ("① Chain overview", ["list_chains", "chain_status", "robotbase_services"]),
        ("② Bitcoin (BTC)", ["btc_fee_estimates", "btc_mempool_summary", "btc_tx_lookup", "btc_block_summary", "btc_address_summary"]),
        ("③ Zcash (ZEC)", ["zec_chain_info", "zec_recent_blocks"]),
        ("④ Kaspa (KAS)", ["kas_node_status", "kas_pool_status"]),
        ("⑤ Ravencoin (RVN)", ["rvn_node_status", "rvn_pool_status"]),
        ("⑥ Dogecoin / Litecoin", ["utxo_chain_status"]),
        ("⑦ Mining & fee intelligence", ["pow_halving_oracle", "pow_network_mining_intel",
                                         "get_recommended_fee_rate", "mempool_congestion_status",
                                         "robotbase_pool_worker_query"]),
        ("⑧ Exclusive local indexes", ["zec_shielded_pools_metrics", "kas_pool_attribution_intel",
                                       "zec_block_attribution_intel", "rvn_asset_lookup",
                                       "broadcast_raw_transaction"]),
    ]
    desc = {n: d for n, d, _s, _f in TOOLS}
    rows = ""
    for gname, names in groups:
        rows += '<tr class="grp"><td colspan="2">' + gname + '</td></tr>'
        for n in names:
            d = desc.get(n, "")
            short = d.split("When to use")[0].split("Do not use")[0].split("Note:")[0].strip().rstrip(".") + "."
            when = ""
            if "When to use:" in d:
                when = d.split("When to use:")[1].split("Do not use")[0].split("Note:")[0].strip().rstrip(".")
            rows += ('<tr><td><code>' + n + '</code></td><td>' + short +
                     ('<div class="when">When to use: ' + when + '</div>' if when else '') + '</td></tr>')
    return DOC_TEMPLATE.replace("__TOOLS_ROWS__", rows)


def server_card():
    desc = {n: d.split("【")[0].strip() for n, d, _s, _f in TOOLS}
    return {
        "name": "robotbase-mcp",
        "title": "RobotBase MCP Server",
        "version": "0.5.2",
        "description": "Read-only multi-chain data for AI agents: BTC / KAS / ZEC / RVN / DOGE / LTC. "
                       "First MCP server covering six classic proof-of-work chains: mempool & fee estimates, "
                       "transaction lookup, address summary, shielded-pool supply, node status. No auth required, no tracking.",
        "homepage": "https://robotbase.cc/mcp",
        "transport": {"type": "streamable-http", "url": "https://robotbase.cc/mcp"},
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "auth": {"type": "none", "optional": "X-API-Key header or ?key= raises rate limit to 600/min per IP"},
        "rateLimits": {"anonymous": "120/min per IP", "withApiKey": "600/min per IP"},
        "tags": ["bitcoin", "kaspa", "zcash", "ravencoin", "dogecoin", "litecoin", "blockchain-data", "onchain",
                 "mempool", "fees", "privacy-coins", "read-only", "mcp-server"],
        "tools": [{"name": n, "description": desc[n]} for n, _d, _s, _f in TOOLS],
    }


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Mcp-Session-Id, Accept, X-API-Key")
        plan = getattr(self, "_plan_name", None)
        if plan:
            self.send_header("X-MCP-Plan", plan)
            self.send_header("X-RateLimit-Limit", str(600 if plan == "key" else RATE_LIMIT_PER_MIN))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _plan(self):
        key = self.headers.get("X-API-Key")
        if not key and "key=" in (self.path or ""):
            key = self.path.split("key=", 1)[1].split("&")[0]
        if key:
            self._plan_name = verify_key(key)
        else:
            self._plan_name = "anon"
        return self._plan_name

    def _rate_ok(self, limit):
        ip = (self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For") or self.client_address[0])
        now = time.time()
        with _rl_lock:
            win = _rl.setdefault(ip, [])
            win[:] = [t for t in win if now - t < 60]
            if len(win) >= limit:
                return False
            win.append(now)
        return True

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        if self.path.startswith("/healthz"):
            return self._send(200, json.dumps({"ok": True, "tools": len(TOOLS)}))
        if self.path.startswith("/stats"):
            hours = 24
            if "hours=" in self.path:
                try:
                    hours = max(1, min(int(self.path.split("hours=")[1].split("&")[0]), 720))
                except ValueError:
                    pass
            accept = self.headers.get("Accept") or ""
            want_html = ("format=html" in self.path) or ("format=json" not in self.path and "text/html" in accept)
            if not want_html:
                return self._send(200, json.dumps(usage_stats(hours), ensure_ascii=False))
            d = usage_stats(hours)
            series = usage_hourly(hours)
            peak = max([s["calls"] for s in series] or [1]) or 1
            bars = "".join(
                f'<div class="bar" title="{s["hour"]}: {s["calls"]}" style="height:{(s["calls"]/peak*100) if s["calls"] else 1:.0f}%"></div>'
                for s in series)
            rows = "".join(
                f'<tr><td>{html.escape(t2["tool"])}</td><td>{t2["calls"]}</td>'
                f'<td>{round(t2["calls"]/max(d["total_calls"],1)*100,1)}%</td><td>{t2["avg_ms"]} ms</td></tr>'
                for t2 in d["top_tools"]) or '<tr><td colspan="4" class="muted">No calls yet</td></tr>'
            page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>RobotBase MCP · usage audit</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;background:#070a0f;color:#e8eef6;margin:0;padding:36px 20px}}
.wrap{{max-width:960px;margin:0 auto}}h1{{margin:0 0 4px;font-size:24px}}.sub{{color:#8b98a9;margin-bottom:24px;font-size:13px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:26px}}
.card{{background:linear-gradient(160deg,rgba(23,30,42,.85),rgba(15,20,28,.9));border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:14px 16px}}
.k{{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:#5c6878}}.v{{font-family:ui-monospace,Menlo,monospace;font-size:22px;margin-top:6px}}
.bars{{display:flex;align-items:flex-end;gap:3px;height:120px;background:#0d1219;border:1px solid #1f2733;border-radius:12px;padding:10px;margin-bottom:26px}}
.bar{{flex:1;background:linear-gradient(180deg,#ffc46b,#ff7a2f);border-radius:2px;min-height:2px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}td,th{{border:1px solid #1f2733;padding:8px 10px;text-align:left}}
th{{background:#0f141c;color:#ffc46b}}.muted{{color:#8b949e}}a{{color:#ffc46b}}code{{background:#0d1219;padding:2px 6px;border-radius:6px}}
h2{{font-size:15px;margin:26px 0 10px;color:#ffc46b}}</style></head><body><div class="wrap">
<h1>RobotBase MCP · usage audit</h1>
<div class="sub">Window: last <b>{hours}</b> hours · source <code>/opt/mcp/usage.db</code> · only the first 16 chars of the IP SHA256 are kept (irreversible)</div>
<div class="cards">
<div class="card"><div class="k">Total calls</div><div class="v">{d["total_calls"]}</div></div>
<div class="card"><div class="k">Errors</div><div class="v">{d["errors"]}</div></div>
<div class="card"><div class="k">Unique clients</div><div class="v">{d["unique_clients"]}</div></div>
<div class="card"><div class="k">Keyed calls</div><div class="v">{d["keyed_calls"]}</div></div>
<div class="card"><div class="k">Active keys</div><div class="v">{d["active_keys"]}</div></div>
</div>
<h2>Calls per hour</h2><div class="bars">{bars}</div>
<h2>Tool ranking</h2>
<table><tr><th>Tool</th><th>Calls</th><th>Share</th><th>Avg latency</th></tr>{rows}</table>
<p class="sub" style="margin-top:18px">JSON: <a href="/mcp/stats?hours={hours}&amp;format=json">/mcp/stats?format=json</a> ·
other windows: <a href="/mcp/stats?hours=1">1h</a> · <a href="/mcp/stats?hours=6">6h</a> · <a href="/mcp/stats?hours=24">24h</a> · <a href="/mcp/stats?hours=168">7d</a></p>
</div></body></html>"""
            return self._send(200, page, "text/html; charset=utf-8")
        if self.path.startswith("/tools"):
            return self._send(200, json.dumps({"tools": tools_list()}, ensure_ascii=False))
        if self.path.startswith("/server.json") or self.path.startswith("/.well-known/mcp"):
            return self._send(200, json.dumps(server_card(), ensure_ascii=False))
        return self._send(200, docs_html(), "text/html; charset=utf-8")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        plan = self._plan()
        limit = 600 if plan == "key" else RATE_LIMIT_PER_MIN
        if not self._rate_ok(limit):
            return self._send(429, json.dumps({"jsonrpc": "2.0", "id": None,
                                               "error": {"code": -32000,
                                                         "message": f"rate limited ({limit}/min per IP); "
                                                                    f"use X-API-Key for higher limits"}}),
                              extra={"X-RateLimit-Limit": str(limit)})
        try:
            msg = json.loads(raw.decode() or "{}")
        except Exception:  # noqa: BLE001
            return self._send(400, json.dumps({"jsonrpc": "2.0", "id": None,
                                               "error": {"code": -32700, "message": "parse error"}}))
        batch = isinstance(msg, list)
        msgs = msg if batch else [msg]
        started = time.time()
        responses = [r for r in (handle(m) for m in msgs) if r is not None]
        for m, r in zip([x for x in msgs], responses or []):
            if isinstance(m, dict) and m.get("method") == "tools/call":
                name = ((m.get("params") or {}).get("name") or "?")
                ok = not ((r.get("result") or {}).get("isError") is True)
                log_call(name, ok, (time.time() - started) * 1000,
                             (self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For") or self.client_address[0]), plan)
        if not responses:
            return self._send(202, b"")
        payload = responses if batch else responses[0]
        accept = (self.headers.get("Accept") or "").lower()
        body = json.dumps(payload, ensure_ascii=False)
        if "text/event-stream" in accept:
            sse = "event: message\ndata: " + body + "\n\n"
            return self._send(200, sse, "text/event-stream", {"Cache-Control": "no-store"})
        return self._send(200, body)


if __name__ == "__main__":
    init_usage()
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
