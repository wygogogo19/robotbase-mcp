#!/usr/bin/env python3
"""RobotBase MCP Server — read-only multi-chain data tools (BTC / XMR / ZEC / DOGE / LTC).

Open-source reference implementation. It talks to YOUR OWN nodes; configure every endpoint
with the RB_* environment variables below (see examples/env.example). The hosted service is
available at https://robotbase.cc/mcp with no setup required.

Read-only by design: no trading, no signing, no custody.
"""
import base64, hashlib, html, json, os, re, socket, sqlite3, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("RB_PORT", "8090"))
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "robotbase-mcp", "version": "0.3.0", "title": "RobotBase 链上数据 MCP"}
# ---------------- configuration (all via environment variables) ----------------
# Optional env file holding BTC_RPC_URL / BTC_RPC_USER / BTC_RPC_PASS (same as RB_BTC_RPC_* vars)
BTC_ENV = os.environ.get("RB_BTC_ENV", "")
BTC_RPC_URL = os.environ.get("RB_BTC_RPC_URL", "http://127.0.0.1:8332")
BTC_RPC_USER = os.environ.get("RB_BTC_RPC_USER", "")
BTC_RPC_PASS = os.environ.get("RB_BTC_RPC_PASS", "")
BTC_DASHBOARD = os.environ.get("RB_BTC_DASHBOARD", "http://127.0.0.1:8600/api/node")
ELECTRS_HOST = os.environ.get("RB_ELECTRS_HOST", "127.0.0.1")
ELECTRS_PORT = int(os.environ.get("RB_ELECTRS_PORT", "50001"))
XMR_RPC = os.environ.get("RB_XMR_RPC", "http://127.0.0.1:18089/json_rpc")
XMR_HELPER = os.environ.get("RB_XMR_HELPER", "http://127.0.0.1:18085/rpc")
ZEC_BASE = os.environ.get("RB_ZEC_BASE", "http://127.0.0.1:8080")
DOGE_BASE = os.environ.get("RB_DOGE_BASE", "http://127.0.0.1:8080")
LTC_BASE = os.environ.get("RB_LTC_BASE", "http://127.0.0.1:8080")
HOME_BASE = os.environ.get("RB_HOME_BASE", "http://127.0.0.1:8081")
_cache, _cache_lock = {}, threading.Lock()
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
    url = os.environ.get("RB_BTC_RPC_URL") or (BTC_ENV and _cfg(BTC_ENV, "BTC_RPC_URL")) or BTC_RPC_URL
    user = os.environ.get("RB_BTC_RPC_USER") or (BTC_ENV and _cfg(BTC_ENV, "BTC_RPC_USER")) or BTC_RPC_USER
    pw = os.environ.get("RB_BTC_RPC_PASS") or (BTC_ENV and _cfg(BTC_ENV, "BTC_RPC_PASS")) or BTC_RPC_PASS
    creds = base64.b64encode(f"{user}:{pw}".encode()).decode()
    d = _http_json(url, {"jsonrpc": "1.0", "id": "mcp", "method": method, "params": params or []},
                   headers={"Authorization": "Basic " + creds})
    if d.get("error"):
        raise RuntimeError(str(d["error"]))
    return d.get("result")


def xmr_rpc(method, params=None):
    d = _http_json(XMR_RPC, {"jsonrpc": "2.0", "id": "mcp", "method": method, "params": params or {}})
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


def t_list_chains():
    d = cached("home", 15, lambda: _http_json(HOME_BASE + "/api/services"))
    out = []
    for s in d.get("status", []):
        if s.get("id") in ("btc", "xmr", "zec", "doge", "ltc"):
            out.append({"chain": s["id"], "ok": bool(s.get("ok")), "metric": s.get("metric") or s.get("error")})
    return {"chains": out, "updated_utc": d.get("updated_utc")}


def t_chain_status(chain):
    chain = (chain or "").lower()
    if chain == "btc":
        d = cached("btc_node", 10, lambda: _http_json(BTC_DASHBOARD))
        return {k: d.get(k) for k in ("chain", "blocks", "headers", "verification_progress_pct", "initial_block_download",
                                      "connections", "connections_out", "mempool_txs", "mempool_usage_mb",
                                      "size_on_disk_gb", "version", "rpc_ok")}
    if chain == "xmr":
        r = xmr_rpc("get_info")
        return {k: r.get(k) for k in ("height", "target_height", "synchronized", "status", "difficulty",
                                      "outgoing_connections_count", "incoming_connections_count", "version",
                                      "database_size", "tx_pool_size", "mainnet")}
    if chain in ("zec", "doge", "ltc"):
        return svc_status(chain)
    raise ValueError("chain must be one of: btc, xmr, zec, doge, ltc")


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
            out[f"{target}块"] = {"btc_per_kvb": r.get("feerate"), "blocks": r.get("blocks")}
        except Exception as e:  # noqa: BLE001
            out[f"{target}块"] = {"error": str(e)[:80]}
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


def t_xmr_node_info():
    r = xmr_rpc("get_info")
    return {k: r.get(k) for k in ("height", "target_height", "synchronized", "status", "difficulty", "version",
                                  "outgoing_connections_count", "incoming_connections_count", "database_size",
                                  "tx_pool_size", "free_space", "nettype")}


def t_xmr_fee_estimate():
    r = xmr_rpc("get_fee_estimate")
    return {"fee_per_byte": r.get("fee"), "fees": r.get("fees"), "quantization_mask": r.get("quantization_mask"),
            "status": r.get("status")}


def t_xmr_last_block():
    r = xmr_rpc("get_last_block_header")
    h = (r or {}).get("block_header") or {}
    return {"height": h.get("height"), "hash": h.get("hash"), "timestamp": h.get("timestamp"),
            "difficulty": h.get("difficulty"), "reward": h.get("reward"), "block_size": h.get("block_size"),
            "num_txes": h.get("num_txes")}


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
    host = host or ELECTRS_HOST
    port = port or ELECTRS_PORT
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
    sh = address_to_scripthash(address)

    def build():
        try:
            bal = electrum_call("blockchain.scripthash.get_balance", [sh], timeout=8).get("result") or {}
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("该 BTC 地址活动量过大（或 electrs 繁忙）导致查询超时，请改用普通地址或稍后重试") from exc
        note = None
        try:
            utxos = electrum_call("blockchain.scripthash.listunspent", [sh], timeout=8).get("result") or []
        except Exception:  # noqa: BLE001
            utxos, note = [], "utxo 列表超时（地址过于活跃）"
        hist = None
        try:
            hist = electrum_call("blockchain.scripthash.get_history", [sh], timeout=6).get("result") or []
        except Exception:  # noqa: BLE001
            note = (note + "；" if note else "") + "历史记录过大，已跳过"
        out = {"address": address,
               "balance_btc": round((bal.get("confirmed") or 0) / 1e8, 8),
               "unconfirmed_btc": round((bal.get("unconfirmed") or 0) / 1e8, 8),
               "utxo_count": len(utxos),
               "utxo_value_btc": round(sum(u.get("value", 0) for u in utxos) / 1e8, 8)}
        if hist is not None:
            out["tx_count"] = len(hist)
            out["confirmed_tx_count"] = sum(h.get("height", 0) > 0 for h in hist)
            out["recent_txs"] = [{"txid": h.get("tx_hash"), "height": h.get("height")} for h in hist[-10:]]
        else:
            out["tx_count"] = None
        if note:
            out["note"] = note
        return out

    return cached("addr_" + sh, 60, build)




def xmr_full(method, params=None):
    d = _http_json(XMR_HELPER, {"method": method, "params": params or {}}, timeout=28)
    if not d.get("ok"):
        raise RuntimeError(str(d.get("error"))[:160])
    return d.get("result")


def t_xmr_mempool_stats():
    ps = (xmr_full("get_transaction_pool_stats") or {}).get("pool_stats") or {}
    histo = ps.get("histo") or []
    return {"bytes_total": ps.get("bytes_total"), "fee_total_atomic": ps.get("fee_total"),
            "bytes_min": ps.get("bytes_min"), "bytes_med": ps.get("bytes_med"), "bytes_max": ps.get("bytes_max"),
            "tx_count": sum(h.get("txs", 0) for h in histo), "histogram_top": histo[:6]}


def t_xmr_tx_lookup(txid):
    txid = (txid or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", txid):
        raise ValueError("txid must be 64 hex chars")
    r = xmr_full("get_transactions", {"txs_hashes": [txid], "decode_as_json": True}) or {}
    txs = r.get("txs") or []
    if not txs:
        return {"txid": txid, "found": False, "status": r.get("status")}
    tx = txs[0]
    j = tx.get("as_json")
    if isinstance(j, str):
        try:
            j = json.loads(j)
        except Exception:
            j = {}
    j = j or {}
    height = tx.get("block_height") or j.get("block_height")
    out = {"txid": txid, "found": True, "in_pool": tx.get("in_pool"),
           "block_height": height if isinstance(height, int) and height > 0 else None,
           "unlock_time": j.get("unlock_time"),
           "vin_count": len(j.get("vin") or []), "vout_count": len(j.get("vout") or []),
           "output_indices": (tx.get("output_indices") or [])[:8]}
    if out["block_height"]:
        try:
            info = xmr_rpc("get_info")
            out["confirmations"] = max(0, (info.get("height") or 0) - out["block_height"] + 1)
        except Exception:
            pass
    return out


_HERE = os.path.dirname(os.path.abspath(__file__))
USAGE_DB = os.environ.get("RB_USAGE_DB", os.path.join(_HERE, "usage.db"))
BILLING_DB = os.environ.get("RB_BILLING_DB", "")


def _db(path, timeout=10):
    con = sqlite3.connect(path, timeout=timeout)
    con.row_factory = sqlite3.Row
    return con


def init_usage():
    try:
        con = _db(USAGE_DB)
        con.execute("""CREATE TABLE IF NOT EXISTS calls(
            ts INTEGER, tool TEXT, ok INTEGER, ms INTEGER, ip_hash TEXT, plan TEXT)""")
        con.commit()
        con.close()
    except Exception:  # read-only filesystem or missing dir: auditing is optional
        pass


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
    if not BILLING_DB:
        return False
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


TOOLS = [
    ("list_chains",
     "列出本服务支持的全部公链（BTC/XMR/ZEC/DOGE/LTC）及其实时可用性与区块高度。"
     "【何时用】用户问“你支持哪些链 / 哪些节点在线 / 各链现在多高”。"
     "【不要用】查单条链的细节状态请用 chain_status。",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_list_chains()),
    ("chain_status",
     "查询【某一条链】节点的运行状态：区块高度、同步进度、已连接节点数、内存池笔数、客户端版本。"
     "【何时用】“某条链的节点是否同步/健康/落后”。"
     "【不要用】问手续费→btc_fee_estimates 或 xmr_fee_estimate；问地址余额→btc_address_summary。",
     {"type": "object", "properties": {"chain": {"type": "string", "enum": ["btc", "xmr", "zec", "doge", "ltc"],
                                                 "description": "链标识：btc / xmr / zec / doge / ltc"}},
      "required": ["chain"], "additionalProperties": False}, lambda a: t_chain_status(a.get("chain"))),
    ("zec_chain_info",
     "查询 Zcash 主网信息，含 6 个价值池供应量（transparent/sprout/sapling/orchard/lockbox/ironwood），即【屏蔽池状态】。"
     "【何时用】“Zcash 的 shielded/sapling/orchard 池子里有多少 ZEC / 隐私池规模”。"
     "【不要用】查 ZEC 节点是否同步→chain_status(zec)。",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_zec_chain_info()),
    ("zec_recent_blocks",
     "查询 Zcash 最近 N 个区块的高度、哈希、出块时间与难度（N 最大 20）。"
     "【何时用】看 ZEC 最近出块是否正常、出块间隔。",
     {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5,
                                             "description": "返回最近多少个区块，默认 5"}},
      "additionalProperties": False}, lambda a: t_zec_recent_blocks(a.get("n", 5))),
    ("btc_fee_estimates",
     "查询比特币【推荐手续费】：按 1/2/3/6/12/24 个区块确认目标给出费率（BTC/kvB），并给出内存池最低费率。"
     "【何时用】“转账该付多少手续费 / 多少能快速确认”。"
     "【不要用】问网络拥堵程度→btc_mempool_summary。",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_btc_fee_estimates()),
    ("btc_mempool_summary",
     "查询比特币内存池概况：待确认笔数、占用字节、最低费率、总手续费、容量上限。"
     "【何时用】“现在网络堵不堵 / 内存池积压多少”。",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_btc_mempool_summary()),
    ("btc_tx_lookup",
     "按 txid 查询【比特币交易】：是否已确认、所在区块高度、确认数、大小、输入/输出摘要。"
     "【何时用】用户给出 64 位十六进制 BTC 交易哈希，问“这笔交易确认了吗/在哪个块”。"
     "【不要用】门罗币交易请用 xmr_tx_lookup。",
     {"type": "object", "properties": {"txid": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$",
                                                "description": "比特币交易哈希（64 位十六进制）"}},
      "required": ["txid"], "additionalProperties": False}, lambda a: t_btc_tx_lookup(a.get("txid"))),
    ("btc_block_summary",
     "查询【比特币区块】摘要：交易数、大小、权重、出块时间、确认数；不传参数则取当前链尖区块。"
     "【何时用】“最新区块有多少笔交易 / 某个高度或区块哈希的概况”。",
     {"type": "object", "properties": {"height": {"type": "integer", "description": "区块高度（可选）"},
                                       "blockhash": {"type": "string", "description": "区块哈希（可选，与 height 二选一）"}},
      "additionalProperties": False}, lambda a: t_btc_block_summary(a.get("height"), a.get("blockhash"))),
    ("btc_address_summary",
     "查询【比特币地址】的余额与活动：已确认/未确认余额、UTXO 数量与总额、交易笔数、最近 10 笔交易。"
     "支持 P2PKH（1…）、P2SH（3…）、bech32（bc1q…）、bech32m（bc1p…）。"
     "【何时用】“这个地址有多少 BTC / 有没有收到款 / 活跃度”。"
     "【注意】交易所冷钱包等超活跃地址可能因索引负载返回降级提示。",
     {"type": "object", "properties": {"address": {"type": "string", "description": "比特币主网地址"}},
      "required": ["address"], "additionalProperties": False}, lambda a: t_btc_address_summary(a.get("address"))),
    ("xmr_node_info",
     "查询门罗币【全节点信息】：高度、是否同步、难度、出/入连接数、数据库大小、内存池笔数、版本。"
     "【何时用】“XMR 节点多高了 / 同步好了吗 / 有没有连上网络”。",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_xmr_node_info()),
    ("xmr_fee_estimate",
     "查询门罗币【手续费估算】：按字节费率与分档费率（低/中/高档）、量化掩码。"
     "【何时用】“XMR 转账要多少手续费 / 现在费率多少”。",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_xmr_fee_estimate()),
    ("xmr_last_block",
     "查询门罗币【最新区块头】：高度、哈希、时间戳、难度、区块奖励、交易数。"
     "【何时用】“XMR 最新出块时间 / 最新高度 / 是否卡块”。",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_xmr_last_block()),
    ("xmr_tx_lookup",
     "按 txid 查询【门罗币交易】：是否仍在内存池、是否已上链、所在区块高度与确认数、输入/输出数量、output indices。"
     "【何时用】判断“某笔 XMR 交易/原子交换是否已上链、是否超时未确认”。"
     "【不要用】比特币交易请用 btc_tx_lookup。",
     {"type": "object", "properties": {"txid": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$",
                                                "description": "门罗币交易哈希（64 位十六进制）"}},
      "required": ["txid"], "additionalProperties": False}, lambda a: t_xmr_tx_lookup(a.get("txid"))),
    ("xmr_mempool_stats",
     "查询门罗币【内存池统计】：总字节数、手续费合计、按体积分组的交易直方图（拥堵程度）。"
     "【何时用】“XMR 现在拥堵吗 / 内存池里多少笔”。",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_xmr_mempool_stats()),
    ("utxo_chain_status",
     "查询 DOGE 或 LTC 的节点状态（高度、同步进度、连接数、内存池笔数）。"
     "【何时用】问狗狗币/莱特币节点进度。ZEC/BTC/XMR 请用 chain_status（一次可传任意链）。",
     {"type": "object", "properties": {"chain": {"type": "string", "enum": ["doge", "ltc"],
                                                 "description": "doge 或 ltc"}},
      "required": ["chain"], "additionalProperties": False}, lambda a: t_utxo_chain_status(a.get("chain"))),
    ("robotbase_services",
     "查询 RobotBase 网关下【全部服务】的实时可用状态：5 条链节点 + Web3 Agent Hub + AITOKENS + MCP 自身。"
     "【何时用】“整体巡检 / 有哪些服务挂了”。",
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
            "instructions": ("RobotBase 只读链上数据服务：BTC/XMR/ZEC/DOGE/LTC。"
                             "所有工具均为只读查询，不执行任何交易或资金操作。")}}
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


DOC_TEMPLATE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RobotBase MCP · 五大经典公链的 AI Agent 数据接口</title>
<meta name="description" content="全网首个覆盖 BTC/XMR/ZEC/DOGE/LTC 5 大非 EVM 经典公链的只读高可用 MCP Server，免鉴权，无追踪。">
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
<div class="tag">全网首个覆盖 BTC / XMR / ZEC / DOGE / LTC 五大非 EVM 经典公链的只读高可用 MCP Server · 免鉴权 · 无追踪</div>
<div class="sub">让 Claude、Cursor、VS Code、ChatGPT 等 AI Agent 直接查询比特币内存池费率、门罗币交易与内存池、Zcash 屏蔽池供应量等链上数据。全部工具只读，不涉及交易与资金。</div>

<div class="card">
  <div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center">
    <a class="btn" href="#connect">接入方式</a>
    <a class="btn ghost" href="/mcp/stats?hours=24">实时调用统计</a>
    <a class="btn ghost" href="/mcp/tools">工具清单 JSON</a>
    <a class="btn ghost" href="/mcp/server.json">Server Card</a>
  </div>
</div>

<h2 id="connect">一键接入</h2>
<div class="card">
  <p class="muted">Claude Desktop / Cursor / VS Code（远程 MCP）配置，复制即用：</p>
<pre id="cfg">{
  "mcpServers": {
    "robotbase": {
      "url": "https://robotbase.cc/mcp"
    }
  }
}</pre>
  <p style="margin-top:12px"><button class="btn" onclick="copyCfg()">复制配置 JSON</button>
  <button class="btn ghost" onclick="copyText('https://robotbase.cc/mcp')">复制端点 URL</button></p>
  <p class="muted">命令行验证：</p>
<pre>curl -s https://robotbase.cc/mcp -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"zec_chain_info","arguments":{}}}'</pre>
</div>

<h2>16 个只读工具</h2>
<div class="card" style="padding:0;overflow:hidden">
<table><tr><th style="width:230px">工具</th><th>说明</th></tr>__TOOLS_ROWS__</table>
</div>

<h2>架构</h2>
<div class="card"><pre>外部 AI Agent ──HTTPS(Streamable HTTP)──► https://robotbase.cc/mcp
                                            │ Cloudflare Tunnel
                                            ▼
                      9108 网关 (/mcp 反代) ──► MCP 服务 (:8090)
                        ├── BTC  9382   全节点 RPC + electrs（地址/交易索引）
                        ├── XMR  9281   全节点（restricted RPC + 白名单 helper）
                        ├── ZEC  9308   Zebra 全节点（含价值池/屏蔽池供应量）
                        ├── DOGE 9309   全节点
                        ├── LTC  9310   全节点
                        └── 网关服务聚合（状态页 / 面板）</pre></div>

<h2>限流与隐私</h2>
<div class="card"><div class="grid">
<div><div class="muted">匿名额度</div><div><b>120 次/分钟/IP</b></div></div>
<div><div class="muted">API Key 额度</div><div><b>600 次/分钟/IP</b>（<code>X-API-Key</code>）</div></div>
<div><div class="muted">隐私</div><div>审计仅存 IP 的 SHA256 前 16 位，<b>不记录查询参数</b></div></div>
<div><div class="muted">数据性质</div><div>只读链上数据，不含交易/签名/托管</div></div>
</div></div>

<h2>示例提问</h2>
<div class="card"><ul style="margin:0;padding-left:20px">
<li>“现在 BTC 最划算的转账手续费是多少？” → <code>btc_fee_estimates</code></li>
<li>“Zcash 的 shielded 池子现在有多少 ZEC？” → <code>zec_chain_info</code></li>
<li>“帮我查这笔 XMR 交易上链了没、几确认了？” → <code>xmr_tx_lookup</code></li>
<li>“这个比特币地址还有多少余额？” → <code>btc_address_summary</code></li>
<li>“狗狗币节点追到哪了？” → <code>utxo_chain_status</code></li>
</ul></div>

<p class="muted" style="margin-top:26px">RobotBase · 只读链上数据基础设施 · <a href="https://robotbase.cc/">robotbase.cc</a> ·
端点 <code>/mcp</code> · 清单 <code>/mcp/tools</code> · 审计 <code>/mcp/stats</code></p>
</div>
<script>
function copyText(s){navigator.clipboard&&navigator.clipboard.writeText(s).then(toast).catch(fallback);function fallback(){var t=document.createElement("textarea");t.value=s;document.body.appendChild(t);t.select();document.execCommand("copy");t.remove();toast()}}
function copyCfg(){copyText(document.getElementById("cfg").innerText)}
function toast(){var el=document.getElementById("t")||Object.assign(document.body.appendChild(document.createElement("div")),{id:"t",className:"toast"});el.textContent="已复制到剪贴板";el.classList.add("on");setTimeout(function(){el.classList.remove("on")},1600)}
</script>
</body></html>"""


def docs_html():
    groups = [
        ("① 链状态总览", ["list_chains", "chain_status", "robotbase_services"]),
        ("② 比特币 BTC", ["btc_fee_estimates", "btc_mempool_summary", "btc_tx_lookup", "btc_block_summary", "btc_address_summary"]),
        ("③ 门罗币 XMR", ["xmr_node_info", "xmr_fee_estimate", "xmr_last_block", "xmr_tx_lookup", "xmr_mempool_stats"]),
        ("④ Zcash ZEC", ["zec_chain_info", "zec_recent_blocks"]),
        ("⑤ 狗狗币 / 莱特币", ["utxo_chain_status"]),
    ]
    desc = {n: d for n, d, _s, _f in TOOLS}
    rows = ""
    for gname, names in groups:
        rows += '<tr class="grp"><td colspan="2">' + gname + '</td></tr>'
        for n in names:
            d = desc.get(n, "")
            short = d.split("【")[0].strip()
            when = ""
            if "【何时用】" in d:
                when = d.split("【何时用】")[1].split("【")[0].strip()
            rows += ('<tr><td><code>' + n + '</code></td><td>' + short +
                     ('<div class="when">何时用：' + when + '</div>' if when else '') + '</td></tr>')
    return DOC_TEMPLATE.replace("__TOOLS_ROWS__", rows)


def server_card():
    desc = {n: d.split("【")[0].strip() for n, d, _s, _f in TOOLS}
    return {
        "name": "robotbase-mcp",
        "title": "RobotBase MCP Server",
        "version": "0.3.0",
        "description": "Read-only multi-chain data for AI agents: BTC / XMR / ZEC / DOGE / LTC. "
                       "First MCP server covering all five classic non-EVM chains: mempool & fee estimates, "
                       "transaction lookup, address summary, shielded-pool supply, node status. No auth required, no tracking.",
        "homepage": "https://robotbase.cc/mcp",
        "transport": {"type": "streamable-http", "url": "https://robotbase.cc/mcp"},
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "auth": {"type": "none", "optional": "X-API-Key header or ?key= raises rate limit to 600/min per IP"},
        "rateLimits": {"anonymous": "120/min per IP", "withApiKey": "600/min per IP"},
        "tags": ["bitcoin", "monero", "zcash", "dogecoin", "litecoin", "blockchain-data", "onchain",
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
        ip = self.client_address[0]
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
                for t2 in d["top_tools"]) or '<tr><td colspan="4" class="muted">暂无调用</td></tr>'
            page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>RobotBase MCP · 用量审计</title><style>
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
<h1>RobotBase MCP · 用量审计</h1>
<div class="sub">统计窗口：最近 <b>{hours}</b> 小时 · 数据来源 <code>/opt/mcp/usage.db</code> · IP 仅保存 SHA256 前 16 位（不可逆）</div>
<div class="cards">
<div class="card"><div class="k">总调用</div><div class="v">{d["total_calls"]}</div></div>
<div class="card"><div class="k">错误</div><div class="v">{d["errors"]}</div></div>
<div class="card"><div class="k">独立客户端</div><div class="v">{d["unique_clients"]}</div></div>
<div class="card"><div class="k">持 Key 调用</div><div class="v">{d["keyed_calls"]}</div></div>
<div class="card"><div class="k">有效 Key 数</div><div class="v">{d["active_keys"]}</div></div>
</div>
<h2>每小时调用量</h2><div class="bars">{bars}</div>
<h2>工具排行</h2>
<table><tr><th>工具</th><th>调用数</th><th>占比</th><th>平均耗时</th></tr>{rows}</table>
<p class="sub" style="margin-top:18px">JSON 版本：<a href="/mcp/stats?hours={hours}&amp;format=json">/mcp/stats?format=json</a> ·
其他窗口：<a href="/mcp/stats?hours=1">1h</a> · <a href="/mcp/stats?hours=6">6h</a> · <a href="/mcp/stats?hours=24">24h</a> · <a href="/mcp/stats?hours=168">7d</a></p>
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
                log_call(name, ok, (time.time() - started) * 1000, self.client_address[0], plan)
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
    ThreadingHTTPServer((os.environ.get("RB_BIND", "127.0.0.1"), PORT), H).serve_forever()
