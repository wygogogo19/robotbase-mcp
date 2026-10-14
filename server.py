#!/usr/bin/env python3
"""RobotBase MCP Server — one read-only surface for an eight-chain roster.

2026-10-13：对外只保留这一个 MCP，不再提供任何 RPC 端点。名册分三层：
live（BTC/ETH/XMR/ZEC 今天可查）、syncing（Base/Solana/Tron 在建）、
planned（LTC/DOGE/KAS/RVN 已定规格）。修剪与保留窗口是节点的属性，
能力矩阵工具（robotbase_capabilities）如实对外说明边界。
"""
import base64, hashlib, html, json, os, re, socket, sqlite3, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("RB_PORT", "8090"))
BIND = os.environ.get("RB_BIND", "127.0.0.1")
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "robotbase-mcp", "version": "0.10.0", "title": "RobotBase MCP — on-chain verification & risk intelligence for Web3 agents"}
BTC_ENV = os.environ.get("RB_BTC_ENV", "")
BTC_DASHBOARD = os.environ.get("RB_BTC_DASHBOARD", "http://127.0.0.1:8600")
ZEC_BASE = os.environ.get("RB_ZEC_BASE", "http://127.0.0.1:8080")
DOGE_BASE = os.environ.get("RB_DOGE_BASE", "http://127.0.0.1:8080")
LTC_BASE = os.environ.get("RB_LTC_BASE", "http://127.0.0.1:8080")
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


# 2026-10-14：名册收敛为七条链，成熟度只用 live / provisioning / planned 三态
# （不再使用 Tier 1/2/3 那种分级）。tier=live 的节点今天就能回答查询；
# provisioning 的链正在接入聚合与清洗逻辑，planned 的链按需储备，两者都绝不冒充可查询。
# LTC/DOGE/KAS/RVN 与 TON 均已从名册下线（用户 2026-10-14 决定砍掉 TON）。
CHAINS = ("btc", "eth", "xmr", "zec")
ROSTER_CHAINS = ("btc", "eth", "xmr", "zec", "sol", "base", "tron")


def _roster():
    return (cached("nodes", 15, lambda: _http_json(HOME_BASE + "/api/nodes")).get("roster") or [])


def t_list_chains():
    """Every chain on the RobotBase roster with its tier and live availability."""
    d = cached("nodes", 15, lambda: _http_json(HOME_BASE + "/api/nodes"))
    nodes = d.get("nodes") or {}
    out = []
    for c in (d.get("roster") or []):
        cid = c.get("id")
        n = nodes.get(cid) or {}
        out.append({"chain": cid, "tier": c.get("tier"),
                    "ok": bool(n.get("ok")), "synced": bool(n.get("synced")),
                    "height": n.get("height"),
                    "state": n.get("state") or c.get("state")})
    if not out:                                  # 网关不可达时退回静态名册
        out = [{"chain": c, "tier": "live" if c in CHAINS else "provisioning"} for c in ROSTER_CHAINS]
    return {"chains": out, "updated_utc": d.get("updated_utc"),
            "note": "tier=live answers queries today; provisioning chains are still being wired into the "
                    "aggregation layer, and planned chains have no node at all. Neither is served yet."}


def t_capabilities():
    """What every node can and cannot answer, including pruning and retention limits."""
    d = cached("nodes", 20, lambda: _http_json(HOME_BASE + "/api/nodes"))
    rows = []
    for c in (d.get("roster") or []):
        rows.append({"chain": c.get("id"), "tier": c.get("tier"), "name": c.get("name"),
                     "client": c.get("client"), "state": c.get("state"),
                     "capability": c.get("spec")})
    return {
        "surface": "single read-only MCP endpoint — RobotBase publishes no RPC endpoint of any kind",
        "endpoint": "https://robotbase.cc/mcp",
        "chains": rows,
        "honesty_note": "Retention is a property of each node. Pruned or window-limited nodes state "
                        "their boundary here; queries outside it are refused rather than approximated.",
        "updated_utc": d.get("updated_utc"),
    }


def t_chain_status(chain):
    chain = (chain or "").lower()
    if chain == "btc":
        d = cached("btc_node", 10, lambda: _http_json(BTC_DASHBOARD + "/api/node"))
        return {k: d.get(k) for k in ("chain", "blocks", "headers", "verification_progress_pct", "initial_block_download",
                                      "connections", "connections_out", "mempool_txs", "mempool_usage_mb",
                                      "size_on_disk_gb", "version", "rpc_ok")}
    if chain == "xmr":
        n = (cached("nodes", 10, lambda: _http_json(HOME_BASE + "/api/nodes")).get("nodes") or {}).get("xmr") or {}
        return {k: n.get(k) for k in ("ok", "synced", "height", "txpool", "difficulty", "db",
                                      "txc", "sync", "state")}
    if chain == "zec":
        return svc_status(chain)
    if chain == "eth":
        n = (cached("nodes", 10, lambda: _http_json(HOME_BASE + "/api/nodes")).get("nodes") or {}).get("eth") or {}
        return {k: n.get(k) for k in ("ok", "synced", "height", "peers", "exec", "cons", "slot",
                                      "progress", "state")}
    if chain in ROSTER_CHAINS:                   # 名册里有，但今天还不提供服务（provisioning / planned）
        spec = next((c for c in _roster() if c.get("id") == chain), None) or {}
        return {"chain": chain, "served": False, "tier": spec.get("tier", "unknown"),
                "state": spec.get("state"), "client": spec.get("client"),
                "capability": spec.get("spec"),
                "reason": "this chain is on the RobotBase roster but is not serving queries yet"}
    raise ValueError("chain must be one of: " + ", ".join(ROSTER_CHAINS))


def t_zec_get_pool_status():
    """ZEC 价值池分布 + 链状态。

    2026-10-14 合并：原 zec_chain_info 与 zec_shielded_pools_metrics 是近重复工具，
    合并为一个「一次调用给出全部 ZEC 状态」的工具，减少 LLM 的选择歧义。
    两个数据源任一不可用时降级返回，绝不编数字。
    """
    out = {"chain": "ZEC", "source": "our Zebra (zebrad) node + our own value-pool factor index"}
    try:
        r = (cached("zec_ci", 15, lambda: _http_json(ZEC_BASE + "/api/chaininfo")).get("result") or {})
        out.update({
            "blocks": r.get("blocks"), "headers": r.get("headers"),
            "estimated_height": r.get("estimatedheight"), "bestblockhash": r.get("bestblockhash"),
            "chain_supply": r.get("chainSupply"), "difficulty": r.get("difficulty"),
            "size_on_disk_gb": round((r.get("size_on_disk") or 0) / 1e9, 2),
            "node_value_pools": {p.get("id"): {"chainValue": p.get("chainValue"),
                                               "monitored": p.get("monitored")}
                                 for p in (r.get("valuePools") or [])},
        })
    except Exception as exc:  # noqa: BLE001
        out["node_error"] = type(exc).__name__

    try:
        d = cached("factors", 60, lambda: _http_json(HOME + "/api/factors"))
        g = ((d.get("groups") or {}).get("zec") or {})
        meta = ((d.get("groups") or {}).get("meta") or {})
        if not g:
            out["factor_error"] = "ZEC factor sampler unavailable"
        else:
            has24 = str(meta.get("has_24h", "")).lower() not in ("", "no", "false")

            def _num(v):
                try:
                    return float(str(v).replace(",", "").split()[0])
                except (ValueError, AttributeError):
                    return None

            supply = _num(g.get("supply"))
            pools = []
            for pid in ("transparent", "sprout", "sapling", "orchard", "ironwood", "lockbox"):
                bal = g.get(pid)
                if not bal or bal == "—":
                    continue
                n = _num(bal)
                pools.append({"pool": pid, "balance": bal,
                              "share_of_supply_pct": round(n / supply * 100, 2) if (n and supply) else None,
                              "delta_24h": g.get(pid + "_delta") if has24 else None,
                              "delta_1h": g.get(pid + "_delta_1h")})
            out.update({"pools": pools, "shielded_total": g.get("shielded"),
                        "shielded_pct": g.get("shielded_pct"),
                        "shielded_delta_24h": g.get("shielded_delta_24h") if has24 else None,
                        "window": "24h" if has24 else "1h"})
    except Exception as exc:  # noqa: BLE001
        out["factor_error"] = type(exc).__name__
    return out


def t_zec_get_recent_blocks(n=5):
    d = cached(f"zec_blocks_{n}", 15, lambda: _http_json(ZEC_BASE + f"/api/blocks?n={int(n)}"))
    return d


def t_btc_get_fee_rates():
    """BTC 费率建议（2026-10-14 合并原 btc_fee_estimates 与 get_recommended_fee_rate）。"""
    estimates, errors = {}, []
    for target in (1, 2, 3, 6, 12, 24):
        try:
            r = btc_rpc("estimatesmartfee", [target]) or {}
            estimates[f"{target}_blocks"] = {"btc_per_kvb": r.get("feerate"), "blocks": r.get("blocks")}
        except Exception as e:  # noqa: BLE001
            estimates[f"{target}_blocks"] = {"error": str(e)[:80]}
            errors.append(target)
    try:
        mi = btc_rpc("getmempoolinfo") or {}
    except Exception:  # noqa: BLE001
        mi = {}

    def to_sat(btc_per_kvb):
        return round(float(btc_per_kvb) * 1e8 / 1000, 2) if btc_per_kvb else None

    pick = lambda t: to_sat((estimates.get(f"{t}_blocks") or {}).get("btc_per_kvb"))
    return {"chain": "BTC", "unit": "sat/vB",
            "estimates": estimates,
            "recommended": {"fast_1_block_sat_vb": pick(1),
                            "medium_3_block_sat_vb": pick(3),
                            "slow_12_block_sat_vb": pick(12)},
            "mempool_min_sat_vb": to_sat(mi.get("mempoolminfee")),
            "mempool_min_btc_kvb": mi.get("mempoolminfee"),
            "source": "our own bitcoind estimatesmartfee + getmempoolinfo",
            "note": "Zcash does not work this way: its fee is a protocol-fixed ZIP-317 conventional "
                    "amount, not a market estimate — see robotbase_capabilities."}


def t_btc_get_mempool_state():
    """BTC 内存池深度与拥堵判定（2026-10-14 合并原 btc_mempool_summary 与 mempool_congestion_status）。"""
    def build():
        mp = btc_rpc("getmempoolinfo") or {}
        usage = mp.get("usage") or 0
        cap = mp.get("maxmempool") or 1
        return {"chain": "BTC",
                "txs": mp.get("size"), "bytes": mp.get("bytes"),
                "usage_mb": round(usage / 1e6, 1),
                "max_mempool_mb": round(cap / 1e6, 1),
                "load_pct": round(100 * usage / cap, 1),
                "min_fee_btc_kvb": mp.get("mempoolminfee"),
                "total_fees_btc": mp.get("total_fee"),
                "verdict": "busy" if usage > 0.5 * cap else "normal",
                "source": "our own bitcoind getmempoolinfo"}
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




USAGE_DB = os.environ.get("RB_USAGE_DB", "usage.db")
BILLING_DB = os.environ.get("RB_BILLING_DB", "billing.db")


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

KAS_BLOCK_DB = os.environ.get("RB_KAS_BLOCK_DB", "")
HOME = HOME_BASE

# Halving schedules (public consensus rules) + the live height comes from our nodes.
HALVING = {
    "btc":  {"next_height": 1050000, "interval": 210000, "reward": "3.125", "next_reward": "1.5625",
             "block_time_s": 600, "unit": "BTC", "note": "4-year epoch, 210,000 blocks"},
    "zec":  {"next_height": 4406400, "interval": 1680000, "reward": "1.5625", "next_reward": "0.78125",
             "block_time_s": 75, "unit": "ZEC", "note": "post-Blossom 1,680,000-block interval"},
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
    # Kaspa / Ravencoin retired from the roster on 2026-10-13 — nothing appended here.
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
    try:                                                        # XMR: our own monerod
        d = cached("xmr_info", 10, lambda: _http_json(
            XMR_HELPER, {"jsonrpc": "2.0", "id": 1, "method": "get_info"}, timeout=10))
        r = (d or {}).get("result") or {}
        diff = r.get("difficulty")
        out["XMR"] = {"difficulty": diff,
                      "network_hashrate_hs": (float(diff) / 120) if diff else None,
                      "height": r.get("height"),
                      "hashrate_method": "difficulty / 120s target block time",
                      "source": "our monerod get_info"}
    except Exception as exc:  # noqa: BLE001
        out["XMR"] = {"error": type(exc).__name__}
    for cid, key, base, factor, block_s in (("ZEC", "zec", ZEC_BASE, 8192, 75),):
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
            "note": "BTC/XMR are node-reported (bitcoind / monerod); ZEC hashrate is derived from the node's "
                    "difficulty with the factor printed next to it. "
                    "No third-party API is involved."}


# 2026-10-14 P0 工具治理：
#   get_recommended_fee_rate  → 并入 t_btc_get_fee_rates
#   mempool_congestion_status → 并入 t_btc_get_mempool_state
#   zec_shielded_pools_metrics→ 并入 t_zec_get_pool_status
# 三个函数体已删除，避免同一份数据出现两个入口让 LLM 选错。
ZIP317_NOTE = ("Zcash fees are protocol-fixed: the ZIP-317 conventional fee ({:.8f} ZEC per logical "
               "action) is a consensus rule, not a market auction. There is no ZEC fee estimator tool."
               ).format(ZIP317_FEE)


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



# ============== 2026-10-14 P1：风险情报工具（计量计费，轻量聚合）==================
# 用户指令：P1 主打「高价值风控工具 + 轻量聚合」，不做通用 RPC 转发。
# 数据源一律是公开聚合接口（GoPlus Labs + TronGrid），只读、不接触私钥、不托管资金。
# 计费：每个 IP 每天 5 次免费；带 X-API-Key 的用户不受此限（Key 由网关的 billing 服务发放）。
RISK_TOOLS = ("tron_check_address_risk", "tron_verify_usdt_transfer", "sol_check_token_safety",
              "base_check_contract_risk")
RISK_FREE_PER_DAY = 5
RISK_DB = os.environ.get("RB_RISK_DB", "risk_quota.db")
GOPLUS_BASE = os.environ.get("RB_GOPLUS_BASE", "https://api.gopluslabs.io/api/v1")
TRONGRID_BASE = os.environ.get("RB_TRONGRID_BASE", "https://api.trongrid.io")
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_DECIMALS = 6
TRON_FINALITY_BLOCKS = 19          # 波场 SR 共识的不可逆深度（约 57 秒）
TOPUP_URL = "https://robotbase.cc/billing/pricing"

_REQ = threading.local()


def _client_ip():
    return getattr(_REQ, "ip", "") or "unknown"


def _client_plan():
    return getattr(_REQ, "plan", "anon")


class PaymentRequired(Exception):
    """免费额度用尽——这不是「错误」，是要求充值。"""


def _bj_day():
    """配额按北京时间自然日切分，服务器时区不参与。"""
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))


def init_risk_db():
    con = _db(RISK_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS risk_quota(
        day TEXT NOT NULL, ip_hash TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(day, ip_hash))""")
    # P2: 按 Key 计数（/billing/v1/usage 的用量看板数据源），只存 key 的 SHA256，不存明文。
    con.execute("""CREATE TABLE IF NOT EXISTS key_usage(
        day TEXT NOT NULL, key_hash TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(day, key_hash))""")
    con.commit()
    con.close()


def _key_hash():
    k = getattr(_REQ, "key", "") or ""
    return hashlib.sha256(k.encode()).hexdigest() if k else ""


def _count_key_call():
    """带 Key 的调用计一次数，返回今天该 Key 的累计次数。"""
    kh = _key_hash()
    if not kh:
        return None
    d = _bj_day()
    con = _db(RISK_DB)
    try:
        con.execute("INSERT INTO key_usage(day,key_hash,used) VALUES(?,?,1) "
                    "ON CONFLICT(day,key_hash) DO UPDATE SET used=used+1", (d, kh))
        con.commit()
        row = con.execute("SELECT used FROM key_usage WHERE day=? AND key_hash=?", (d, kh)).fetchone()
        return row["used"] if row else None
    finally:
        con.close()


def _key_meter():
    used = _count_key_call()
    return {"api_key": {"recognized": True, "unlimited": True,
                        "calls_today": used,
                        "resets": "00:00 Asia/Shanghai",
                        "usage_endpoint": "https://robotbase.cc/billing/v1/usage (pass your key)"}}


def _risk_quota(consume=True):
    """返回 (allowed, used, limit)。API-key 用户 limit=-1 表示不限量。"""
    if _client_plan() == "key":
        return True, 0, -1
    h = hashlib.sha256(_client_ip().encode()).hexdigest()[:16]
    d = _bj_day()
    con = _db(RISK_DB)
    try:
        row = con.execute("SELECT used FROM risk_quota WHERE day=? AND ip_hash=?", (d, h)).fetchone()
        used = row["used"] if row else 0
        if not consume:
            return used < RISK_FREE_PER_DAY, used, RISK_FREE_PER_DAY
        if used >= RISK_FREE_PER_DAY:
            return False, used, RISK_FREE_PER_DAY
        con.execute("INSERT INTO risk_quota(day, ip_hash, used) VALUES(?,?,1) "
                    "ON CONFLICT(day, ip_hash) DO UPDATE SET used=used+1", (d, h))
        con.commit()
        return True, used + 1, RISK_FREE_PER_DAY
    finally:
        con.close()


def _risk_gate():
    """风险工具的统一门禁（只查不扣）：额度用尽就抛 PaymentRequired。

    真正的扣减放在 _risk_commit()，只有确实向上游查过、返回了结论的调用才计费；
    地址格式错误这类本地校验失败不占用户额度。
    """
    allowed, used, limit = _risk_quota(False)
    if not allowed:
        raise PaymentRequired(
            "Payment Required: Upgrade or Top-up with USDT to get an API Key. "
            "The free allowance for RobotBase risk tools is %d calls/day per IP and is used up for "
            "today (Beijing time, resets 00:00 Asia/Shanghai). Top up at %s and send the key back as "
            "the X-API-Key header on this same endpoint." % (limit, TOPUP_URL))
    return _meter_payload(used, limit, pending=True)


def _meter_payload(used, limit, pending=False):
    if limit < 0:
        return {"free_tier": {"unlimited_with_api_key": True}}
    payload = {"free_tier": {"used_today": used, "limit_per_day": limit,
                             "resets": "00:00 Asia/Shanghai", "unlimited_with_api_key": True}}
    if pending:
        payload["free_tier"]["remaining_today"] = max(0, limit - used)
    return payload


def _risk_commit(used_before, limit):
    """结算一次真实查询：扣掉今天的 1 次免费额度并返回最终计量信息。"""
    if limit < 0:
        return _meter_payload(0, -1)
    allowed, used, _l = _risk_quota(True)
    return _meter_payload(used if allowed else used_before + 1, limit)


_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(raw):
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58_ALPHABET[r] + out
    for byte in raw:                       # 保留前导零字节
        if byte == 0:
            out = "1" + out
        else:
            break
    return out


def _tron_hex_to_address(h):
    """TronGrid 返回的 0x/41 前缀 hex 转 base58check 地址。"""
    h = h[2:] if h.startswith("0x") else h
    raw = bytes.fromhex(h)
    if len(raw) == 20:
        raw = b"\x41" + raw
    return _b58encode(raw + hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4])


def _goplus_tron(address):
    d = _http_json("%s/address_security/%s?chain_id=tron" % (GOPLUS_BASE, address), timeout=12)
    return d.get("result") or {}


_TRON_HIGH_FLAGS = ("sanctioned", "money_laundering", "mixer", "cybercrime", "darkweb_transactions",
                    "phishing_activities", "stealing_attack", "blacklist_doubt", "financial_crime",
                    "honeypot_related_address")
_TRON_MED_FLAGS = ("gas_abuse", "fake_kyc", "fake_token", "malicious_mining_activities",
                   "fake_standard_interface", "reinit", "blackmail_activities")


def t_tron_check_address_risk(address):
    """TRON 地址风控：司法/黑灰产标签 + 账户画像，给出安全评分。"""
    addr = (address or "").strip()
    if not re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", addr):
        return {"address": addr, "chain": "TRON", "error": "not a TRON base58 address (expected T + 33 chars)",
                "checked": False, "_no_charge": True}
    out = {"address": addr, "chain": "TRON", "asset_scope": "TRX / TRC-20",
           "sources": ["GoPlus Labs address_security (tron)", "TronGrid /v1/accounts"]}
    account, flags = {}, {}
    try:
        d = _http_json("%s/v1/accounts/%s" % (TRONGRID_BASE, addr), timeout=12)
        rows = d.get("data") or []
        if rows:
            a = rows[0]
            account = {"activated": True, "is_contract": bool(a.get("code")),
                       "created_at_ms": a.get("create_time"),
                       "balance_trx": round((a.get("balance") or 0) / 1e6, 6),
                       "trc20_holdings": len(a.get("trc20") or [])}
            if a.get("create_time"):
                age_days = max(0, int((time.time() * 1000 - a["create_time"]) / 86400000))
                account["age_days"] = age_days
        else:
            account = {"activated": False}
    except Exception as exc:  # noqa: BLE001
        out["tron_account_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:100])
    try:
        flags = _goplus_tron(addr)
    except Exception as exc:  # noqa: BLE001
        out["goplus_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:100])

    high = [f for f in _TRON_HIGH_FLAGS if str(flags.get(f, "0")) == "1"]
    med = [f for f in _TRON_MED_FLAGS if str(flags.get(f, "0")) == "1"]
    bad_contracts = int(flags.get("number_of_malicious_contracts_created") or 0)

    if not flags and not account:
        out.update({"checked": False, "risk_level": "unknown",
                    "reason": "both upstream sources were unavailable; no score is invented",
                    "_no_charge": True})
        return out
    if account.get("activated") is False:
        out.update({"checked": True, "account": account, "flags_raw": flags or None,
                    "risk_level": "unknown", "safety_score": None,
                    "hit_flags": high + med,
                    "note": "The address is not activated on TRON, so there is no transaction history "
                            "to judge. Not activated is not the same as clean."})
        out["disclaimer"] = ("Labels come from the public GoPlus Labs dataset as fetched at call time; "
                             "RobotBase relays them verbatim and forms no legal opinion.")
        return out

    score = 100
    score -= 50 * len(high)
    score -= 12 * len(med)
    if bad_contracts:
        score -= 20
    if account.get("age_days") is not None and account["age_days"] < 7:
        score -= 8
    if account.get("is_contract"):
        score -= 5
    score = max(0, min(100, score))
    level = "high" if high else ("medium" if (med or bad_contracts) else "low")
    out.update({"checked": True, "risk_level": level, "safety_score": score,
                "hit_flags": high + med,
                "malicious_contracts_created": bad_contracts,
                "account": account,
                "flags_raw": {k: v for k, v in flags.items() if str(v) not in ("0", "", "None")},
                "scoring": "100 minus 50 per judicial/black-market hit, 12 per medium flag, "
                           "20 for created malicious contracts, 8 if the account is under 7 days old, "
                           "5 if it is a contract address",
                "not_covered": ["TRON 'judicial freeze' status is not in this dataset - TronScan's "
                                "security endpoint now requires an API key and is not queried here",
                                "counterparty graph depth (who paid whom) is not scored in this call"],
                "disclaimer": "Labels come from the public GoPlus Labs dataset as fetched at call time; "
                              "RobotBase relays them verbatim, forms no legal opinion, and this is not "
                              "compliance advice."})
    return out


def t_tron_verify_usdt_transfer(txid, expected_to=None, expected_amount_usdt=None):
    """TRC-20 USDT 到账核验：终局性、金额、收付款方，一次给全。"""
    tx = (txid or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", tx):
        return {"txid": txid, "chain": "TRON", "verified": False,
                "error": "not a TRON transaction id (expected 64 hex chars)", "_no_charge": True}
    out = {"txid": tx, "chain": "TRON", "network": "mainnet",
           "source": "TronGrid full node + solidity (finalized) node"}
    try:
        raw = _http_json("%s/wallet/gettransactionbyid" % TRONGRID_BASE, {"value": tx}, timeout=15)
    except Exception as exc:  # noqa: BLE001
        return {"txid": tx, "chain": "TRON", "verified": False,
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
    if not raw or not raw.get("txID"):
        return {"txid": tx, "chain": "TRON", "found": False, "verified": False,
                "reason": "the full node does not know this transaction id"}

    contract = ((raw.get("raw_data") or {}).get("contract") or [{}])[0]
    ctype = contract.get("type")
    value = (contract.get("parameter") or {}).get("value") or {}
    ret = (raw.get("ret") or [{}])[0]
    status = ret.get("contractRet") or "UNKNOWN"
    out.update({"found": True, "contract_type": ctype, "status": status})

    amount_raw, to_addr, from_addr, is_usdt = None, None, None, False
    try:
        from_addr = _tron_hex_to_address(value.get("owner_address", ""))
    except Exception:  # noqa: BLE001
        pass
    if ctype == "TriggerSmartContract":
        token = value.get("contract_address") or ""
        data = value.get("data") or ""
        try:
            is_usdt = _tron_hex_to_address(token) == USDT_TRC20_CONTRACT
        except Exception:  # noqa: BLE001
            is_usdt = False
        method = data[:8]
        if method == "a9059cbb" and len(data) >= 136:      # transfer(address,uint256)
            try:
                to_addr = _tron_hex_to_address(data[32:72])
                amount_raw = int(data[72:136], 16)
            except Exception:  # noqa: BLE001
                pass
        elif method == "23b872dd" and len(data) >= 200:     # transferFrom(address,address,uint256)
            try:
                to_addr = _tron_hex_to_address(data[96:136])
                amount_raw = int(data[136:200], 16)
            except Exception:  # noqa: BLE001
                pass
        out["token_contract"] = USDT_TRC20_CONTRACT if is_usdt else (token and _safe_b58(token))
        out["method"] = method
    elif ctype == "TransferContract":
        out["note"] = "this is a native TRX transfer, not a TRC-20 USDT transfer"
        try:
            to_addr = _tron_hex_to_address(value.get("to_address", ""))
            amount_raw = value.get("amount")
        except Exception:  # noqa: BLE001
            pass

    info = {}
    try:
        info = _http_json("%s/wallet/gettransactioninfobyid" % TRONGRID_BASE, {"value": tx}, timeout=15) or {}
    except Exception:  # noqa: BLE001
        pass
    block = info.get("blockNumber")
    if block:
        out["block_number"] = block
        out["block_time_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((info.get("blockTimeStamp") or 0) / 1000))
    out["fee_trx"] = round((info.get("fee") or 0) / 1e6, 6) if info.get("fee") else None
    receipt = (info.get("receipt") or {}).get("result")
    if receipt:
        out["receipt_result"] = receipt

    finalized = False
    try:
        solidified = _http_json("%s/walletsolidity/gettransactionbyid" % TRONGRID_BASE, {"value": tx}, timeout=15)
        finalized = bool(solidified and solidified.get("txID"))
    except Exception:  # noqa: BLE001
        finalized = False
    out["finalized"] = finalized
    if not finalized and block:
        try:
            nb = _http_json("%s/wallet/getnowblock" % TRONGRID_BASE, {}, timeout=12) or {}
            height = ((nb.get("block_header") or {}).get("raw_data") or {}).get("number")
            if height:
                conf = int(height) - int(block) + 1
                out["confirmations"] = conf
                out["blocks_until_finality"] = max(0, TRON_FINALITY_BLOCKS - conf)
        except Exception:  # noqa: BLE001
            pass

    if amount_raw is not None and is_usdt:
        out["amount_usdt"] = round(amount_raw / (10 ** USDT_DECIMALS), 6)
    if from_addr:
        out["from"] = from_addr
    if to_addr:
        out["to"] = to_addr

    checks = {"status_success": status == "SUCCESS", "is_usdt_trc20": is_usdt, "finalized": finalized}
    if expected_to:
        checks["recipient_matches"] = (expected_to.strip() == (to_addr or ""))
    if expected_amount_usdt is not None:
        try:
            checks["amount_matches"] = abs(float(expected_amount_usdt) - (out.get("amount_usdt") or 0)) < 1e-6
        except (TypeError, ValueError):
            checks["amount_matches"] = False
    out["checks"] = checks
    out["verified"] = all(checks.values())
    out["verdict"] = ("USDT arrived and is irreversible" if out["verified"] and finalized else
                      "confirmed but not yet finalized (wait for 19 blocks)" if out["verified"] else
                      "not verified - see checks")
    return out


def _safe_b58(h):
    try:
        return _tron_hex_to_address(h)
    except Exception:  # noqa: BLE001
        return h


def t_sol_check_token_safety(mint):
    """Solana 代币防割检测：铸造权/冻结权、LP 锁定、筹码集中度。"""
    m = (mint or "").strip()
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", m):
        return {"mint": m, "chain": "SOL", "checked": False,
                "error": "not a Solana mint address (base58, 32-44 chars)", "_no_charge": True}
    out = {"mint": m, "chain": "SOL", "sources": ["GoPlus Labs solana/token_security"]}
    try:
        d = _http_json("%s/solana/token_security?contract_addresses=%s" % (GOPLUS_BASE, m), timeout=15)
        res = (d.get("result") or {}).get(m)
    except Exception as exc:  # noqa: BLE001
        return {"mint": m, "chain": "SOL", "checked": False,
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:120]), "_no_charge": True}
    if not res:
        return {"mint": m, "chain": "SOL", "checked": False,
                "reason": "no token-security record for this mint (unknown token, not a token mint, "
                          "or the upstream dataset has not seen it yet)"}

    def status(field):
        return str((res.get(field) or {}).get("status", "0"))

    mintable, freezable = status("mintable"), status("freezable")
    holders = res.get("holders") or []
    try:
        top10 = round(sum(float(h.get("percent") or 0) for h in holders[:10]), 2)
    except (TypeError, ValueError):
        top10 = None
    lp = res.get("lp_holders") or []
    try:
        lp_total = round(sum(float(x.get("percent") or 0) for x in lp), 2)
        lp_locked = round(sum(float(x.get("percent") or 0) for x in lp if x.get("is_locked")), 2)
    except (TypeError, ValueError):
        lp_total, lp_locked = None, None

    warnings = []
    if mintable != "0":
        warnings.append("mint authority is still live - the supply can be inflated at will")
    if freezable != "0":
        warnings.append("freeze authority is still live - your tokens can be frozen")
    if status("closable") != "0":
        warnings.append("token account is closable")
    if str(res.get("non_transferable") or "0") == "1":
        warnings.append("token is non-transferable")
    if res.get("transfer_fee"):
        warnings.append("a transfer fee is configured")
    if res.get("transfer_hook"):
        warnings.append("a transfer hook is installed")
    if top10 is not None and top10 >= 50:
        warnings.append("top-10 holders control %.1f%% of supply" % top10)
    if lp_total is not None and lp_total > 0 and (lp_locked or 0) <= 0:
        warnings.append("liquidity is not locked")

    # 受监管的发行方代币（USDC/USDT 这类）本来就长期持有增发权与冻结权，
    # 这是合规特征而不是拉盘信号；不区分就会把主流稳定币误判成高危。
    trusted = str(res.get("trusted_token") or "0") == "1"
    try:
        holders_n = int(str(res.get("holder_count") or "0"))
    except ValueError:
        holders_n = 0
    issuer_backed = bool(trusted and holders_n >= 100000 and (top10 or 0) < 20)

    score = 100
    authority_penalty = (35 if mintable != "0" else 0) + (25 if freezable != "0" else 0)
    if issuer_backed:
        authority_penalty = min(authority_penalty, 15)
    score -= authority_penalty
    if top10 is not None:
        score -= min(25, int(max(0, top10 - 30) / 2))
    if lp_total and not lp_locked:
        score -= 15
    if res.get("transfer_fee") or res.get("transfer_hook"):
        score -= 10
    score = max(0, min(100, score))
    hard = ("not locked", "non-transferable", "closable", "transfer hook")
    if issuer_backed:
        level = "medium" if any(any(k in w for k in hard) for w in warnings) else "low"
    else:
        level = ("high" if any(("mint authority" in w or "freeze authority" in w or "not locked" in w)
                               for w in warnings)
                 else "medium" if warnings else "low")
    out.update({"checked": True, "risk_level": level, "safety_score": score,
                "name": (res.get("metadata") or {}).get("name"),
                "symbol": (res.get("metadata") or {}).get("symbol"),
                "mint_authority_revoked": mintable == "0",
                "freeze_authority_revoked": freezable == "0",
                "holder_count": res.get("holder_count"),
                "top10_holder_pct": top10,
                "lp_holder_pct": lp_total,
                "lp_locked_pct": lp_locked,
                "trusted_by_goplus": trusted,
                "issuer_backed": issuer_backed,
                "authority_note": ("this mint is on GoPlus's trusted list with a wide holder base, so live "
                                   "mint/freeze authority is read as issuer control (how regulated "
                                   "stablecoins work) rather than as a rug signal"
                                   if issuer_backed else None),
                "creators": [c.get("address") for c in (res.get("creators") or [])][:3],
                "warnings": warnings or ["no red flag in the checked dimensions"],
                "scoring": "100 minus 35 if mint authority is live, 25 if freeze authority is live "
                           "(capped at 15 combined for issuer-backed tokens), "
                           "up to 25 for top-10 concentration, 15 if liquidity is unlocked, "
                           "10 for transfer fee or hook",
                "not_covered": ["holder wallet history and wash-trading patterns",
                                "creator's other deployments",
                                "on-chain liquidity depth in USD (no DEX aggregator is queried here)"],
                "disclaimer": "Judgement is derived from the public GoPlus Labs dataset as fetched at "
                              "call time. It is a risk signal, not investment advice."})
    return out




# ---- P2 (2026-10-14): Base 合约与代币风控（EVM，GoPlus chain_id 8453，纯聚合）---
BASE_CHAIN_ID = 8453
BASESCAN = "https://basescan.org"

_BASE_HARD_FLAGS = ("is_honeypot", "cannot_sell_all", "owner_change_balance", "hidden_owner",
                    "can_take_back_ownership", "is_blacklisted", "selfdestruct")
_BASE_WARN_FLAGS = ("is_proxy", "transfer_pausable", "trading_cooldown", "slippage_modifiable",
                    "personal_slippage_modifiable", "external_call", "is_anti_whale", "is_mintable")
# 受监管发行方代币（USDC/USDT 这类）本来就有代理升级、暂停、增发权，这些是合规控制而不是
# 拉盘后门；不区分就会把主流稳定币误判成中危。命中这些关键词的告警在 issuer_backed 时
# 挪到 issuer_control[]，只说明事实，不参与扣分。
_BASE_ISSUER_SOFT = ("upgradeable", "mintable", "can be paused", "slippage/tax can be changed",
                     "anti-whale limits", "trading cooldown")


def _goplus_evm_token(chain_id, contract):
    d = _http_json("%s/token_security/%s?contract_addresses=%s" % (GOPLUS_BASE, chain_id, contract),
                   timeout=15)
    res = d.get("result") or {}
    return res.get(contract) or res.get(contract.lower()) or {}


def _pct(v):
    """GoPlus 的税率是小数（0.05 = 5%）。"""
    try:
        return round(float(v) * 100, 3)
    except (TypeError, ValueError):
        return None


def _flag(res, key):
    return str(res.get(key) or "0") == "1"


def t_base_check_contract_risk(contract):
    """Base 合约/代币风控：貔貅、税率、开源、后门与 LP 锁定，一次给全。"""
    c = (contract or "").strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", c):
        return {"contract": c, "chain": "BASE", "checked": False,
                "error": "not an EVM contract address (0x + 40 hex chars)", "_no_charge": True}
    c = c.lower()
    out = {"contract": c, "chain": "BASE", "chain_id": BASE_CHAIN_ID, "asset_scope": "ERC-20 on Base",
           "sources": ["GoPlus Labs token_security (EVM chain 8453)", BASESCAN + "/address/" + c]}
    try:
        res = _goplus_evm_token(BASE_CHAIN_ID, c)
    except Exception as exc:  # noqa: BLE001
        return {"contract": c, "chain": "BASE", "checked": False,
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:120]), "_no_charge": True}
    if not res:
        return {"contract": c, "chain": "BASE", "checked": False,
                "reason": "no token-security record: the contract is not indexed by GoPlus on Base, "
                          "is not an ERC-20, or was deployed in the last minutes",
                "how_to_check": BASESCAN + "/address/" + c,
                "note": "no record is not the same as safe"}

    honeypot = _flag(res, "is_honeypot") or _flag(res, "cannot_sell_all")
    buy_tax = _pct(res.get("buy_tax"))
    sell_tax = _pct(res.get("sell_tax"))
    holders = res.get("holders") or []
    try:
        top10 = round(sum(float(h.get("percent") or 0) for h in holders[:10]) * 100, 2)
    except (TypeError, ValueError):
        top10 = None
    lp = res.get("lp_holders") or []
    try:
        lp_total = round(sum(float(x.get("percent") or 0) for x in lp) * 100, 2)
        lp_locked = round(sum(float(x.get("percent") or 0) for x in lp if x.get("is_locked")) * 100, 2)
    except (TypeError, ValueError):
        lp_total, lp_locked = None, None

    try:
        holders_n = int(str(res.get("holder_count") or "0"))
    except ValueError:
        holders_n = 0
    # trust_list 是 GoPlus 自己维护的白名单；稳定币的 top-10 里坐的是交易所与财库地址，
    # 集中度高不等于控盘，所以这里不再拿集中度当门槛，而是把发行方控制类告警单独列出。
    issuer_backed = bool(_flag(res, "trust_list") and holders_n >= 100000)

    warnings, hard = [], []
    if _flag(res, "is_honeypot"):
        hard.append("GoPlus flags this token as a honeypot")
    if _flag(res, "cannot_sell_all"):
        hard.append("holders cannot sell the full balance")
    if _flag(res, "owner_change_balance"):
        hard.append("the owner can rewrite holder balances")
    if _flag(res, "hidden_owner"):
        hard.append("a hidden owner exists")
    if _flag(res, "can_take_back_ownership"):
        hard.append("ownership can be taken back after renouncing")
    if _flag(res, "selfdestruct"):
        hard.append("the contract can selfdestruct")
    if _flag(res, "is_blacklisted"):
        hard.append("the token is blacklisted by GoPlus")
    if sell_tax is not None and sell_tax >= 10:
        warnings.append("sell tax is %.2f%% - a large part of the exit is confiscated" % sell_tax)
    elif sell_tax:
        warnings.append("sell tax is %.2f%%" % sell_tax)
    if buy_tax:
        warnings.append("buy tax is %.2f%%" % buy_tax)
    if not _flag(res, "is_open_source") and res.get("is_open_source") is not None:
        warnings.append("contract source is NOT verified/open")
    if _flag(res, "is_proxy"):
        warnings.append("contract is upgradeable (proxy) - the logic can be replaced")
    if _flag(res, "transfer_pausable"):
        warnings.append("transfers can be paused")
    if _flag(res, "is_mintable"):
        warnings.append("supply is mintable")
    if _flag(res, "trading_cooldown"):
        warnings.append("a trading cooldown is enforced")
    if _flag(res, "slippage_modifiable") or _flag(res, "personal_slippage_modifiable"):
        warnings.append("slippage/tax can be changed by the owner")
    if _flag(res, "external_call"):
        warnings.append("contract makes external calls (a rug vector)")
    if _flag(res, "is_anti_whale"):
        warnings.append("anti-whale limits are active")
    if top10 is not None and top10 >= 50:
        warnings.append("top-10 holders control %.1f%% of supply" % top10)
    if lp_total is not None and lp_total > 0 and (lp_locked or 0) <= 0:
        warnings.append("liquidity is not locked")
    if _flag(res, "honeypot_with_same_creator"):
        warnings.append("the same creator has shipped honeypots before")
    if _flag(res, "fake_token") or (res.get("is_true_token") is not None and not _flag(res, "is_true_token")):
        warnings.append("GoPlus does not treat this as the genuine token")
    if _flag(res, "is_airdrop_scam"):
        warnings.append("flagged as an airdrop scam")

    score = 100
    score -= 45 * len([k for k in _BASE_HARD_FLAGS if _flag(res, k)])
    if honeypot:
        score = min(score, 15)
    tax_penalty = 0
    if sell_tax is not None and sell_tax >= 10:
        tax_penalty += 25
    elif sell_tax is not None and sell_tax >= 5:
        tax_penalty += 10
    if buy_tax is not None and buy_tax >= 10:
        tax_penalty += 15
    score -= tax_penalty
    if res.get("is_open_source") is not None and not _flag(res, "is_open_source"):
        score -= 15
    issuer_control = []
    if issuer_backed:
        issuer_control = [w for w in warnings if any(k in w for k in _BASE_ISSUER_SOFT)]
        warnings = [w for w in warnings if w not in issuer_control]
    else:
        for k in _BASE_WARN_FLAGS:
            if _flag(res, k):
                score -= 8
    if top10 is not None:
        score -= min(25, int(max(0, top10 - 30) / 2))
    if lp_total and not lp_locked:
        score -= 15
    score = max(0, min(100, score))
    if hard or honeypot or (sell_tax or 0) >= 10:
        level = "high"
    elif warnings:
        level = "medium"
    else:
        level = "low"
    if issuer_backed and level == "high" and not honeypot:
        level = "medium"

    out.update({
        "checked": True, "risk_level": level, "safety_score": score,
        "token_name": res.get("token_name"), "token_symbol": res.get("token_symbol"),
        "is_honeypot": _flag(res, "is_honeypot"),
        "cannot_sell_all": _flag(res, "cannot_sell_all"),
        "buy_tax_pct": buy_tax, "sell_tax_pct": sell_tax,
        "is_open_source": None if res.get("is_open_source") is None else _flag(res, "is_open_source"),
        "is_proxy": _flag(res, "is_proxy"), "is_mintable": _flag(res, "is_mintable"),
        "owner_change_balance": _flag(res, "owner_change_balance"),
        "hidden_owner": _flag(res, "hidden_owner"),
        "can_take_back_ownership": _flag(res, "can_take_back_ownership"),
        "transfer_pausable": _flag(res, "transfer_pausable"),
        "selfdestruct": _flag(res, "selfdestruct"),
        "is_blacklisted": _flag(res, "is_blacklisted"),
        "honeypot_with_same_creator": _flag(res, "honeypot_with_same_creator"),
        "holder_count": res.get("holder_count"),
        "top10_holder_pct": top10,
        "lp_holder_pct": lp_total, "lp_locked_pct": lp_locked,
        "trust_list": _flag(res, "trust_list"), "issuer_backed": issuer_backed,
        "issuer_control": issuer_control or None,
        "issuer_note": ("this token is on GoPlus's trust list with a wide holder base, so upgradeable "
                        "proxy / pausable / mintable control is read as issuer compliance control "
                        "(how regulated stablecoins work) rather than as a rug signal"
                        if issuer_backed else None),
        "owner_address": res.get("owner_address"),
        "dex": [{"name": d.get("name"), "liquidity": d.get("liquidity"),
                 "pair": d.get("pair")} for d in (res.get("dex") or [])[:5]],
        "creators": [x.get("address") for x in (res.get("creators") or [])][:3],
        "warnings": (hard + warnings) or ["no red flag in the checked dimensions"],
        "scoring": "100 minus 45 per hard backdoor flag (honeypot caps the score at 15), 25 if the sell "
                   "tax is >=10% (15 if the buy tax is), 15 if the source is not verified, 8 per "
                   "upgrade/pause/mint/slippage flag, up to 25 for top-10 concentration, 15 if "
                   "liquidity is unlocked",
        "not_covered": [
            "a live sell simulation with a real quote (no swap simulator is queried here)",
            "whether the owner key is compromised, or off-chain rug intent",
            "liquidity depth in USD (no DEX aggregator is queried)",
            "contracts and tokens that GoPlus has not indexed on Base yet"],
        "disclaimer": "Derived from the public GoPlus Labs dataset as fetched at call time. A risk screen, "
                      "not investment advice; no record does not mean safe.",
    })
    return out


TOOLS = [    ("list_chains",
     "List every chain on the RobotBase roster with its state. "
     "When to use: the user asks which chains are supported, which nodes are online, or where to start. "
     "Do not use: for one chain's live health call chain_status; for what a node can and cannot answer call robotbase_capabilities. "
     "Returns: chains[], each {chain, tier (live|provisioning|planned), ok, synced, height, state}.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_list_chains()),    ("chain_status",
     "Live run-time state of one chain's node. "
     "When to use: is this chain's node synced, healthy or lagging; what is its height, peer count, mempool depth or client version. "
     "Do not use: fees (that is btc_get_fee_rates); a specific payment (that is btc_get_transaction); ZEC pool distribution (that is zec_get_pool_status). "
     "Returns: chain, ok, synced, height, peers, mempool, client, state; a chain that is not live yet returns served=false plus its tier instead of an error.",
     {"type": "object", "properties": {"chain": {"type": "string",
                                                 "enum": ["btc", "eth", "xmr", "zec", "base", "sol", "tron"],
                                                 "description": "Chain id: btc / eth / xmr / zec / base / sol / tron"}},
      "required": ["chain"], "additionalProperties": False}, lambda a: t_chain_status(a.get("chain"))),    ("robotbase_capabilities",
     "Per-chain capability, retention boundary and fee model. "
     "When to use: before promising that some query is possible, or when a tool refused a query as out of range. "
     "Do not use: for live numbers (that is chain_status). "
     "Returns: one row per chain {chain, tier, name, client, state, capability}, plus the fee models we support and an honesty note. "
     "Key fact: RobotBase publishes no RPC endpoint - every read path is this MCP surface.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_capabilities()),    ("zec_get_pool_status",
     "Zcash chain state plus the distribution of all six value pools, in one call. "
     "When to use: how much ZEC sits in each shielded pool, the shielded share of supply, and whether the ZEC node is caught up. "
     "Do not use: we do not index Zcash transactions or wallets; for node peers and disk usage call chain_status. "
     "Returns: blocks/headers/estimated_height/chain_supply/difficulty/size_on_disk_gb, node_value_pools, "
     "pools[] with {pool, balance, share_of_supply_pct, delta_1h, delta_24h}, shielded_total, shielded_pct.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_zec_get_pool_status()),    ("zec_get_recent_blocks",
     "The most recent Zcash blocks. "
     "When to use: checking whether ZEC is producing blocks normally and at what interval. "
     "Do not use: for shielded-pool supply (that is zec_get_pool_status). "
     "Returns: for the last N blocks (default 5, max 20): height, hash, time, difficulty.",
     {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5,
                                             "description": "How many recent blocks to return; default 5"}},
      "additionalProperties": False}, lambda a: t_zec_get_recent_blocks(a.get("n", 5))),    ("btc_get_fee_rates",
     "Bitcoin fee rates for confirmation targets, plus the mempool floor. "
     "When to use: pricing a Bitcoin transaction, or deciding when a sweep will confirm cheaply. "
     "Do not use: to measure backlog depth (that is btc_get_mempool_state). There is no equivalent for Zcash: ZEC fees are protocol-fixed, see robotbase_capabilities. "
     "Returns: estimates for 1/2/3/6/12/24-block targets in BTC/kvB, recommended.fast_1_block_sat_vb / medium_3_block_sat_vb / slow_12_block_sat_vb, mempool_min_sat_vb.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_btc_get_fee_rates()),    ("btc_get_mempool_state",
     "Bitcoin mempool depth, capacity load and a busy/normal verdict. "
     "When to use: deciding whether now is a good moment to settle on-chain, or how large the backlog is. "
     "Do not use: to choose a fee (that is btc_get_fee_rates). "
     "Returns: txs, bytes, usage_mb, max_mempool_mb, load_pct, min_fee_btc_kvb, total_fees_btc, verdict.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_btc_get_mempool_state()),    ("btc_get_transaction",
     "Look up one Bitcoin transaction by txid - the core settlement check. "
     "When to use: has this payment arrived, which block is it in, how many confirmations does it have. "
     "Do not use: for a whole block (that is btc_get_block); for an address balance (that is btc_get_address). "
     "Returns: txid, blockhash, confirmations, time, size/vsize, vin_count, up to 12 outputs with address and value, value_out_btc.",
     {"type": "object", "properties": {"txid": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$",
                                                "description": "Bitcoin transaction hash (64 hex chars)"}},
      "required": ["txid"], "additionalProperties": False}, lambda a: t_btc_tx_lookup(a.get("txid"))),    ("btc_get_block",
     "Bitcoin block summary by height or hash; omit both for the chain tip. "
     "When to use: an overview of a block, or confirming which block a payment landed in. "
     "Do not use: to fetch a single payment (that is btc_get_transaction). "
     "Returns: hash, height, time, txs, size, weight, confirmations, previousblockhash.",
     {"type": "object", "properties": {"height": {"type": "integer", "description": "Block height (optional)"},
                                       "blockhash": {"type": "string", "description": "Block hash (optional; use instead of height)"}},
      "additionalProperties": False}, lambda a: t_btc_block_summary(a.get("height"), a.get("blockhash"))),    ("btc_get_address",
     "Balance and activity of a Bitcoin address, with no key involved. "
     "When to use: reconciling a merchant wallet, or checking whether an address received funds and how active it is. "
     "Do not use: we do not label or risk-score addresses; for a single payment use btc_get_transaction. "
     "Returns: address, confirmed/unconfirmed balance, utxo count and total, tx count, last 10 transactions. "
     "Supports P2PKH (1..), P2SH (3..), bech32 (bc1q..), bech32m (bc1p..). "
     "Ultra-active addresses may return a degraded=true response under index load rather than hanging.",
     {"type": "object", "properties": {"address": {"type": "string", "description": "Bitcoin mainnet address"}},
     "required": ["address"], "additionalProperties": False}, lambda a: t_btc_address_summary(a.get("address"))),
    ("xmr_get_node_status",
     "Monero node state: height vs target, sync flag, difficulty, txpool size, database size, monerod version and peer counts. "
     "When to use: whether the XMR node is synced and healthy, or how big its chain and txpool are. "
     "Do not use: RobotBase does not index Monero transactions, wallets or ring signatures - a Monero payment cannot be verified or risk-scored from here. "
     "Returns: height, target_height, synced, status, network, difficulty, txpool_txs, database_size_gb, "
     "block_size_limit, version, incoming_connections / outgoing_connections.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_xmr_node_status()),
    ("broadcast_raw_transaction",
     "Relay an already-signed raw transaction to the network through our own full node (non-custodial: we never see a private key). "
     "When to use: an agent signed a transaction locally and wants a high-availability broadcast path into the Bitcoin network. "
     "Do not use: to build or sign a transaction (we never hold keys), or to look up a payment (that is btc_get_transaction). "
     "Returns: {accepted, txid, chain, relayed} on success, or {accepted:false, reason} - a rejected transaction is never relayed. "
     "Availability: disabled by default on this deployment; the operator enables it with RB_ENABLE_BROADCAST=1.",
     {"type": "object", "properties": {"chain": {"type": "string", "enum": ["btc"]},
                                       "signed_raw_tx_hex": {"type": "string", "description": "Raw signed transaction hex"}},
      "required": ["chain", "signed_raw_tx_hex"], "additionalProperties": False},
     lambda a: t_broadcast_raw_transaction(a.get("chain"), a.get("signed_raw_tx_hex"))),
    ("robotbase_services",
     "Live availability of every service behind the RobotBase gateway: the chain nodes, Web3 Agent Hub, AITOKENS and MCP itself. "
     "When to use: an overall platform health check, or finding which service is down. "
     "Do not use: for a single chain's node state (chain_status) or its capability boundary (robotbase_capabilities). "
     "Returns: services[] {name, ok, detail} plus an overall ok flag.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_robotbase_services()),
    ("tron_check_address_risk",
     "TRON address risk intelligence: judicial and black-market labels, account profile and a 0-100 safety score. "
     "When to use: before accepting USDT from a counterparty - OTC settlement, merchant payout, or any "
     "agent that must not receive tainted funds; also to sanity-check an address you are about to pay. "
     "Do not use: to confirm that a specific transfer arrived (that is tron_verify_usdt_transfer). "
     "Returns: risk_level (low|medium|high|unknown), safety_score, hit_flags (sanctioned / money_laundering / "
     "mixer / cybercrime / phishing / blacklist_doubt ...), malicious_contracts_created, account {activated, "
     "age_days, balance_trx, is_contract, trc20_holdings}, not_covered[] and a disclaimer. "
     "Metered: 5 free calls per day per IP, unlimited with an API key.",
     {"type": "object", "properties": {"address": {"type": "string",
                                                   "description": "TRON base58 address (T...)"}},
      "required": ["address"], "additionalProperties": False},
     lambda a: t_tron_check_address_risk(a.get("address"))),
    ("tron_verify_usdt_transfer",
     "TRC-20 USDT settlement check by transaction id: did it land, is it irreversible, how much, from and to whom. "
     "When to use: an agent or a merchant must confirm a USDT payment before releasing value - this is the "
     "settlement check that closes an automated trade loop. "
     "Do not use: to assess the counterparty (that is tron_check_address_risk). "
     "Returns: found, status (SUCCESS|FAILED), finalized (present on the solidity/irreversible chain), "
     "confirmations, blocks_until_finality, block_number, block_time_utc, amount_usdt, from, to, fee_trx, "
     "checks {}, verified and a plain-language verdict. Pass expected_to / expected_amount_usdt to have the "
     "recipient and amount matched for you. "
     "Metered: 5 free calls per day per IP, unlimited with an API key.",
     {"type": "object", "properties": {"txid": {"type": "string", "description": "TRON transaction id (64 hex chars)"},
                                       "expected_to": {"type": "string", "description": "Optional: the recipient you expect"},
                                       "expected_amount_usdt": {"type": "number", "description": "Optional: the USDT amount you expect"}},
      "required": ["txid"], "additionalProperties": False},
     lambda a: t_tron_verify_usdt_transfer(a.get("txid"), a.get("expected_to"), a.get("expected_amount_usdt"))),
    ("sol_check_token_safety",
     "Solana token rug-pull screen: mint and freeze authority, liquidity lock, holder concentration. "
     "When to use: before an agent buys or quotes a Solana token, or when a mint address shows up in a "
     "trade signal and you need the safety facts first. "
     "Do not use: for price or liquidity depth in USD (no DEX aggregator is queried here); for TRON "
     "addresses use tron_check_address_risk. "
     "Returns: risk_level (low|medium|high), safety_score, mint_authority_revoked, freeze_authority_revoked, "
     "lp_locked_pct, lp_holder_pct, top10_holder_pct, holder_count, transfer_fee / transfer_hook presence, "
     "warnings[], not_covered[] and a disclaimer. "
     "Metered: 5 free calls per day per IP, unlimited with an API key.",
     {"type": "object", "properties": {"mint": {"type": "string", "description": "Solana token mint address (base58)"}},
      "required": ["mint"], "additionalProperties": False},
     lambda a: t_sol_check_token_safety(a.get("mint"))),
    ("base_check_contract_risk",
     "Base (EVM) token and contract risk screen: honeypot, transfer taxes, verified source, backdoors "
     "and liquidity lock. "
     "When to use: before an agent trades, quotes or accepts a token on Base, or when an address shows up "
     "in an AgentKit / onchain-agent flow and you need the hard facts first - can it be sold, what does "
     "the exit cost, who can change the rules. "
     "Do not use: for Solana mints (that is sol_check_token_safety) or for TRON addresses "
     "(tron_check_address_risk); it does not simulate a live sell. "
     "Returns: risk_level (low|medium|high), safety_score, is_honeypot, cannot_sell_all, buy_tax_pct, "
     "sell_tax_pct, is_open_source, is_proxy, is_mintable, owner_change_balance, hidden_owner, "
     "can_take_back_ownership, transfer_pausable, selfdestruct, holder_count, top10_holder_pct, "
     "lp_locked_pct, dex pairs, warnings[], not_covered[] and a disclaimer. "
     "Metered: 5 free calls per day per IP, unlimited with an API key.",
     {"type": "object", "properties": {"contract": {"type": "string",
                                                    "description": "Base ERC-20 contract address (0x...)"}},
      "required": ["contract"], "additionalProperties": False},
     lambda a: t_base_check_contract_risk(a.get("contract"))),
]


def tools_list():
    return [{"name": n, "description": d, "inputSchema": s} for n, d, s, _ in TOOLS]


def call_tool(name, args):
    for n, _d, _s, fn in TOOLS:
        if n == name:
            if n not in RISK_TOOLS:
                if _client_plan() == "key":
                    _count_key_call()
                return fn(args or {})
            gate = _risk_gate()                      # 额度用尽则抛 PaymentRequired，不消耗上游
            if _client_plan() == "key":
                out = fn(args or {})
                meter = _key_meter()
                if isinstance(out, dict):
                    out["_metering"] = meter
                else:
                    out = {"result": out, "_metering": meter}
                return out
            tier = gate.get("free_tier") or {}
            used_before, limit = tier.get("used_today", 0), tier.get("limit_per_day", -1)
            out = fn(args or {})
            if isinstance(out, dict) and out.pop("_no_charge", False):
                meter = _meter_payload(used_before, limit, pending=True) if limit >= 0 else _meter_payload(0, -1)
            else:
                meter = _risk_commit(used_before, limit)
            if isinstance(out, dict):
                out["_metering"] = meter
            else:
                out = {"result": out, "_metering": meter}
            return out
    raise ValueError(f"unknown tool: {name}")


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": ("RobotBase is the semantic verification and risk-intelligence surface for "
                             "Web3 agents. Call list_chains or robotbase_capabilities first to see which "
                             "chains are live today (BTC/ETH/XMR/ZEC) and what each node can and cannot "
                             "answer. Every tool is a read-only query: none of them sign, trade, move "
                             "funds or hold keys, and RobotBase operates no mining pool. For payment "
                             "safety use tron_check_address_risk before accepting USDT and "
                             "tron_verify_usdt_transfer before releasing value; sol_check_token_safety "
                             "screens a Solana mint before a trade; base_check_contract_risk screens a Base "
                             "ERC-20 before touching it (honeypot, taxes, verified source, backdoors). "
                             "Those four risk tools are metered (free daily allowance per IP, then an API "
                             "key; every answer carries its own _metering block, and a key holder can read "
                             "usage at https://robotbase.cc/billing/v1/usage).")}}
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
        except PaymentRequired as pay:
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(
                    {"error": "payment_required", "tool": name, "message": str(pay),
                     "free_per_day": RISK_FREE_PER_DAY, "top_up": TOPUP_URL},
                    ensure_ascii=False)}],
                "isError": True}}
        except Exception as exc:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"error: {exc}"[:300]}],
                                                            "isError": True}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}


DOC_TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RobotBase MCP · the semantic risk &amp; verification layer for Web3 agents</title>
<meta name="description" content="One read-only MCP endpoint where an autonomous agent verifies that an on-chain payment settled and prices the next one. Bitcoin, Ethereum, Monero and Zcash are live; Solana, Base and Tron are provisioning. No public RPC, no custody, no tracking.">
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
<div class="tag">Read-only MCP · BTC / ETH / XMR / ZEC live · Solana / Base / Tron provisioning · no RPC endpoint · no custody</div>
<div class="sub">RobotBase is the semantic risk and verification layer for Web3 agents: one endpoint where an autonomous agent checks whether a payment actually settled on Bitcoin, prices the next transaction, reads the Zcash shielded-pool distribution, and confirms that a node really is synced - straight from bare-metal nodes we operate ourselves. Solana, Base and Tron are being wired into the aggregation layer and join the live set once they are. RobotBase publishes no RPC endpoint of any kind: this MCP surface is the only read path. Every tool is read-only, none touch trading or funds, and RobotBase operates no mining pool.</div>

<div class="card">
  <div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center">
    <a class="btn" href="#connect">Get connected</a>
    <a class="btn ghost" href="#pricing">Pricing</a>
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
       "params":{"name":"zec_get_pool_status","arguments":{}}}'</pre>
</div>

<h2>All __TOOL_COUNT__ read-only tools</h2>
<div class="card" style="padding:0;overflow:hidden">
<table><tr><th style="width:230px">Tool</th><th>What it returns</th></tr>__TOOLS_ROWS__</table>
</div>

<h2>Architecture</h2>
<div class="card"><pre>External AI agent ──HTTPS (Streamable HTTP)──► https://robotbase.cc/mcp
                                            │ Cloudflare Tunnel
                                            ▼
                      gateway (/mcp reverse proxy) ──► MCP service (:8090)
                        ├── BTC   full node RPC + electrs (address/tx index)
                        ├── ETH   Reth execution + Lighthouse consensus
                        ├── XMR   monerod full node
                        ├── ZEC   Zebra full node (incl. value-pool / shielded supply)
                        ├── SOL / BASE / TRON   provisioning - light API aggregation, no heavy node on this host
                        └── gateway service aggregation (status pages / dashboards)</pre></div>

<h2>Rate limits and privacy</h2>
<div class="card"><div class="grid">
<div><div class="muted">Verification tools</div><div><b>Free</b> - no key, no signup</div></div>
<div><div class="muted">Risk tools</div><div><b>5 free/day per IP</b>, then a prepaid API key</div></div>
<div><div class="muted">Pricing</div><div><b>Free</b> during the pioneer beta - a prepaid API-key tier follows</div></div>
<div><div class="muted">Anonymous</div><div><b>120 req/min/IP</b></div></div>
<div><div class="muted">With API key</div><div><b>600 req/min/IP</b> (<code>X-API-Key</code>)</div></div>
<div><div class="muted">Privacy</div><div>Audit keeps only the first 16 chars of the IP SHA256 — <b>query arguments are never logged</b></div></div>
<div><div class="muted">Data</div><div>Read-only on-chain data; no trading, signing or custody</div></div>
</div></div>

<h2>Risk intelligence (metered)</h2>
<div class="card">
  <p class="muted" style="margin-top:0">Four tools answer questions a free block explorer cannot:
  is this counterparty tainted, did the USDT really arrive irreversibly, is this Solana token a
  rug, and can this Base contract be sold at all. They are aggregations over public datasets
  (GoPlus Labs + TronGrid), read-only, no keys, no custody.</p>
<ul style="margin:0;padding-left:20px">
<li><code>tron_check_address_risk</code> - judicial / black-market labels, account age, 0-100 safety score</li>
<li><code>tron_verify_usdt_transfer</code> - finalized?, amount, sender, recipient, matched against what you expect</li>
<li><code>sol_check_token_safety</code> - mint &amp; freeze authority, liquidity lock, top-10 concentration</li>
<li><code>base_check_contract_risk</code> - Base honeypot screen, buy/sell tax, verified source, backdoors, LP lock</li>
</ul>
  <p class="muted" style="margin-bottom:0">Free allowance: <b>__RISK_FREE__ calls per day per IP</b>. After that the
  tool answers <code>Payment Required</code> and points at the top-up page. An API key lifts the cap.</p>
</div>

<h2 id="pricing">Pricing</h2>
<div class="card"><div class="grid">
<div><div class="muted">Verification tools (13)</div><div><b>$0</b> — free forever, no key, no signup</div></div>
<div><div class="muted">Risk intelligence (4 tools)</div><div><b>__RISK_FREE__ calls/day per IP free</b>, then a prepaid API key</div></div>
<div><div class="muted">Pioneer beta</div><div><b>Free key for the first 100 developers</b> — 600 req/min, unlimited risk calls</div></div>
<div><div class="muted">After the beta</div><div>prepaid API key, topped up in <b>USDT (TRC-20)</b> or <b>BTC</b> on-chain</div></div>
</div>
<p class="muted" style="margin-bottom:0">Keys are issued from the billing gateway at
<a href="/billing/pricing">/billing/pricing</a>; send yours back as <code>X-API-Key</code> on this same
endpoint. Nothing is metered by surprise: every risk-tool answer carries its own <code>_metering</code>
block with the allowance and the reset time (00:00 Asia/Shanghai).</p>
</div>

<h2>Example prompts</h2>
<div class="card"><ul style="margin:0;padding-left:20px">
<li>"What is the cheapest BTC fee right now?" → <code>btc_get_fee_rates</code></li>
<li>"Did this txid actually confirm, and in which block?" → <code>btc_get_transaction</code></li>
<li>"How much ZEC is in the Zcash shielded pools?" → <code>zec_get_pool_status</code></li>
<li>"Is the Monero node synced, and how deep is its txpool?" → <code>xmr_get_node_status</code></li>
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
        ("① Chain overview", ["list_chains", "chain_status", "robotbase_capabilities", "robotbase_services"]),
        ("② Bitcoin (BTC) — settlement verification & fees", ["btc_get_fee_rates", "btc_get_mempool_state",
                                                              "btc_get_transaction", "btc_get_block",
                                                              "btc_get_address", "broadcast_raw_transaction"]),
        ("③ Zcash (ZEC) — shielded-pool intelligence", ["zec_get_pool_status", "zec_get_recent_blocks"]),
        ("④ Monero (XMR) — node telemetry", ["xmr_get_node_status"]),
        ("⑤ Risk intelligence — metered", ["tron_check_address_risk", "tron_verify_usdt_transfer",
                                           "sol_check_token_safety", "base_check_contract_risk"]),
    ]
    desc = {n: d for n, d, _s, _f in TOOLS}
    rows = ""
    for gname, names in groups:
        rows += '<tr class="grp"><td colspan="2">' + gname + '</td></tr>'
        for n in names:
            if n not in desc:      # 分组里若残留已退役的工具名，跳过而不是渲染成空行
                continue
            d = desc.get(n, "")
            short = d.split("When to use")[0].split("Do not use")[0].split("Note:")[0].strip().rstrip(".") + "."
            when = ""
            if "When to use:" in d:
                when = d.split("When to use:")[1].split("Do not use")[0].split("Note:")[0].strip().rstrip(".")
            rows += ('<tr><td><code>' + n + '</code></td><td>' + short +
                     ('<div class="when">When to use: ' + when + '</div>' if when else '') + '</td></tr>')
    return (DOC_TEMPLATE.replace("__TOOLS_ROWS__", rows)
                .replace("__TOOL_COUNT__", str(len(TOOLS)))
                .replace("__RISK_FREE__", str(RISK_FREE_PER_DAY)))


def server_card():
    desc = {n: d.split("【")[0].strip() for n, d, _s, _f in TOOLS}
    return {
        "name": "robotbase-mcp",
        "title": "RobotBase MCP Server - on-chain verification & risk intelligence for Web3 agents",
        "version": "0.10.0",
        "description": "The semantic verification and risk-intelligence layer for Web3 agents, as one "
                       "read-only MCP endpoint. Live: Bitcoin (fees, mempool state, tx/block/address "
                       "verification, optional raw-tx relay), Ethereum (Reth execution + Lighthouse "
                       "consensus state), Monero (node telemetry - no tx or wallet indexing) and Zcash "
                       "(node state + all six shielded value pools + recent blocks). Risk intelligence "
                       "as light aggregation over public datasets: TRON address risk, TRC-20 USDT "
                       "settlement verification, Solana rug-pull screening and Base (EVM) contract "
                       "honeypot/tax/backdoor screening. RobotBase publishes no "
                       "RPC endpoint of any kind. Verification tools are free; risk tools include a "
                       "daily free allowance with a prepaid API key above it.",
        "homepage": "https://robotbase.cc/mcp",
        "transport": {"type": "streamable-http", "url": "https://robotbase.cc/mcp"},
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "auth": {"type": "none", "optional": "X-API-Key header or ?key= raises rate limit to 600/min per IP"},
        "rateLimits": {"anonymous": "120/min per IP", "withApiKey": "600/min per IP"},
        "tags": ["bitcoin", "ethereum", "monero", "zcash", "solana", "base", "tron", "blockchain-data",
                 "onchain", "verification", "risk-intelligence", "aml", "rug-pull", "honeypot", "evm",
                 "usdt", "mempool",
                 "fees", "privacy-coins", "read-only", "mcp-server", "web3-agents"],
        "pricing": {"verification_tools": "free", "risk_tools": "%d free calls/day per IP, then a prepaid API key" % RISK_FREE_PER_DAY,
                    "top_up": TOPUP_URL},
        "tools": [{"name": n, "description": desc[n]} for n, _d, _s, _f in TOOLS],
    }


# ============================================================ 2026-09-27 三链扩展（BTC/XMR/ZEC）
XMR_HELPER = os.environ.get("XMR_HELPER", os.environ.get("RB_XMR_HELPER", "http://127.0.0.1:18085/rpc"))


def t_xmr_node_status():
    """Monero node state straight from our monerod helper RPC."""
    d = cached("xmr_info", 10, lambda: _http_json(
        XMR_HELPER, {"jsonrpc": "2.0", "id": 1, "method": "get_info"}, timeout=10))
    r = (d or {}).get("result") or {}
    if not r:
        return {"available": False, "reason": "monerod helper RPC unreachable"}
    return {
        "height": r.get("height"), "target_height": r.get("target_height"),
        "synced": bool(r.get("synchronized")) and not bool(r.get("busy_syncing")),
        "status": r.get("status"),
        "network": "mainnet" if r.get("mainnet") else "stagenet/testnet",
        "difficulty": r.get("difficulty"),
        "txpool_txs": r.get("tx_pool_size"),
        "database_size_gb": round((r.get("database_size") or 0) / 1e9, 2),
        "version": r.get("version"), "uptime_s": r.get("uptime"),
        "incoming_connections": r.get("incoming_connections_count"),
        "outgoing_connections": r.get("outgoing_connections_count"),
        "block_size_limit": r.get("block_size_limit"),
        "source": "our monerod get_info (helper RPC)",
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
<div class="sub">Window: last <b>{hours}</b> hours · source <code>the local usage audit store</code> · only the first 16 chars of the IP SHA256 are kept (irreversible)</div>
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
        _REQ.ip = (self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For")
                   or self.client_address[0])
        _REQ.key = (self.headers.get("X-API-Key") or
                    (self.path.split("key=", 1)[1].split("&")[0] if "key=" in self.path else ""))
        _REQ.plan = plan
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
    init_risk_db()
    ThreadingHTTPServer((BIND, PORT), H).serve_forever()
