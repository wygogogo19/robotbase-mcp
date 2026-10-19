#!/usr/bin/env python3
"""RobotBase MCP Server — one read-only surface for a five-chain roster.

2026-10-18：Base / Solana 的链上数据工具已下线（8 个工具），这份参考实现的名册只保留
BTC/ETH/XMR/ZEC/TRON 五条链。BTC/ETH/XMR/ZEC 由自建全节点回答；TRON 背后是自建的
java-tron lite full node，只覆盖最近 65,536 块（约 6 天）与最新状态，超出窗口的查询
会被明确拒绝。修剪与保留窗口是节点的属性，能力矩阵工具（robotbase_capabilities）如实
对外说明边界。x402 支付（Base/Solana 上的 USDC）保留，但它只是收款通道，不提供这两条
链的链上数据服务。
"""
import base64, hashlib, html, html.parser, ipaddress, json, os, re, socket, sqlite3, threading, time, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET, zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client

# x402 付款面（本模块只用标准库：调 facilitator 的 HTTP 接口 + 本地 sqlite）
try:
    import x402_pay
except Exception:            # 缺文件时整个服务仍要能起，x402 只是降级不可用
    x402_pay = None

# 2026-10-16 付款核验台账：只记 verified=true 的链上付款事实（见 verify_ledger.py），
# 供 a2a 目录的「Agent 可信度评分」当付款数据源。缺文件时服务照常起。
try:
    import verify_ledger
except Exception:            # noqa: BLE001
    verify_ledger = None

PORT = int(os.environ.get("RB_PORT", "8090"))
BIND = os.environ.get("RB_BIND", "127.0.0.1")
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "robotbase-mcp", "version": "0.25.2", "title": "RobotBase MCP — search, read, verify and pay from one read-only endpoint"}
BTC_ENV = os.environ.get("RB_BTC_ENV", "")
ZEC_BASE = os.environ.get("RB_ZEC_BASE", "http://127.0.0.1:8080")
HOME_BASE = os.environ.get("RB_HOME_BASE", "http://127.0.0.1:8081")

# 2026-10-14：DOGE / LTC 的基址常量已删除。
# 原因：两条链 2026-09-27 就已退休、不在名册内，但常量一直指向
# 已退休链的常量指向旧容器；容器被回收后 IP 可能被重新分配，悬空常量会静默
# 指向错误服务，所以这里不再保留任何具体地址。

_cache, _cache_lock = {}, threading.Lock()
# ---- BTC 地址摘要加固参数（2026-09-16；2026-10-15 重定标）----
# 2026-10-15 外部实测：本机 electrs 0.11 对「有过交易」的地址，**每一次** scripthash
# 查询（get_balance / listunspent / get_history）都要 4~11 秒（未订阅路径的特性，见
# electrs 日志 "less efficient method ... unsubscribed scripthash"）。旧值 4s 硬预算
# + 单步上限 2.5/1.2/1.6s 意味着**每一个普通地址都必然超时**，对外表现为「余额永远取不到」。
# 已用 ASB 冻结实验排除「被客户端打爆」：冻结后延迟一模一样（4.3~8.5s），是索引本身的固有开销。
# 2026-10-15 第二轮实测（决定性）：electrs 把**已订阅**的 scripthash 缓存在**连接**上——
#   未订阅，单连接三步串行：8.41s + 9.72s + 10.70s = 28.84s（1F1tAaz…，1845 笔）
#   先 subscribe 再同连接三步：10.66s + 0.14s + 0.05s + 0.58s = 11.43s（同一地址，快 2.5 倍）
# 所以正确姿势是「一条连接 + 先 subscribe」，而不是「三个方法各自建连」。
BTC_ADDR_BUDGET = 25.0         # 总预算秒数（覆盖 subscribe+balance+utxo+history）
BTC_ADDR_STEP_SUBSCRIBE = 15.0 # 单步上限：subscribe（最贵的一步，它把索引读一次并缓存到连接上）
BTC_ADDR_STEP_BALANCE = 6.0    # 单步上限：余额（订阅成功后实测 0.1s 级，留足余量）
BTC_ADDR_STEP_UTXO = 6.0       # 单步上限：UTXO 列表
BTC_ADDR_STEP_HISTORY = 8.0    # 单步上限：交易历史（订阅后 0.6s 级；异常时才可能变慢）
BTC_ADDR_GATE_WAIT = 5.0       # 排队等锁上限：宁可快速回「忙」，也不要让调用方干等 25s
BTC_ADDR_CACHE_TTL = 900       # 正常结果缓存（15 分钟）
BTC_ADDR_CACHE_TTL_BAD = 120   # 降级/超时结果缓存，避免热门地址反复打 electrs
BTC_ELECTRUM_HOST = os.environ.get("RB_ELECTRS_HOST", "127.0.0.1")
BTC_ELECTRUM_PORT = 50001
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
    base = {"zec": ZEC_BASE}.get(chain)
    if not base:
        raise ValueError(f"unknown chain: {chain}")
    d = _http_json(base + "/api/status")
    return {k: d.get(k) for k in ("chain", "blocks", "headers", "estimated_height", "verification_progress_pct",
                                  "initial_block_download", "connections", "mempool_txs", "size_on_disk_gb", "rpc_ok", "error")}


def _jlen(obj):
    try:
        return len(json.dumps(obj, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        return 10 ** 9


_PRIORITY_KEYS = ("_x402", "_metering", "_no_charge", "_truncated", "_original_chars",
                  "_truncated_note", "ok", "error", "reason", "tool")


def _shrink_value(v, budget):
    """Return a size-reduced copy of v that serialises to <= budget if possible."""
    if _jlen(v) <= budget:
        return v
    if isinstance(v, list):
        if not v:
            return v
        # keep the largest prefix that fits, at least one item
        lo, hi = 1, len(v)
        best = v[:1]
        while lo <= hi:
            mid = (lo + hi) // 2
            if _jlen(v[:mid]) <= budget:
                best = v[:mid]
                lo = mid + 1
            else:
                hi = mid - 1
        return best
    if isinstance(v, dict):
        out = {}
        remaining = budget
        prio = [(k, val) for k, val in v.items() if k in _PRIORITY_KEYS]
        rest = [(k, val) for k, val in v.items() if k not in _PRIORITY_KEYS]
        items = prio + sorted(rest, key=lambda kv: _jlen(kv[1]))
        for idx, (k, val) in enumerate(items):
            if remaining <= 20:
                break
            cap = max(20, min(remaining - 20, 800)) if idx < len(prio) else max(20, remaining - _jlen(k) - 8)
            got = _shrink_value(val, cap)
            out[k] = got
            remaining -= _jlen(k) + _jlen(got) + 6
        return out
    if isinstance(v, str):
        if budget <= 24:
            return v[:max(0, budget - 8)]
        return v[:budget - 8] + "…"
    return v


def truncate(obj, limit=3800):
    """Serialise to JSON and guarantee the returned string is *valid JSON*.

    2026-10-19 fix (ported from the deployed v0.22.0, QA P1-1): the old implementation
    cut the serialised string at `limit`, so any payload above the cap became
    unparseable JSON. We now shrink lists/strings/dicts structurally and add explicit
    truncation metadata instead of slicing the text.
    """
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        obj = {"result": str(obj)[:2000]}
        s = json.dumps(obj, ensure_ascii=False)
    if len(s) <= limit:
        return s
    original_chars = len(s)
    budget = max(600, limit - 220)
    shrunk = _shrink_value(obj, budget)
    if isinstance(shrunk, dict):
        shrunk = dict(shrunk)
        shrunk["_truncated"] = True
        shrunk["_truncated_note"] = ("response exceeded the %d-character transport cap; collections were "
                                     "shortened. Increase limit/per-page arguments for more." % limit)
        shrunk["_original_chars"] = original_chars
    elif isinstance(shrunk, list):
        shrunk = {"_truncated": True, "_truncated_note": "list shortened to fit the transport cap",
                  "_original_chars": original_chars, "items": shrunk}
    else:
        shrunk = {"_truncated": True, "_truncated_note": "value shortened to fit the transport cap",
                  "_original_chars": original_chars, "value": shrunk}
    out = json.dumps(shrunk, ensure_ascii=False, default=str)
    if len(out) > limit:      # last resort: still valid JSON, keep billing/metering keys
        keep = {k: obj[k] for k in _PRIORITY_KEYS if k in obj and _jlen(obj[k]) < 900}
        keep.update({"_truncated": True, "_original_chars": original_chars,
                     "_truncated_note": "response exceeded the transport cap and was replaced",
                     "_preview": out[:max(120, limit - 320)]})
        out = json.dumps(keep, ensure_ascii=False, default=str)[:limit]
        if not out.rstrip().endswith("}"):
            out = json.dumps({"_truncated": True, "_original_chars": original_chars,
                              "_truncated_note": "response exceeded the transport cap",
                              "_preview": out[:max(120, limit - 200)]}, ensure_ascii=False)
    return out


# 2026-10-18：Base / Solana 的链上数据工具下线，名册收敛为五条链
# （BTC/ETH/XMR/ZEC/TRON），五条今天都是 live。成熟度仍只用 live / provisioning /
# planned 三态；provisioning / planned 的链绝不冒充可查询。
# LTC/DOGE/KAS/RVN、TON 以及 Base/Solana 均已从名册下线。
CHAINS = ("btc", "eth", "xmr", "zec", "tron")
ROSTER_CHAINS = ("btc", "eth", "xmr", "zec", "tron")


def _as_num(v):
    """Coerce gateway-formatted strings ('#972,109', '8 peers') to numbers (ported P2-16)."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        m = re.search(r"-?\d[\d,]*", v)
        if m:
            try:
                return int(m.group(0).replace(",", ""))
            except ValueError:
                return v
    return v


def _nums(d, keys):
    out = dict(d)
    for k in keys:
        if k in out:
            out[k] = _as_num(out[k])
    return out


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
                    "height": _as_num(n.get("height")),
                    "state": n.get("state") or c.get("state")})
    if not out:                                  # 网关不可达时退回静态名册
        out = [{"chain": c, "tier": "live"} for c in ROSTER_CHAINS]
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
        d = cached("btc_node", 10, lambda: _http_json("http://127.0.0.1:8600/api/node"))
        return _nums({k: d.get(k) for k in ("chain", "blocks", "headers", "verification_progress_pct", "initial_block_download",
                                            "connections", "connections_out", "mempool_txs", "mempool_usage_mb",
                                            "size_on_disk_gb", "version", "rpc_ok")},
                     ("blocks", "headers", "connections", "connections_out", "mempool_txs", "mempool_usage_mb"))
    if chain == "xmr":
        n = (cached("nodes", 10, lambda: _http_json(HOME_BASE + "/api/nodes")).get("nodes") or {}).get("xmr") or {}
        return _nums({k: n.get(k) for k in ("ok", "synced", "height", "txpool", "difficulty", "db",
                                            "txc", "sync", "state")},
                     ("height", "txpool", "difficulty", "db", "txc"))
    if chain == "zec":
        return svc_status(chain)
    if chain == "eth":
        n = (cached("nodes", 10, lambda: _http_json(HOME_BASE + "/api/nodes")).get("nodes") or {}).get("eth") or {}
        return _nums({k: n.get(k) for k in ("ok", "synced", "height", "peers", "exec", "cons", "slot",
                                            "progress", "state")},
                     ("height", "peers", "slot"))
    if chain == "tron":
        n = (cached("nodes", 10, lambda: _http_json(HOME_BASE + "/api/nodes")).get("nodes") or {}).get("tron") or {}
        return _nums({k: n.get(k) for k in ("ok", "synced", "height", "solidity", "peers", "client",
                                            "lag_s", "progress", "state")},
                     ("height", "solidity", "peers", "lag_s"))
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
        out = {"chain": "BTC",
               "txs": mp.get("size"), "bytes": mp.get("bytes"),
               "usage_mb": round(usage / 1e6, 1),
               "max_mempool_mb": round(cap / 1e6, 1),
               "load_pct": round(100 * usage / cap, 1),
               "min_fee_btc_kvb": mp.get("mempoolminfee"),
               "total_fees_btc": mp.get("total_fee"),
               "verdict": "busy" if usage > 0.5 * cap else "normal",
               "source": "our own bitcoind getmempoolinfo"}
        # 2026-10-16 加深：费率直方图（矿工视角的"要排多久"），按 sat/vB 分桶累计 vbytes
        try:
            entries = btc_rpc("getrawmempool", [True]) or {}
            buckets = [(0, 1), (1, 2), (2, 5), (5, 10), (10, 20), (20, 50), (50, 100), (100, 10 ** 9)]
            hist = [{"min_sat_vb": lo, "max_sat_vb": (hi if hi < 10 ** 9 else None),
                     "txs": 0, "vbytes": 0} for lo, hi in buckets]
            for _tx, e in entries.items():
                try:
                    vs = e.get("vsize") or e.get("size") or 0
                    rate = (e["fees"]["base"] * 1e8 / vs) if vs else 0
                except Exception:  # noqa: BLE001
                    continue
                for i, (lo, hi) in enumerate(buckets):
                    if lo <= rate < hi:
                        hist[i]["txs"] += 1
                        hist[i]["vbytes"] += vs
                        break
            out["fee_rate_histogram"] = hist
            out["how_to_read"] = ("fee_rate_histogram buckets the pool by sat/vB with the vbytes queued in "
                                  "each bucket: what sits below your chosen rate is what you are bidding "
                                  "against for the next block.")
        except Exception:  # noqa: BLE001
            out["fee_rate_histogram"] = None
        return out
    return cached("btc_mp", 20, build)


def t_btc_tx_lookup(txid):
    txid = (txid or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", txid):
        raise ValueError("txid must be 64 hex chars")
    tx = None
    err = None
    try:
        tx = btc_rpc("getrawtransaction", [txid, True])
    except Exception as exc:  # noqa: BLE001 未确认且不在本地索引时可能取不到
        err = exc
    if tx is None:
        # 2026-10-16 加深：交易可能仍在内存池里；这时仍然能回答"卡住没、能不能加价"
        try:
            entry = btc_rpc("getmempoolentry", [txid]) or {}
        except Exception:  # noqa: BLE001
            raise ValueError("transaction not found on our node (and not in the mempool): %s"
                             % (str(err)[:80] if err else "unknown"))
        return {"txid": txid, "in_mempool": True, "confirmations": 0,
                "mempool": _btc_mempool_detail(entry),
                "note": "the raw transaction is not in our local index yet, but it is in our mempool",
                "source": "our own bitcoind getmempoolentry"}
    vout = [{"n": v["n"], "value": v["value"], "type": v["scriptPubKey"].get("type"),
             "address": (v["scriptPubKey"].get("address") or (v["scriptPubKey"].get("addresses") or [None])[0])}
            for v in (tx.get("vout") or [])][:12]
    out = {"txid": tx.get("txid"), "blockhash": tx.get("blockhash"), "confirmations": tx.get("confirmations"),
           "time": tx.get("time"), "size": tx.get("size") or tx.get("vsize"), "vin_count": len(tx.get("vin") or []),
           "vout": vout, "value_out_btc": round(sum(v["value"] for v in (tx.get("vout") or [])), 8),
           "in_mempool": tx.get("confirmations") is None}
    if out["in_mempool"]:
        try:
            entry = btc_rpc("getmempoolentry", [txid]) or {}
            out["mempool"] = _btc_mempool_detail(entry)
        except Exception:  # noqa: BLE001
            out["mempool"] = None
    return out


def _btc_mempool_detail(entry):
    """把 getmempoolentry 翻译成"卡住了吗 / 能不能加价"的结论。"""
    vs = entry.get("vsize") or entry.get("size") or 0
    base = ((entry.get("fees") or {}).get("base") or 0)
    anc = ((entry.get("fees") or {}).get("ancestor") or 0)
    desc_size = entry.get("descendantsize") or 0
    rbf = bool(entry.get("bip125-replaceable"))
    spentby = entry.get("spentby") or []
    detail = {
        "vsize": vs,
        "fee_btc": base,
        "fee_rate_sat_vb": round(base * 1e8 / vs, 2) if vs else None,
        "ancestor_count": entry.get("ancestorcount"),
        "ancestor_size_vb": entry.get("ancestorsize"),
        "ancestor_fee_btc": anc,
        "ancestor_fee_rate_sat_vb": round(anc * 1e8 / entry["ancestorsize"], 2) if entry.get("ancestorsize") else None,
        "descendant_count": entry.get("descendantcount"),
        "descendant_size_vb": desc_size,
        "depends_on": (entry.get("depends") or [])[:10],
        "spent_by": spentby[:10],
        "rbf_replaceable_bip125": rbf,
    }
    hints = []
    if rbf:
        hints.append("Replaceable via RBF (BIP125): you can re-spend the same inputs with a higher fee.")
    else:
        hints.append("Not RBF-signalled: you cannot bump this transaction directly.")
    if not spentby:
        hints.append("No child is spending it yet, so a CPFP child (spending one of its outputs) is still possible.")
    else:
        hints.append("A child transaction already spends it (%d) - fee-bumping now means bumping the package."
                     % len(spentby))
    if (entry.get("descendantcount") or 0) > 0:
        hints.append("It already has descendants, so any bump must cover the whole package.")
    detail["can_bump"] = {"rbf": rbf, "cpfp_possible": not spentby,
                          "package_fee_rate_sat_vb": round(
                              ((anc + ((entry.get("fees") or {}).get("descendant") or 0)) * 1e8
                               / max(1, (entry.get("ancestorsize") or 0) + desc_size)), 2)
                          if (entry.get("ancestorsize") or desc_size) else None}
    detail["how_to_read"] = " ".join(hints)
    return detail


def t_btc_block_summary(height=None, blockhash=None):
    if blockhash:
        bh = blockhash.strip()
    elif height is not None:
        bh = btc_rpc("getblockhash", [int(height)])
    else:
        bh = btc_rpc("getbestblockhash")
    hdr = btc_rpc("getblockheader", [bh, True])
    blk = btc_rpc("getblock", [bh, 1])
    out = {"hash": bh, "height": hdr.get("height"), "time": hdr.get("time"), "txs": len(blk.get("tx") or []),
           "size": blk.get("size"), "weight": blk.get("weight"), "confirmations": hdr.get("confirmations"),
           "previousblockhash": hdr.get("previousblockhash")}
    # 2026-10-16 加深：区块经济学（手续费总额、费率分位、UTXO 净变化）
    try:
        st = btc_rpc("getblockstats", [hdr.get("height")]) or {}
        out["stats"] = {
            "total_fee_btc": round((st.get("totalfee") or 0) / 1e8, 8),
            "avg_fee_rate_sat_vb": st.get("avgfeerate"),
            "min_fee_rate_sat_vb": st.get("minfeerate"),
            "max_fee_rate_sat_vb": st.get("maxfeerate"),
            "feerate_percentiles_sat_vb": ({"p10": st.get("feerate_percentiles", [None] * 5)[0],
                                            "p25": st.get("feerate_percentiles", [None] * 5)[1],
                                            "p50": st.get("feerate_percentiles", [None] * 5)[2],
                                            "p75": st.get("feerate_percentiles", [None] * 5)[3],
                                            "p90": st.get("feerate_percentiles", [None] * 5)[4]}
                                           if st.get("feerate_percentiles") else None),
            "avg_tx_size_vb": st.get("avgtxsize"),
            "utxo_increase": st.get("utxo_increase"),
            "ins": st.get("ins"), "outs": st.get("outs"),
        }
    except Exception:  # noqa: BLE001
        out["stats"] = None
    return out


def t_robotbase_services():
    d = cached("home", 15, lambda: _http_json(HOME_BASE + "/api/services"))
    return {"updated_utc": d.get("updated_utc"),
            "services": [{"id": s["id"], "ok": bool(s.get("ok")), "metric": s.get("metric") or s.get("error")}
                         for s in d.get("status", [])]}


# ---------------- KAS: node + hashport telemetry from the gateway ----------------
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


BECH32M_CONST = 0x2BC830A3


def bech32_decode(addr):
    """Return (hrp, data, const). 2026-10-19: accept bech32 (1) and bech32m (BIP350)."""
    pos = addr.rfind("1")
    if pos < 1 or pos + 7 > len(addr):
        raise ValueError("bad bech32 address")
    hrp, data = addr[:pos], addr[pos + 1:]
    values = [CHARSET.find(c) for c in data]
    if -1 in values:
        raise ValueError("bad bech32 character")
    const = bech32_polymod([ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + values)
    if const not in (1, BECH32M_CONST):
        raise ValueError("bad bech32 checksum")
    return hrp, values[:-6], const


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


def b58check_decode(s):
    """Base58check: validate the 4-byte double-SHA256 checksum (ported P1-3).

    The old code decoded base58 without checking the checksum, so a typo'd address
    silently mapped to another script and could return plausible balances.
    """
    raw = b58decode(s)
    if len(raw) < 5:
        raise ValueError("bad base58 address length")
    body, chk = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] != chk:
        raise ValueError("bad base58 checksum")
    return body


def address_to_script(addr):
    addr = addr.strip()
    if addr.lower().startswith("bc1"):
        hrp, data, const = bech32_decode(addr.lower())
        if hrp != "bc" or not data:
            raise ValueError("not a Bitcoin mainnet bech32 address")
        witver = data[0]
        prog = bytes(convertbits(data[1:], 5, 8, False))
        # BIP173: witness v0 uses bech32; BIP350: witness v1+ uses bech32m (ported P1-2)
        if witver == 0 and const != 1:
            raise ValueError("bad bech32 checksum (witness v0 must use bech32)")
        if witver >= 1 and const != BECH32M_CONST:
            raise ValueError("bad bech32m checksum (witness v1+ must use bech32m)")
        if witver == 0:
            if len(prog) == 20:
                return b"\x00\x14" + prog
            if len(prog) == 32:
                return b"\x00\x20" + prog
        elif witver == 1 and len(prog) == 32:
            return b"\x51\x20" + prog
        raise ValueError("unsupported witness program")
    raw = b58check_decode(addr)
    if len(raw) != 21:
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


# 2026-10-15 guard: ultra-active addresses make electrs 0.11 fetch thousands of blocks over P2P
# (even for get_balance), which crashes it. Static denylist + dynamic denylist + circuit breaker.
BTC_MEGA_ADDRESSES = {
    "bc1qm34lsc65zpw79lxes69zkqmk6ee3ewf0j77s3h",
    "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
    "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo",
    "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97",
    "3M219KR5vEneNb47ewrPfWyb5jQ2DjxRP6",
    "bc1ql49ydapnjafl5t2cp9zqpjwe6pdgmxy98859v2",
}
_ELECTRS_BREAKER = {"until": 0.0}
_ELECTRS_BREAKER_SEC = 60
_BTC_DYN_DENY = {}  # scripthash -> expiry
_BTC_DYN_DENY_SEC = 3600


def _electrs_trip(reason):
    _ELECTRS_BREAKER["until"] = time.time() + _ELECTRS_BREAKER_SEC
    print(f"[btc_addr] electrs circuit breaker OPEN {_ELECTRS_BREAKER_SEC}s: {reason}", flush=True)


def _btc_addr_unsupported(address, err, note):
    return {"address": address, "degraded": True, "error": err,
            "balance_btc": None, "unconfirmed_btc": None,
            "utxo_count": None, "utxo_value_btc": None, "tx_count": None,
            "note": note, "elapsed_ms": 0}


def electrum_call(method, params, host=None, port=None, timeout=10):
    host = host or BTC_ELECTRUM_HOST
    port = int(port or os.environ.get("RB_ELECTRS_PORT", "50001"))
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall((json.dumps({"id": 1, "method": method, "params": params}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    line = buf.split(b"\n")[0].strip()
    if not line:
        raise ElectrsError("empty response (connection closed)")
    resp = json.loads(line.decode())
    if resp.get("error") is not None:
        raise ElectrsError(str(resp.get("error"))[:300])
    if "result" not in resp:
        raise ElectrsError("missing result")
    return resp


class ElectrsError(Exception):
    pass


class _ElectrsConn:
    """一条 electrs 连接上连续发多次 Electrum 请求（2026-10-15 新增）。

    为什么非要有它：electrs 0.11 对**未订阅** scripthash 走 "less efficient method"
    路径，每个方法（get_balance / listunspent / get_history）都要独立做一次索引读，
    每次 4~11 秒；而 `blockchain.scripthash.subscribe` 一旦成功，electrs 会把该地址
    的结果**缓存在这条连接上**，同一连接后续三步只要 0.05~0.6 秒。

    实测（1F1tAaz5x1HUXrCNLbtMDqcw6o5GNn4xqX，1845 笔交易）：
        三步各自建连：8.41 + 9.72 + 10.70 = 28.84s
        subscribe + 同连接三步：10.66 + 0.14 + 0.05 + 0.58 = 11.43s
    """

    def __init__(self, host, port, timeout):
        self.sock = socket.create_connection((host, port), timeout=timeout)

    def call(self, method, params, timeout):
        self.sock.settimeout(timeout)
        self.sock.sendall((json.dumps({"id": 1, "method": method, "params": params}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        line = buf.split(b"\n")[0].strip()
        if not line:
            raise ElectrsError("empty response (connection closed)")
        resp = json.loads(line.decode())
        if resp.get("error") is not None:
            raise ElectrsError(str(resp.get("error"))[:300])
        if "result" not in resp:
            raise ElectrsError("missing result")
        return resp["result"]

    def close(self):
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass


def t_btc_address_summary(address):
    """BTC 地址摘要（2026-09-16 加固：硬预算 + 分步降级 + 独立缓存）

    背景：SOP 要求慢工具 <4000ms；原实现三步各给 8/8/6s 超时（最坏 22s），
    且失败不缓存 → 热门地址会反复打 electrs 并触发 LLM 超时。

    2026-10-15 第二轮提速：改成「单连接 + 先 subscribe」。electrs 0.11 会把已订阅的
    scripthash 缓存在连接上，同一连接后续三步从 8~11s 掉到 0.05~0.6s；实测
    1F1tAaz…（1845 笔）从 28.84s 降到 11.43s。subscribe 失败则退回各自建连的老路径，
    保证「余额至少能拿到」的语义不变。

    语义契约：拿不到余额时**绝不返回 0**，而是 `balance_btc: null` + `degraded: true`
    + `error`/`note`，让 agent 无法把「查不到」误读成「余额为 0」。
    """
    sh = address_to_scripthash(address)
    _a = (address or "").strip()
    if _a in BTC_MEGA_ADDRESSES or _a.lower() in BTC_MEGA_ADDRESSES or _BTC_DYN_DENY.get(sh, 0) > time.time():
        return _btc_addr_unsupported(address, "address_too_active_unsupported",
            "该地址交易量极大（交易所热钱包/知名大户级），本服务不支持查询其余额与历史：结果不是 0，请勿视为 0。"
            "如需核验某一笔付款，请使用 btc_get_transaction 按交易 ID 查询。")
    if _ELECTRS_BREAKER["until"] > time.time():
        return _btc_addr_unsupported(address, "electrs_unavailable",
            "BTC 地址索引暂时不可用（熔断中，约 %d 秒后重试）：未能取得余额，请勿视为 0。单笔付款可用 btc_get_transaction 核验。"
            % int(_ELECTRS_BREAKER["until"] - time.time()))
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
        bal, berr = None, None
        utxos, hist = None, None

        def _err_note(kind, what):
            if kind == "electrs_timeout":
                return "%s超时未取" % what
            return "%s未取（electrs 错误）" % what

        def _maybe_trip(e):
            # 只有「连接层坏了 / 索引缺块」才拉闸，业务性错误不拉闸
            if "failed to get block" in str(e):
                _BTC_DYN_DENY[sh] = time.time() + _BTC_DYN_DENY_SEC
            if ("failed to get block" in str(e) or isinstance(e, (ConnectionError, OSError))
                    or "empty response" in str(e)):
                _electrs_trip(f"{type(e).__name__}: {str(e)[:120]}")

        sub_ok = False
        conn = None
        # ① subscribe（一次索引读；成功后同连接三步全走缓存，这是 2026-10-15 的关键提速点）
        try:
            conn = _ElectrsConn(BTC_ELECTRUM_HOST, BTC_ELECTRUM_PORT,
                                timeout=max(1.0, min(BTC_ADDR_STEP_SUBSCRIBE, left())))
        except Exception as e:  # noqa: BLE001
            conn, berr = None, "electrs_error"
            print(f"[btc_addr] connect error {address}: {type(e).__name__}: {e}", flush=True)
            _maybe_trip(e)
        if conn is not None:
            try:
                conn.call("blockchain.scripthash.subscribe", [sh],
                          timeout=max(1.0, min(BTC_ADDR_STEP_SUBSCRIBE, left())))
                sub_ok = True
            except socket.timeout:
                berr = "electrs_timeout"
            except Exception as e:  # noqa: BLE001
                berr = "electrs_error"
                print(f"[btc_addr] subscribe error {address}: {type(e).__name__}: {e}", flush=True)
                _maybe_trip(e)
            # 超时的连接里还挂着一个未读响应，必须废弃整条连接（否则响应会串帧）
            if sub_ok:
                # ② 余额（必需）
                try:
                    bal = conn.call("blockchain.scripthash.get_balance", [sh],
                                    timeout=max(1.0, min(BTC_ADDR_STEP_BALANCE, left())))
                    if not isinstance(bal, dict):
                        raise ElectrsError("bad balance result")
                except socket.timeout:
                    bal, berr = None, "electrs_timeout"
                except Exception as e:  # noqa: BLE001
                    bal, berr = None, "electrs_error"
                    print(f"[btc_addr] get_balance error {address}: {type(e).__name__}: {e}", flush=True)
                    _maybe_trip(e)
                # ③ UTXO 列表（可选）
                if bal is not None:
                    if left() < 0.3:
                        degraded = True; notes.append("预算不足，跳过 UTXO 列表")
                    else:
                        try:
                            utxos = conn.call("blockchain.scripthash.listunspent", [sh],
                                              timeout=max(0.3, min(BTC_ADDR_STEP_UTXO, left())))
                            if not isinstance(utxos, list):
                                raise ElectrsError("bad listunspent result")
                        except socket.timeout:
                            utxos = None; degraded = True; notes.append(_err_note("electrs_timeout", "UTXO 列表"))
                        except Exception as e:  # noqa: BLE001
                            utxos = None; degraded = True; notes.append(_err_note("electrs_error", "UTXO 列表"))
                            print(f"[btc_addr] listunspent error {address}: {type(e).__name__}: {e}", flush=True)
                    # ④ 交易历史（可选，最慢的一步：大户动辄几十万条）
                    if left() < 0.4:
                        degraded = True; notes.append("预算不足，跳过交易历史")
                    else:
                        try:
                            hist = conn.call("blockchain.scripthash.get_history", [sh],
                                             timeout=max(0.4, min(BTC_ADDR_STEP_HISTORY, left())))
                            if not isinstance(hist, list):
                                raise ElectrsError("bad history result")
                        except socket.timeout:
                            hist = None; degraded = True; notes.append(_err_note("electrs_timeout", "交易历史"))
                        except Exception as e:  # noqa: BLE001
                            hist = None; degraded = True; notes.append(_err_note("electrs_error", "交易历史"))
                            print(f"[btc_addr] get_history error {address}: {type(e).__name__}: {e}", flush=True)
            conn.close()
        # 回退：subscribe 没成功（超时/报错）时退回「各自建连」的老路径，至少把余额拿到手
        if not sub_ok and bal is None:
            try:
                bal = electrum_call("blockchain.scripthash.get_balance", [sh],
                                    timeout=max(1.0, min(BTC_ADDR_STEP_BALANCE + 6.0, left())))["result"]
                if not isinstance(bal, dict):
                    bal, berr = None, "electrs_error"
                    raise ElectrsError("bad balance result")
                degraded = True
                notes.append("索引订阅不可用（回退路径）：本次只取到余额，UTXO 列表与交易历史未取")
            except socket.timeout:
                bal, berr = None, "electrs_timeout"
            except Exception as e:  # noqa: BLE001
                bal, berr = None, "electrs_error"
                print(f"[btc_addr] get_balance fallback error {address}: {type(e).__name__}: {e}", flush=True)
                _maybe_trip(e)
        if bal is None:
            return {"address": address, "degraded": True, "error": berr,
                    "balance_btc": None, "unconfirmed_btc": None,
                    "utxo_count": None, "utxo_value_btc": None, "tx_count": None,
                    "note": ("%.0fs 预算内未取到余额（electrs 超时）" % BTC_ADDR_BUDGET if berr == "electrs_timeout"
                             else "electrs 返回错误") + "：未能取得余额，请勿视为 0，请稍后重试",
                    "elapsed_ms": int((time.monotonic() - t0) * 1000)}
        out = {"address": address,
               "balance_btc": round((bal.get("confirmed") or 0) / 1e8, 8),
               "unconfirmed_btc": round((bal.get("unconfirmed") or 0) / 1e8, 8),
               "utxo_count": len(utxos) if utxos is not None else None,
               "utxo_value_btc": (round(sum(u.get("value", 0) for u in utxos) / 1e8, 8)
                                  if utxos is not None else None),
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

    # 2026-10-15：先抢锁（最多等 BTC_ADDR_GATE_WAIT 秒），**排队时间不计入预算**——
    # 否则第二个并发调用会拿着被排队吃光的预算进去，必然立刻超时。
    if not _btc_gate.acquire(timeout=BTC_ADDR_GATE_WAIT):
        return _btc_addr_unsupported(
            address, "electrs_busy",
            "BTC 地址索引当前正忙（另一个地址查询正在占用索引层），本次未取到余额——结果不是 0，请稍后重试。")
    try:
        t0 = time.monotonic()
        val = build()
    finally:
        _btc_gate.release()
    ttl = BTC_ADDR_CACHE_TTL_BAD if val.get("degraded") else BTC_ADDR_CACHE_TTL
    with _cache_lock:
        _addr_cache[key] = (now, val, ttl)
    return val




USAGE_DB = os.environ.get("RB_USAGE_DB", "./usage.db")
BILLING_DB = os.environ.get("RB_BILLING_DB", "")  # deprecated: no API keys are issued


def _db(path, timeout=10):
    con = sqlite3.connect(path, timeout=timeout)
    con.row_factory = sqlite3.Row
    return con


def init_usage():
    con = _db(USAGE_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS calls(
        ts INTEGER, tool TEXT, ok INTEGER, ms INTEGER, ip_hash TEXT, plan TEXT)""")
    # 2026-10-15: 目录站爬虫和真人客户端在审计里长得一模一样，分不出来。补三列：
    #   ua     —— 请求头 User-Agent（每次请求都有，最稳定的分组键）
    #   client —— MCP 握手 initialize 里的 clientInfo（name/version）
    #   err    —— 失败原因（单行、截断），之前只记 ok=0，查不出为什么失败
    have = {r[1] for r in con.execute("PRAGMA table_info(calls)")}
    for col, decl in (("ua", "TEXT"), ("client", "TEXT"), ("err", "TEXT")):
        if col not in have:
            con.execute(f"ALTER TABLE calls ADD COLUMN {col} {decl}")
    # 握手单独记一张表：目录站爬虫会握手但从不调用工具，混进 calls 会把调用量撑虚。
    # 有了它才能回答「谁连过 vs 谁真的用了」。
    con.execute("""CREATE TABLE IF NOT EXISTS clients(
        ts INTEGER, ip_hash TEXT, client TEXT, ua TEXT, proto TEXT)""")
    con.commit()
    con.close()


def log_client(ip, client, ua, proto):
    try:
        con = _db(USAGE_DB)
        con.execute("INSERT INTO clients(ts,ip_hash,client,ua,proto) VALUES(?,?,?,?,?)",
                    (int(time.time()), hashlib.sha256((ip or "").encode()).hexdigest()[:16],
                     (client or "")[:80], (ua or "")[:160], (proto or "")[:20]))
        con.commit()
        con.close()
    except Exception:
        pass


def log_call(tool, ok, ms, ip, plan, ua="", client="", err=""):
    try:
        con = _db(USAGE_DB)
        con.execute("INSERT INTO calls(ts,tool,ok,ms,ip_hash,plan,ua,client,err) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (int(time.time()), tool, 1 if ok else 0, int(ms),
                     hashlib.sha256((ip or "").encode()).hexdigest()[:16], plan,
                     (ua or "")[:160], (client or "")[:80],
                     (err or "").replace("\n", " ")[:180]))
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
    by_client = con.execute(
        "SELECT COALESCE(NULLIF(client,''), NULLIF(ua,''), 'unknown') name, COUNT(*) n, "
        "       SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) bad "
        "FROM calls WHERE ts>=? GROUP BY name ORDER BY n DESC LIMIT 12", (since,)).fetchall()
    by_error = con.execute(
        "SELECT COALESCE(NULLIF(err,''), '(no reason recorded)') reason, COUNT(*) n "
        "FROM calls WHERE ts>=? AND ok=0 GROUP BY reason ORDER BY n DESC LIMIT 12", (since,)).fetchall()
    conns = con.execute(
        "SELECT client, COUNT(*) n, COUNT(DISTINCT ip_hash) ips, MAX(ts) last "
        "FROM clients WHERE ts>=? AND COALESCE(client,'')<>'' "
        "GROUP BY client ORDER BY n DESC LIMIT 12", (since,)).fetchall()
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
            "top_tools": [{"tool": r["tool"], "calls": r["c"], "avg_ms": round(r["avg_ms"] or 0)} for r in top],
            "top_clients": [{"client": r["name"], "calls": r["n"], "errors": r["bad"] or 0} for r in by_client],
            "top_errors": [{"reason": r["reason"], "calls": r["n"]} for r in by_error],
            "connectors": [{"client": r["client"], "handshakes": r["n"], "ips": r["ips"],
                            "last_seen_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["last"]))}
                           for r in conns]}


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
    """**已停用**：付费现在只走 x402，不再有 API key 档位。

    2026-10-16 起，MCP 的付费访问只有一条路径：调用时带
    `params._meta["x402/payment"]`（首次结算 + 之后重放同一载荷扣信用）。
    X-API-Key 一律按匿名处理——它不再授予任何额度或速率档位。
    保留此函数是为了让 `_plan()` 的调用点不必改动，行为上是恒等返回 "anon"。
    """
    return "anon"


# =================== v0.5.0: mining / fee / attribution intelligence =============
# Every datum here comes from infrastructure we run ourselves. Chains where we have
# no source are reported as unavailable rather than filled with a guess.

KAS_BLOCK_DB = os.environ.get("RB_KAS_BLOCK_DB", "/var/lib/robotbase-home/kas_blocks.db")
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
        # 与 t_xmr_node_status / t_xmr_network_stats 共用同一个缓存键，
        # 形状必须一致（_xmr_rpc 的结果本体），否则会互相污染。
        r = cached("xmr_info", 10, lambda: _xmr_rpc("get_info")) or {}
        diff = r.get("difficulty")
        _tip, _hdr = cached("xmr_tip", 10, _xmr_tip)
        out["XMR"] = {"difficulty": diff,
                      "network_hashrate_hs": (float(diff) / 120) if diff else None,
                      "height": _tip if _tip else r.get("height"),   # 末块高度，不是链长度
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




# ============ v0.5.1: ZEC coinbase attribution =============
# Lives on our own node and is read-only by construction.


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


# ============== 2026-10-14 P1：风险情报工具（计量计费，轻量聚合）==================
# 用户指令：P1 主打「高价值风控工具 + 轻量聚合」，不做通用 RPC 转发。
# 数据源一律是公开聚合接口（GoPlus Labs + TronGrid），只读、不接触私钥、不托管资金。
# 计费：每个 IP 每天 5 次免费；超出后走 x402（USDC）。2026-10-16 起不再发放 API key。
RISK_TOOLS = ("tron_check_address_risk", "tron_verify_usdt_transfer")
RISK_FREE_PER_DAY = 5
RISK_DB = os.environ.get("RB_RISK_DB", "./risk_quota.db")
GOPLUS_BASE = os.environ.get("RB_GOPLUS_BASE", "https://api.gopluslabs.io/api/v1")
TRONGRID_BASE = os.environ.get("RB_TRONGRID_BASE", "https://api.trongrid.io")
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_DECIMALS = 6
TRON_FINALITY_BLOCKS = 19          # 波场 SR 共识的不可逆深度（约 57 秒）
# 2026-10-16：/billing/pricing 的 API-key 商店已整体退役（不再卖 key、不再按月订阅），
# 付费只走 x402。充值指引统一指向首页定价段。
TOPUP_URL = "https://robotbase.cc/#pricing"

# ---- 2026-10-15 修复：账本一致性 / 重试安全 --------------------------------
# 外部实测发现：客户端拿不到响应（链路抖动、工具耗时 24s 超过客户端超时重试），
# 但额度已经扣掉。5 次调用里出现 2 次——用户既没拿到数据、又少了 1 次免费额度。
#  ① 本地格式校验移到门禁之前：地址写错应该提示「地址写错」，而不是先吃 402；
#  ② 同一次查询在 RISK_DEDUP_TTL 秒内重复提交不再扣费，直接回同一份结论；
#  ③ 每条 _metering 都带 charged_this_call，用户能确认这一次到底扣没扣。
RISK_DEDUP_TTL = 300
RISK_DEDUP_MAX = 256
_risk_dedup, _risk_dedup_lock = {}, threading.Lock()
# 付费调用的幂等窗口：同一个 payer 对同一工具、同一参数在窗口内重复提交，
# 直接回缓存，不再扣信用。客户端超时重试不该被收两次钱。
PAID_DEDUP_TTL = 300
PAID_DEDUP_MAX = 512
_paid_dedup, _paid_dedup_lock = {}, threading.Lock()

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
    # 历史遗留：P2 时代按 API key 计数（/billing/v1/usage 的用量看板数据源）。
    # 2026-10-16 起不再发放任何 API key，这张表不再写入，保留只为免去做破坏性迁移。
    con.execute("""CREATE TABLE IF NOT EXISTS key_usage(
        day TEXT NOT NULL, key_hash TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(day, key_hash))""")
    con.commit()
    con.close()


RISK_FREE_GLOBAL_PER_DAY = int(os.environ.get("RB_RISK_FREE_GLOBAL", "1000"))


def _quota_subject():
    """免费额度的计数主体：IPv4 按单个地址，IPv6 按 /64（一台机器通常拿到整段 /64）。"""
    ip = _client_ip().split(",")[0].strip()
    try:
        a = ipaddress.ip_address(ip)
        if a.version == 6:
            if a.ipv4_mapped:
                return str(a.ipv4_mapped)
            return str(ipaddress.ip_network(ip + "/64", strict=False))
        return str(a)
    except ValueError:
        return ip


def _risk_quota(consume=True):
    """返回 (allowed, used, limit)：按 IP 的每日免费额度。

    2026-10-16：不再有 API key 档，免费额度只按 IP 记；超出后由 x402 信用余额接管
    （见 call_tool 的 x402 分支），所以这里不会再出现 limit<0。
    """
    h = hashlib.sha256(_quota_subject().encode()).hexdigest()[:16]
    d = _bj_day()
    con = _db(RISK_DB)
    try:
        row = con.execute("SELECT used FROM risk_quota WHERE day=? AND ip_hash=?", (d, h)).fetchone()
        used = row["used"] if row else 0
        g = con.execute("SELECT used FROM risk_quota WHERE day=? AND ip_hash='__global__'", (d,)).fetchone()
        gused = g["used"] if g else 0
        # 全站每日免费总量封顶：轮换 IP 刷免费额度时，总量也有上限
        if gused >= RISK_FREE_GLOBAL_PER_DAY:
            return False, max(used, RISK_FREE_PER_DAY), RISK_FREE_PER_DAY
        if not consume:
            return used < RISK_FREE_PER_DAY, used, RISK_FREE_PER_DAY
        if used >= RISK_FREE_PER_DAY:
            return False, used, RISK_FREE_PER_DAY
        con.execute("INSERT INTO risk_quota(day, ip_hash, used) VALUES(?,?,1) "
                    "ON CONFLICT(day, ip_hash) DO UPDATE SET used=used+1", (d, h))
        con.execute("INSERT INTO risk_quota(day, ip_hash, used) VALUES(?,'__global__',1) "
                    "ON CONFLICT(day, ip_hash) DO UPDATE SET used=used+1", (d,))
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
            "Payment Required. The free allowance for RobotBase risk tools is %d calls/day per IP and "
            "is used up for today (Beijing time, resets 00:00 Asia/Shanghai). Pay in USDC with x402: "
            "read accepts[] from structuredContent, sign one EIP-3009 authorization, and retry this same "
            "call with it in params._meta[\"x402/payment\"]. There is no API key - after the first "
            "settlement, keep sending that same payment payload with each call and it will not be "
            "settled again; each call just spends one credit." % (limit,))
    return _meter_payload(used, limit, pending=True)


def _meter_payload(used, limit, pending=False, charged=None):
    if limit < 0:
        # limit < 0 = 这次调用走的是 x402 付费额度（2026-10-16 起不再有 API key 档）
        return {"x402": {"paid": True, "metered_by": "x402 credit balance"}}
    payload = {"free_tier": {"used_today": used, "limit_per_day": limit,
                             "resets": "00:00 Asia/Shanghai",
                             "more_calls_via": "x402 USDC payment - there is no API key"}}
    if pending:
        payload["free_tier"]["remaining_today"] = max(0, limit - used)
    if charged is not None:
        payload["free_tier"]["charged_this_call"] = bool(charged)
    return payload


def _risk_precheck(name, args):
    """风险工具的本地格式校验（与各工具内部正则保持同一套）。

    None = 参数看起来合法；dict = 本地校验失败：不进门禁、不扣额度。
    """
    a = args or {}
    if name == "tron_check_address_risk":
        v = (a.get("address") or "").strip()
        if _tron_addr_normalize(v) is None:
            return {"address": v, "chain": "TRON", "checked": False,
                    "error": "not a valid TRON address (base58 checksum failed; expected T + 33 chars "
                             "or 41-hex)"}
    elif name == "tron_verify_usdt_transfer":
        v = (a.get("txid") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", v):
            return {"txid": v, "chain": "TRON", "verified": False,
                    "error": "not a TRON transaction id (expected 64 hex chars)"}
        et = (a.get("expected_to") or "").strip()
        if et and _tron_addr_normalize(et) is None:
            return {"txid": v, "chain": "TRON", "verified": False,
                    "error": "expected_to is not a valid TRON address (base58 checksum failed)"}
    return None


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
    norm = _tron_addr_normalize(addr)
    if norm is None:
        return {"address": addr, "chain": "TRON",
                "error": "not a valid TRON address (base58 checksum failed); no score was produced",
                "checked": False, "safety_score": None, "_no_charge": True}
    addr = norm
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
            if not account.get("is_contract"):
                h = _tron_hex(addr)
                if h:
                    try:
                        _ctr = _tron("wallet/getcontract", {"value": h}) or {}
                        if _ctr.get("bytecode") or _ctr.get("code_hash") or _ctr.get("contract_address"):
                            account["is_contract"] = True
                            account["has_code"] = True
                    except Exception:  # noqa: BLE001
                        pass
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
                    "scoring": "not scored: the account is not activated, so there is no history to judge",
                    "not_covered": ["no transaction history for an unactivated account",
                                    "counterparty graph depth (who paid whom) is not scored in this call"],
                    "disclaimer": ("Labels come from the public GoPlus Labs dataset as fetched at call "
                                   "time; RobotBase relays them verbatim, forms no legal opinion, and this "
                                   "is not compliance advice."),
                    "note": "The address is not activated on TRON, so there is no transaction history "
                            "to judge. Not activated is not the same as clean."})
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


def _tron_usdt_parse_local(tx):
    """用**我们自己的节点**解析一笔 USDT 交易。本地没有这笔则返回 None。

    取数全部来自自有 java-tron；from/to/金额优先取 receipt 日志里的 Transfer 事件
    （那是链上真正发生了什么），calldata 只用来判 call_kind（transfer / approve / …）。
    """
    raw = _tron("wallet/gettransactionbyid", {"value": tx}) or {}
    info = _tron("wallet/gettransactioninfobyid", {"value": tx}) or {}
    if not raw and not info:
        return None
    out = {"txid": tx, "chain": "TRON", "network": "mainnet"}
    contract = ((raw.get("raw_data") or {}).get("contract") or [{}])[0]
    ctype = contract.get("type")
    value = (contract.get("parameter") or {}).get("value") or {}
    out.update({"found": True, "contract_type": ctype,
                "status": ((raw.get("ret") or [{}])[0].get("contractRet")
                           or (info.get("receipt") or {}).get("result") or "UNKNOWN")})
    from_addr, to_addr, amount_raw, is_usdt = None, None, None, False
    try:
        from_addr = _tron_b58("41" + (value.get("owner_address") or "").rjust(40, "0")[-40:])
    except Exception:  # noqa: BLE001
        pass
    if ctype == "TriggerSmartContract":
        token = value.get("contract_address") or ""
        data = value.get("data") or ""
        is_usdt = (token[-40:].lower() == TRON_USDT_HEX[2:].lower())
        method = data[:8]
        if method == "a9059cbb" and len(data) >= 136:
            out["call_kind"] = "transfer"
            try:
                to_addr = _tron_b58("41" + data[32:72])
                amount_raw = int(data[72:136], 16)
            except Exception:  # noqa: BLE001
                pass
        elif method == "23b872dd" and len(data) >= 200:
            out["call_kind"] = "transfer_from"
            try:
                to_addr = _tron_b58("41" + data[96:136])
                amount_raw = int(data[136:200], 16)
            except Exception:  # noqa: BLE001
                pass
        elif method == "095ea7b3" and len(data) >= 136:
            # approve 只授权额度、不转移任何 USDT，绝不能判成到账（2026-10-15 修复）
            out["call_kind"] = "approve"
            try:
                out["spender"] = _tron_b58("41" + data[32:72])
                out["allowance_usdt"] = round(int(data[72:136], 16) / (10 ** USDT_DECIMALS), 6)
            except Exception:  # noqa: BLE001
                pass
        elif method == "39509351" and len(data) >= 136:
            out["call_kind"] = "increase_allowance"
        elif method:
            out["call_kind"] = "other_contract_call"
        else:
            out["call_kind"] = "unknown"
        out["token_contract"] = USDT_TRC20_CONTRACT if is_usdt else (token and _safe_b58(token))
        out["method"] = method
    elif ctype == "TransferContract":
        out["call_kind"] = "native_trx_transfer"
        out["note"] = "this is a native TRX transfer, not a TRC-20 USDT transfer"
        try:
            to_addr = _tron_b58("41" + (value.get("to_address") or "").rjust(40, "0")[-40:])
            amount_raw = value.get("amount")
        except Exception:  # noqa: BLE001
            pass
    else:
        out["call_kind"] = "unknown"
    # receipt 日志才是「真正发生了什么」：有 Transfer 事件就以它为准
    logs = _tron_decode_trc20_logs(info)
    if logs:
        lg = logs[0]
        to_addr = lg.get("to")
        if not from_addr:
            from_addr = lg.get("from")
        if lg.get("amount_atomic") is not None:
            amount_raw = lg.get("amount_atomic")
        out["moved_source"] = "receipt_transfer_log"
    elif amount_raw is not None:
        out["moved_source"] = "calldata_intent"
    block = info.get("blockNumber")
    if block:
        out["block_number"] = block
        out["block_time_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime((info.get("blockTimeStamp") or 0) / 1000))
    out["fee_trx"] = round((info.get("fee") or 0) / 1e6, 6) if info.get("fee") else None
    receipt = (info.get("receipt") or {}).get("result")
    if receipt:
        out["receipt_result"] = receipt
    try:
        solidified = _http_json(TRON_SOLIDITY + "/walletsolidity/gettransactionbyid",
                                {"value": tx}, timeout=10)
        out["finalized"] = bool(solidified and solidified.get("txID"))
    except Exception:  # noqa: BLE001
        out["finalized"] = False
    if not out["finalized"] and block:
        tip = ((_tron_tip() or {}).get("block_header") or {}).get("raw_data", {}).get("number")
        if tip:
            conf = int(tip) - int(block) + 1
            out["confirmations"] = conf
            out["blocks_until_finality"] = max(0, TRON_FINALITY_BLOCKS - conf)
    if amount_raw is not None and is_usdt:
        out["amount_usdt"] = round(amount_raw / (10 ** USDT_DECIMALS), 6)
    if from_addr:
        out["from"] = from_addr
    if to_addr:
        out["to"] = to_addr
    return out


def _tron_usdt_parse_trongrid(tx):
    """回落到 TronGrid 解析（窗口外的交易只能走这条路）。返回 None 表示查不到。"""
    try:
        raw = _http_json("%s/wallet/gettransactionbyid" % TRONGRID_BASE, {"value": tx}, timeout=15)
    except Exception as exc:  # noqa: BLE001
        return {"_error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
    if not raw or not raw.get("txID"):
        return None
    out = {"txid": tx, "chain": "TRON", "network": "mainnet"}
    contract = ((raw.get("raw_data") or {}).get("contract") or [{}])[0]
    ctype = contract.get("type")
    value = (contract.get("parameter") or {}).get("value") or {}
    out.update({"found": True, "contract_type": ctype,
                "status": ((raw.get("ret") or [{}])[0].get("contractRet") or "UNKNOWN")})
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
        if method == "a9059cbb" and len(data) >= 136:
            out["call_kind"] = "transfer"
            try:
                to_addr = _tron_hex_to_address(data[32:72])
                amount_raw = int(data[72:136], 16)
            except Exception:  # noqa: BLE001
                pass
        elif method == "23b872dd" and len(data) >= 200:
            out["call_kind"] = "transfer_from"
            try:
                to_addr = _tron_hex_to_address(data[96:136])
                amount_raw = int(data[136:200], 16)
            except Exception:  # noqa: BLE001
                pass
        elif method == "095ea7b3" and len(data) >= 136:
            out["call_kind"] = "approve"
            try:
                out["spender"] = _tron_hex_to_address(data[32:72])
                out["allowance_usdt"] = round(int(data[72:136], 16) / (10 ** USDT_DECIMALS), 6)
            except Exception:  # noqa: BLE001
                pass
        elif method == "39509351" and len(data) >= 136:
            out["call_kind"] = "increase_allowance"
        elif method:
            out["call_kind"] = "other_contract_call"
        else:
            out["call_kind"] = "unknown"
        out["token_contract"] = USDT_TRC20_CONTRACT if is_usdt else (token and _safe_b58(token))
        out["method"] = method
    elif ctype == "TransferContract":
        out["call_kind"] = "native_trx_transfer"
        out["note"] = "this is a native TRX transfer, not a TRC-20 USDT transfer"
        try:
            to_addr = _tron_hex_to_address(value.get("to_address", ""))
            amount_raw = value.get("amount")
        except Exception:  # noqa: BLE001
            pass
    else:
        out["call_kind"] = "unknown"
    info = {}
    try:
        info = _http_json("%s/wallet/gettransactioninfobyid" % TRONGRID_BASE, {"value": tx}, timeout=15) or {}
    except Exception:  # noqa: BLE001
        pass
    block = info.get("blockNumber")
    if block:
        out["block_number"] = block
        out["block_time_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime((info.get("blockTimeStamp") or 0) / 1000))
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
    return out


def _tron_usdt_verdict(out, expected_to, expected_amount_usdt):
    """判定逻辑（与改造前逐字一致）：必须真的解析出收款方与金额才算一笔 USDT 转账。"""
    status = out.get("status")
    is_usdt = bool(out.get("token_contract") == USDT_TRC20_CONTRACT)
    to_addr = out.get("to")
    amount_raw = out.get("amount_usdt")
    finalized = bool(out.get("finalized"))
    is_transfer_call = bool(amount_raw is not None and to_addr)
    checks = {"status_success": status == "SUCCESS",
              "is_usdt_trc20": is_usdt,
              "is_transfer_call": is_transfer_call,
              "finalized": finalized}
    if expected_to:
        checks["recipient_matches"] = (expected_to.strip() == (to_addr or ""))
    if expected_amount_usdt is not None:
        try:
            checks["amount_matches"] = abs(float(expected_amount_usdt) - (amount_raw or 0)) < 1e-6
        except (TypeError, ValueError):
            checks["amount_matches"] = False
    # 2026-10-19 (ported QA P2-16): finality must not flip `verified` to false - the verdict
    # below already says "confirmed but not yet finalized". Optional checks count only when asked.
    required = ["status_success", "is_usdt_trc20", "is_transfer_call"]
    if expected_to:
        required.append("recipient_matches")
    if expected_amount_usdt is not None:
        required.append("amount_matches")
    out["checks"] = checks
    out["verified"] = all(checks[k] for k in required)
    if out["verified"] and finalized:
        out["verdict"] = "USDT arrived and is irreversible"
    elif out["verified"]:
        out["verdict"] = "confirmed but not yet finalized (wait for 19 blocks)"
    elif is_usdt and not is_transfer_call:
        out["not_a_transfer"] = True
        out["verdict"] = ("not verified: this transaction does not transfer USDT - it is a '%s' call on the "
                          "USDT contract, so no USDT moved. Do not release value on this txid."
                          % out.get("call_kind"))
    else:
        out["verdict"] = "not verified - see checks"
    return out


def t_tron_verify_usdt_transfer(txid, expected_to=None, expected_amount_usdt=None):
    """TRC-20 USDT 到账核验：终局性、金额、收付款方，一次给全。

    2026-10-16：改为**本地优先**。窗口内（约 6 天）走我们自己的 java-tron（毫秒级、不依赖第三方）；
    本地查不到时**回落 TronGrid**——本地的空结果只代表"超出保留窗口"，绝不等于"没到账"。
    """
    tx = (txid or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", tx):
        return {"txid": txid, "chain": "TRON", "verified": False,
                "error": "not a TRON transaction id (expected 64 hex chars)", "_no_charge": True}
    out = None
    try:
        out = _tron_usdt_parse_local(tx)
    except Exception:  # noqa: BLE001
        out = None
    if out is not None:
        out["source"] = "our own java-tron full node"
        out["retention_window"] = _tron_window() or None
    else:
        tg = _tron_usdt_parse_trongrid(tx)
        if tg is None:
            return {"txid": tx, "chain": "TRON", "found": False, "verified": False,
                    "reason": ("not found on our own node (outside its ~6 day retention window) nor on "
                               "TronGrid - the transaction id does not exist on the chain"),
                    "source": "our java-tron (miss) + TronGrid (miss)"}
        if tg.get("_error"):
            return {"txid": tx, "chain": "TRON", "verified": False, "error": tg["_error"],
                    "_no_charge": True}
        out = tg
        out["source"] = "TronGrid (transaction is older than our own node's retention window)"
    return _tron_usdt_verdict(out, expected_to, expected_amount_usdt)


def _safe_b58(h):
    try:
        return _tron_hex_to_address(h)
    except Exception:  # noqa: BLE001
        return h


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
                                                 "enum": ["btc", "eth", "xmr", "zec", "tron"],
                                                 "description": "Chain id: btc / eth / xmr / zec / tron"}},
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
     "Do not use: for fees call xmr_get_fee_rates; for one transaction call xmr_get_transaction; for "
     "network-wide numbers call xmr_get_network_stats. RobotBase does not index Monero wallets or ring signatures. "
     "Returns: height, target_height, synced, status, network, difficulty, txpool_txs, database_size_gb, "
     "block_size_limit, version, incoming_connections / outgoing_connections.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_xmr_node_status()),
    ("xmr_get_fee_rates",
     "Monero fee estimate per kB, split into ascending priority tiers, read from our own monerod. "
     "When to use: pricing an XMR transaction, or deciding whether to pay the baseline fee to land in the next block. "
     "Do not use: for Bitcoin fees (btc_get_fee_rates) or for mempool depth (xmr_get_mempool_state). "
     "Returns: baseline_fee_per_kb_xmr plus priority_tiers[] each with {priority, fee_per_kb_atomic, fee_per_kb_xmr}. "
     "Monero has no EIP-1559 style fee market: blocks are never full, so the baseline tier normally confirms next block.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_xmr_fee_rates()),
    ("xmr_get_mempool_state",
     "Monero mempool: pending transaction count, total bytes and fees, age of the oldest pending tx, double-spend/failing counts. "
     "When to use: is the network quiet or piling up, and roughly how long a new transaction waits. "
     "Do not use: for fees (xmr_get_fee_rates) or for a specific payment (xmr_get_transaction). "
     "Returns: pending_txs, bytes_total, fee_total_xmr, oldest_pending_age_s, double_spends, failing, "
     "tx_size_median_bytes, congestion verdict.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_xmr_mempool_state()),
    ("xmr_get_block",
     "One Monero block header, by height or by block hash. "
     "When to use: confirming which block a transaction landed in, or inspecting reward, difficulty and weight of a block. "
     "Do not use: to list recent blocks (xmr_get_recent_blocks) or to look up a transaction (xmr_get_transaction). "
     "Returns: height, hash, prev_hash, timestamp, age_s, tx_count, reward_xmr, difficulty, block_weight, "
     "block_size_bytes, major_version / minor_version, orphan.",
     {"type": "object", "properties": {"height": {"type": "integer", "description": "Block height"},
                                       "block_hash": {"type": "string", "description": "64 hex chars; takes precedence over height"}},
      "additionalProperties": False}, lambda a: t_xmr_block(a.get("height"), a.get("block_hash"))),
    ("xmr_get_recent_blocks",
     "The most recent Monero blocks with the observed block interval. "
     "When to use: checking that XMR is producing blocks normally and at roughly the 120s target. "
     "Do not use: for one specific block (xmr_get_block). "
     "Returns: tip_height, avg_block_interval_s, target_interval_s, blocks[] {height, hash, timestamp, tx_count, reward_xmr, difficulty, weight}.",
     {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50,
                                                 "description": "How many blocks (default 10, max 50)"}},
      "additionalProperties": False}, lambda a: t_xmr_recent_blocks(a.get("limit"))),
    ("xmr_get_transaction",
     "Look up one Monero transaction by txid: is it mined, in which block, how many confirmations, "
     "its size, output count and ring size. "
     "When to use: did this XMR transfer get into a block, and how deep is it now. "
     "Do not use: to learn the amount or the sender/recipient - Monero hides those by design and no "
     "third party can recover them; only the recipient (or a holder of the tx key) can prove a payment. "
     "Returns: found, confirmed, in_mempool, block_height, block_timestamp, confirmations, double_spend_seen, "
     "size_bytes, inputs, outputs, ring_size_per_input, tx_version, unlock_time, and explicitly the list of "
     "fields not disclosed by the protocol.",
     {"type": "object", "properties": {"txid": {"type": "string", "description": "64 hex char Monero transaction id"}},
      "required": ["txid"], "additionalProperties": False}, lambda a: t_xmr_transaction(a.get("txid"))),
    ("xmr_get_network_stats",
     "Monero network-wide state: network hashrate, difficulty, emission and fees over the last 720 blocks, "
     "protocol version and the next hard fork. "
     "When to use: macro questions about the Monero network - how much hashrate secures it, what is being "
     "issued, and which protocol version is active. "
     "Do not use: for our node's own health (xmr_get_node_status). "
     "Returns: height, network_hashrate_hs, difficulty, block_size/weight median and limit, "
     "emission_last_720_blocks_xmr, fees_last_720_blocks_xmr, hard_fork_version, next_fork_earliest_height, client_version.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_xmr_network_stats()),
    ("xmr_verify_payment",
     "Verify one Monero payment cryptographically, using proof material only the recipient can provide. "
     "Give it a txid plus either the transaction key (from your own wallet: get_tx_key <txid>) or a "
     "transaction proof signature (get_tx_proof), and it returns whether that transaction really pays "
     "the given address, for how much, and how many confirmations. "
     "When to use: a merchant or agent needs to confirm an XMR payment arrived before releasing value, "
     "without trusting a block explorer - and for Monero there is no other way, because amounts and both "
     "counterparties are hidden by the protocol. "
     "Do not use: to discover payments you cannot already prove (RobotBase does not index Monero wallets, "
     "and no third party can enumerate your incoming payments), or for TRC-20 USDT settlement "
     "(that is tron_verify_usdt_transfer). "
     "Returns: verified, confirmations, received_xmr, received_atomic, in_pool. Neither the tx key nor the "
     "proof can be used to spend funds or to scan other payments.",
     {"type": "object", "properties": {
        "txid": {"type": "string", "description": "64 hex char Monero transaction id"},
        "address": {"type": "string", "description": "The Monero address that should have received the payment"},
        "tx_key": {"type": "string", "description": "Transaction key from your own wallet (get_tx_key)"},
        "signature": {"type": "string", "description": "Transaction proof signature (get_tx_proof) instead of tx_key"},
        "message": {"type": "string", "description": "Optional message the proof was signed over"}},
      "required": ["txid", "address"], "additionalProperties": False},
     lambda a: t_xmr_verify_payment(a.get("txid"), a.get("address"), a.get("tx_key"),
                                    a.get("signature"), a.get("message"))),
    ("btc_get_network_stats",
     "Bitcoin network and node state: difficulty, network hashrate, total transactions processed and the "
     "current tx/s rate, protocol version, peer connections, relay fee, mempool size, uptime and which "
     "indexes (txindex) are synced on our node. "
     "When to use: macro questions about Bitcoin - how much hashrate secures it, how fast the chain is moving, "
     "or whether our node's indexes are complete. "
     "Do not use: for fees (btc_get_fee_rates) or mempool depth (btc_get_mempool_state). "
     "Returns: difficulty, network_hashrate_hs, chain_tx_count_total, tx_rate_per_s, client, connections, "
     "relay_fee_btc_kvb, uptime_s, indexes{}.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_btc_get_network_stats()),
    ("eth_get_block",
     "One Ethereum block by number, tag (latest/finalized/safe) or hash: timestamp, transaction count, gas "
     "used against the limit, base fee, block builder and size. "
     "When to use: block-level context - how full is Ethereum right now, when a block was produced, what the "
     "base fee was, or which builder produced it. "
     "Do not use: for a transaction (eth_get_transaction) or for fees across blocks (eth_get_fee_rates). "
     "Returns: found, number, hash, parent_hash, timestamp, age_s, tx_count, gas_used, gas_limit, gas_used_pct, "
     "base_fee_gwei, miner, size_bytes. "
     "Boundary: block bodies are retained from 15,500,000 (2022-09-14) only; older heights return a pruned-history "
     "error, not an empty block.",
     {"type": "object", "properties": {
        "height": {"description": "Block number (integer or hex), or a tag like latest / finalized / safe"},
        "block_hash": {"type": "string", "description": "0x + 64 hex block hash; takes precedence"}},
      "additionalProperties": False}, lambda a: t_eth_get_block(a.get("height"), a.get("block_hash"))),
    ("eth_get_logs",
     "Ethereum event logs over a block range, optionally filtered by contract address and event signature, "
     "with ERC-20 Transfer logs decoded (token, from, to, amount). "
     "When to use: find transfers or contract events in a window - 'who moved this token in the last N "
     "blocks', or watch a contract's activity without an indexer. "
     "Do not use: for a single transaction (eth_get_transaction) or to scan huge ranges (max 1000 blocks per "
     "call, results truncated with a flag). "
     "Returns: filter, matched, returned, truncated, logs[] with block_number, tx_hash, address, topics, data "
     "and a decoded block for ERC-20 Transfers. "
     "Boundary: logs are read from receipts, which exist only for a rolling window (~6 days today, 1.4-8.3 days as "
     "the 50k-block static-file shard advances); older ranges are refused.",
     {"type": "object", "properties": {
        "from_block": {"description": "Integer or hex or tag; defaults to 50 blocks back"},
        "to_block": {"description": "Integer or hex or tag; defaults to latest"},
        "address": {"type": "string", "description": "Optional contract address filter"},
        "topic0": {"type": "string", "description": "Optional event signature hash filter"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "Max logs to return (default 50)"}},
      "additionalProperties": False},
     lambda a: t_eth_get_logs(a.get("from_block"), a.get("to_block"), a.get("address"),
                              a.get("topic0"), a.get("limit"))),
    ("xmr_get_output_distribution",
     "Monero output distribution: how many spendable outputs fall into each ring-size bucket, read from our "
     "own monerod. This is the anonymity-set side of Monero that free explorers rarely expose. "
     "When to use: reasoning about Monero privacy - how large the spendable-output set is at a given height. "
     "Do not use: to guess a 'privacy score' (we return raw counts only). "
     "Boundary: monerod's distribution data lags the chain tip by a node-dependent number of blocks "
     "(~102 today, historically ~5,000); the default probes backwards until it finds a range with data, and "
     "the response states the real tip, the queried range and the lag. "
     "Returns: tip_height, queried_range, data_lags_tip_by_blocks, start_height, outputs_before_start, "
     "distribution[] with the raw per-bucket counts.",
     {"type": "object", "properties": {
        "from_height": {"type": "integer", "description": "Start height; defaults to a probed 10-block range (~tip-102 today)"},
        "to_height": {"type": "integer", "description": "End height; defaults to the newest height that still has distribution data"}},
      "additionalProperties": False},
     lambda a: t_xmr_get_output_distribution(a.get("from_height"), a.get("to_height"))),
    ("zec_get_treestate",
     "Zcash shielded commitment-tree state: the Sapling, Orchard and Ironwood anchor roots at a given block. "
     "When to use: shielded-pool depth - how the shielded sets advance over time, or the anchor a wallet or "
     "prover needs to recognise. "
     "Do not use: for pool supplies (zec_get_pool_status) or a transaction (zec_get_transaction). "
     "Returns: block_hash, height, time, pools{sapling, orchard, ironwood}{final_root, final_state}.",
     {"type": "object", "properties": {"block_hash": {"type": "string", "description": "64 hex block hash"},
                                       "height": {"type": "integer", "description": "Block height instead of a hash"}},
      "additionalProperties": False}, lambda a: t_zec_get_treestate(a.get("block_hash"), a.get("height"))),
    ("zec_get_network_stats",
     "Zcash network and node state: mempool size and bytes, node version, difficulty, network hashrate, chain "
     "progress and disk, block subsidy, and the funding streams (including Zcash Community Grants). "
     "When to use: macro questions about Zcash - how much hashrate secures it, whether our node is caught up, "
     "what the block subsidy and funding allocations currently are. "
     "Do not use: for shielded-pool supplies (zec_get_pool_status) or our node's own sync state "
     "(zec_get_pool_status / chain_status). "
     "Returns: mempool, network, difficulty, network_hashrate_hs, blockchain, height, verification_progress, "
     "size_on_disk_gb, block_reward_zec, funding_streams[], lockbox_streams[].",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_zec_get_network_stats()),
    ("tron_list_proposals",
     "Tron governance proposals: id, proposer, state, approvals, expiry and the parameters they would change. "
     "When to use: governance or staking questions, or checking whether a chain-parameter change is pending. "
     "Do not use: for the current parameter values (tron_get_chain_economics) or the witness list "
     "(tron_list_witnesses). "
     "Returns: total_proposals, by_state{}, top_by_approvals[] with proposal_id, proposer, approvals, state, "
     "parameters{}.",
     {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100,
        "description": "How many top proposals to return (default 20)"}},
      "additionalProperties": False}, lambda a: t_tron_list_proposals(a.get("limit"))),
    ("eth_get_transaction",
     "Ethereum transaction check by hash: success or reverted, gas used, fee paid, confirmations, whether the "
     "block is finalized, and any ERC-20 transfers decoded from the receipt logs (token, from, to, amount). "
     "When to use: did this ETH or ERC-20 payment actually settle, is it irreversible, who paid whom and how much. "
     "Do not use: for fee estimation (eth_get_fee_rates) or for Tron (tron_get_transaction). "
     "Returns: found, status, block_number, from, to, value_eth, gas_used, fee_eth, confirmations, finalized, "
     "erc20_transfers[]. A miss is reported as absent from *our* node only - never as proof of non-existence. "
     "Boundary: the transaction-hash index starts at block 26,170,371 (2026-10-12, only widens) while receipts cover "
     "a rolling ~6 days, so a miss on an older transaction means outside our window, not non-existent.",
     {"type": "object", "properties": {"txid": {"type": "string", "description": "0x + 64 hex transaction hash"}},
      "required": ["txid"], "additionalProperties": False}, lambda a: t_eth_transaction(a.get("txid"))),
    ("eth_get_fee_rates",
     "Ethereum fee market from our own Reth node: current and next base fee, priority-fee percentiles over the "
     "last blocks, and the estimated ETH cost of a plain transfer and of an ERC-20 transfer. "
     "When to use: pricing an Ethereum transaction, or deciding what tip gets included quickly. "
     "Do not use: for Bitcoin (btc_get_fee_rates), Monero (xmr_get_fee_rates) or Tron (tron_get_chain_economics). "
     "Returns: base_fee_gwei, next_base_fee_gwei, base_fee_last_blocks_gwei, priority_percentiles_gwei, "
     "estimated_cost{simple_eth_transfer_21000_gas_eth, erc20_transfer_65000_gas_eth}.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_eth_fee_rates()),
    ("eth_get_account",
     "Ethereum account state from our own node: ETH balance, nonce, and whether the address is a contract. "
     "When to use: balance and account type before sizing a transfer, or telling an EOA from a contract. "
     "Do not use: for ERC-20 token balances (eth_read_contract with balanceOf) or address risk scoring. "
     "Returns: address, balance_eth, nonce, is_contract, code_size_bytes.",
     {"type": "object", "properties": {"address": {"type": "string", "description": "0x + 40 hex address"}},
      "required": ["address"], "additionalProperties": False}, lambda a: t_eth_account(a.get("address"))),
    ("eth_read_contract",
     "Read-only (eth_call) against any Ethereum contract, executed by our own Reth node. ERC-20 helpers "
     "balanceOf / allowance / decimals / symbol / name / totalSupply are built in; any other function can be "
     "called by passing its 4-byte `selector` plus ABI-encoded `parameter`. Nothing is signed or written. "
     "When to use: an agent needs an on-chain value without trusting a third-party API - a USDT/USDC balance, "
     "token decimals, an allowance, or any view function. "
     "Do not use: to send a transaction (this never writes), or for a historical balance. "
     "Returns: ok, contract, function, selector, output_type, value, value_human for known-decimals tokens.",
     {"type": "object", "properties": {
        "contract": {"type": "string", "description": "0x + 40 hex contract address"},
        "function": {"type": "string", "description": "balanceOf | allowance | decimals | symbol | name | totalSupply, or a label for a raw selector"},
        "argument": {"type": "string", "description": "Ethereum address argument (balanceOf / allowance)"},
        "argument2": {"type": "string", "description": "Second address argument (allowance)"},
        "selector": {"type": "string", "description": "Explicit 4-byte selector hex for non-ERC-20 calls"},
        "parameter": {"type": "string", "description": "ABI-encoded parameter hex (with an explicit selector)"},
        "output_type": {"type": "string", "enum": ["uint256", "address", "string", "bool"]}},
      "required": ["contract", "function"], "additionalProperties": False},
     lambda a: t_eth_read_contract(a.get("contract"), a.get("function"), a.get("argument"),
                                   a.get("argument2"), a.get("selector"), a.get("parameter"),
                                   a.get("output_type"))),
    ("zec_get_transaction",
     "Zcash transaction check by txid from our own Zebra node: confirmations, block height and time, whether it "
     "is in the active chain, size, and how much of it is transparent versus shielded. "
     "When to use: did this ZEC payment land, how deep is it, and does it involve shielded inputs/outputs. "
     "Do not use: to learn shielded amounts or parties - shielded values are hidden by the protocol and only a "
     "viewing key held by a party to the transaction can read them; this tool reports the shielded *shape* "
     "(spend/output/action counts) and says so explicitly rather than guessing. "
     "Returns: found, confirmations, block_height, block_hash, block_time, in_active_chain, size_bytes, "
     "transparent{inputs, outputs, total_output_zec}, shielded{sapling_spends, sapling_outputs, orchard_actions}, "
     "has_shielded_component, not_disclosed_by_protocol[].",
     {"type": "object", "properties": {"txid": {"type": "string", "description": "64 hex char Zcash transaction id"}},
      "required": ["txid"], "additionalProperties": False}, lambda a: t_zec_transaction(a.get("txid"))),
    ("tron_get_node_status",
     "TRON node state from our own java-tron full node: tip height, finalized (solidity) height and how far "
     "solidity lags, peers, client version, transactions in the tip block. "
     "When to use: is the TRON node we operate synced and healthy, and how far behind is the finalized state. "
     "Do not use: for historical blocks or transactions - this is a lite full node and those APIs are closed. "
     "Returns: height, solidity_height, solidity_lag_blocks, age_s, tx_in_tip_block, peer_count, client_version.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_tron_node_status()),
    ("tron_get_block",
     "One Tron block by height or block id, read from our own node: producer, timestamp, transaction count "
     "split by contract type, and the tx ids in it. "
     "When to use: inspect a specific block, or confirm which block a transaction landed in. "
     "Do not use: for the tip only (tron_get_current_block) or for a transaction itself (tron_get_transaction). "
     "Boundary: our Tron node is a lite full node that keeps only a recent window (~6 days). An empty result "
     "means 'outside our window', NOT 'the block does not exist'. "
     "Returns: found, number, block_id, parent_hash, timestamp_ms, tx_count, tx_by_type, producer_address, "
     "tx_ids, retention_window.",
     {"type": "object", "properties": {"height": {"type": "integer", "description": "Block height"},
        "block_id": {"type": "string", "description": "Block id (64 hex chars); takes precedence over height"}},
      "additionalProperties": False}, lambda a: t_tron_block(a.get("height"), a.get("block_id"))),
    ("tron_get_transaction",
     "One Tron transaction by txid: success or reverted, energy used, the call it made, and - this is the "
     "important part - any TRC-20 transfers decoded from the receipt logs (token, from, to, amount). "
     "When to use: did this USDT payment really land, for how much, from whom, and is it finalized. "
     "Do not use: for address risk scoring (tron_check_address_risk) or for blocks (tron_get_block). "
     "Boundary: our node is a lite full node with a recent window (~6 days); an empty result means 'outside "
     "our window', NOT 'the payment did not arrive' - fall back to an indexer before saying a payment is missing. "
     "Returns: found, block_number, status, contract_type, owner/to/contract_address, amount_trx, "
     "trc20_transfers[], energy_used, confirmations, finalized, retention_window.",
     {"type": "object", "properties": {"txid": {"type": "string", "description": "64 hex char Tron transaction id"}},
      "required": ["txid"], "additionalProperties": False}, lambda a: t_tron_transaction(a.get("txid"))),
    ("tron_get_current_block",
     "The current (tip) TRON block: number, id, parent, timestamp, transaction count and a breakdown by "
     "contract type, plus the producing witness. "
     "When to use: how busy TRON is right now, what is in the block being produced, who produced it. "
     "Do not use: to fetch an older block - a lite full node serves the tip only, historical blocks are closed. "
     "Returns: number, block_id, parent_hash, timestamp_ms, age_s, tx_count, tx_by_type, producer_address.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_tron_current_block()),
    ("tron_get_account",
     "TRON account state read straight off our own node: TRX balance, whether the account is activated, "
     "energy and bandwidth (used / limit), whether it is a witness, and account age. "
     "When to use: TRX balance and resource state before sizing a transfer, or checking whether an address exists on chain. "
     "Do not use: for USDT/TRC-20 token balances (use tron_read_contract with balanceOf) or for address risk "
     "scoring (that is tron_check_address_risk). "
     "Returns: found, address, trx_balance, name, created_ms, last_operation_ms, is_witness, has_code, energy, bandwidth.",
     {"type": "object", "properties": {"address": {"type": "string",
        "description": "TRON address, base58 (T...) or 41-hex"}}, "required": ["address"],
      "additionalProperties": False}, lambda a: t_tron_account(a.get("address"))),
    ("tron_get_contract",
     "TRON contract metadata: name, creator, whether its ABI is published on chain, the function list, "
     "code hash and whether it looks like a TRC-20. "
     "When to use: before calling a contract, or to tell an unverified contract from a published one. "
     "Do not use: for risk/backdoor analysis (that is tron_check_address_risk). "
     "Returns: found, address, name, creator, has_abi, abi_entry_count, is_trc20, functions, code_hash.",
     {"type": "object", "properties": {"address": {"type": "string", "description": "TRON contract address"}},
      "required": ["address"], "additionalProperties": False}, lambda a: t_tron_contract(a.get("address"))),
    ("tron_read_contract",
     "Read-only (constant) call against any TRON contract, executed by our own node. TRC-20 helpers "
     "balanceOf / allowance / decimals / symbol / name / totalSupply are built in; any other function can be "
     "called with a raw selector plus ABI-encoded parameter. No state is changed and no key is involved. "
     "When to use: an agent needs an on-chain value without trusting a third-party API - USDT balance of an "
     "address, token decimals, an allowance, or any view function. "
     "Do not use: to send a transaction (this never writes), or to look up a historical balance. "
     "Returns: ok, contract, function, output_type, value (decoded), value_human for 6-decimal tokens, raw, energy_used.",
     {"type": "object", "properties": {
        "contract": {"type": "string", "description": "Contract address (base58 or 41-hex)"},
        "function": {"type": "string", "description": "balanceOf | allowance | decimals | symbol | name | totalSupply, or a raw selector like 'foo(uint256)'"},
        "argument": {"type": "string", "description": "TRON address argument (for balanceOf / allowance)"},
        "argument2": {"type": "string", "description": "Second address argument (for allowance)"},
        "parameter": {"type": "string", "description": "Raw ABI-encoded parameter hex, for raw selectors"},
        "output_type": {"type": "string", "enum": ["uint256", "address", "string", "bool"]}},
      "required": ["contract", "function"], "additionalProperties": False},
     lambda a: t_tron_read_contract(a.get("contract"), a.get("function"), a.get("argument"),
                                    a.get("argument2"), a.get("parameter"), a.get("output_type"))),
    ("tron_get_chain_economics",
     "TRON chain economics straight off our node: energy price (sun per energy unit), transaction fee, "
     "account creation fee, witness pay per block, total energy and bandwidth limits, maintenance interval "
     "and the next maintenance time, unfreeze delay. "
     "When to use: working out what a transaction will actually cost in TRX, or how TRON's resource market "
     "is currently priced. "
     "Do not use: for Bitcoin fees (btc_get_fee_rates) or Monero fees (xmr_get_fee_rates). "
     "Returns: energy_fee_sun_per_unit, transaction_fee_sun, create_account_fee_trx, witness_pay_per_block_trx, "
     "maintenance_interval_s, next_maintenance_ms, unfreeze_delay_days, latest_energy_price.",
     {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: t_tron_chain_economics()),
    ("tron_list_witnesses",
     "TRON super representatives and candidates: who is producing blocks, how many they produced, how many "
     "they missed, votes and websites - read from our own node. "
     "When to use: governance or staking questions about TRON, or checking the health of block production. "
     "Do not use: for account balances (tron_get_account). "
     "Returns: registered_witnesses, active_block_producers, top_by_blocks_produced[] with address, url, "
     "blocks_produced, blocks_missed, votes, in_active_set.",
     {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200,
        "description": "How many top producers to return (default 30)"}},
      "additionalProperties": False}, lambda a: t_tron_list_witnesses(a.get("limit"))),
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
     "Metered: 5 free calls per day per IP, then 0.01 USDC per call over x402. There is no API key: "
     "the 402 answer carries the machine-readable terms in structuredContent.accepts[].",
     {"type": "object", "properties": {"address": {"type": "string",
                                                   "description": "TRON base58 address (T...)"}},
      "required": ["address"], "additionalProperties": False},
     lambda a: t_tron_check_address_risk(a.get("address"))),
    ("tron_verify_usdt_transfer",
     "TRC-20 USDT settlement check by transaction id: did it land, is it irreversible, how much, from and to whom. "
     "When to use: an agent or a merchant must confirm a USDT payment before releasing value - this is the "
     "settlement check that closes an automated trade loop. "
     "Do not use: to assess the counterparty (that is tron_check_address_risk). "
     "Data source: our own java-tron full node when the transaction is inside its ~6 day retention window "
     "(sub-second, no third party); it falls back to TronGrid automatically for older transactions, and says "
     "which one answered in `source`. A local miss is never reported as 'not found' by itself. "
     "Returns: found, status (SUCCESS|FAILED), finalized (present on the solidity/irreversible chain), "
     "confirmations, blocks_until_finality, block_number, block_time_utc, amount_usdt, from, to, fee_trx, "
     "checks {}, verified and a plain-language verdict. Pass expected_to / expected_amount_usdt to have the "
     "recipient and amount matched for you. "
     "Metered: 5 free calls per day per IP, then 0.01 USDC per call over x402. There is no API key: "
     "the 402 answer carries the machine-readable terms in structuredContent.accepts[].",
     {"type": "object", "properties": {"txid": {"type": "string", "description": "TRON transaction id (64 hex chars)"},
                                       "expected_to": {"type": "string", "description": "Optional: the recipient you expect"},
                                       "expected_amount_usdt": {"type": "number", "description": "Optional: the USDT amount you expect"}},
      "required": ["txid"], "additionalProperties": False},
     lambda a: t_tron_verify_usdt_transfer(a.get("txid"), a.get("expected_to"), a.get("expected_amount_usdt"))),
]


# ============== 2026-10-19 M1：web_search + web_fetch（高频联网工具）==================
# 工具契约与托管端点一致：搜索 provider 链 Serper（主选，条款明确允许增值分发）
# → Tavily（备选，日 credit 上限默认 15）→ Brave（可选，很多网络不可达，3s 快速失败）
# → cn.bing RSS（零 key 最后兜底）；抓取优先直连 + 标准库正文抽取，直连失败/被墙再走
# Tavily /extract。边界：纯标准库、单文件、不新增常驻服务；既有 43 个工具的行为一行不改。
# 计费：web 独立免费额度桶（search 20/天/IP、fetch 50/天/IP；全站 100/200），
#       超出走既有 x402 402 流程；web_search / web_fetch 各 1 credit。
WEB_TOOLS = ("web_search", "web_fetch")
METERED_TOOLS = RISK_TOOLS + WEB_TOOLS


def _env_int(name, default):
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


TAVILY_KEY = os.environ.get("RB_TAVILY_KEY", "").strip()
SERPER_KEY = os.environ.get("RB_SERPER_KEY", "").strip()      # 主通道（Serper）
BRAVE_KEY = os.environ.get("RB_BRAVE_KEY", "").strip()        # 可选备选（很多网络不可达）
TAVILY_SEARCH_URL = os.environ.get("RB_TAVILY_SEARCH_URL", "https://api.tavily.com/search")
TAVILY_EXTRACT_URL = os.environ.get("RB_TAVILY_EXTRACT_URL", "https://api.tavily.com/extract")
SERPER_SEARCH_URL = os.environ.get("RB_SERPER_SEARCH_URL", "https://google.serper.dev/search")
BRAVE_SEARCH_URL = os.environ.get("RB_BRAVE_SEARCH_URL", "https://api.search.brave.com/res/v1/web/search")
BING_RSS_URL = os.environ.get("RB_BING_RSS_URL", "https://cn.bing.com/search")
WEB_SEARCH_FREE = _env_int("RB_WEB_SEARCH_FREE", 20)
WEB_FETCH_FREE = _env_int("RB_WEB_FETCH_FREE", 50)
WEB_SEARCH_GLOBAL = _env_int("RB_WEB_SEARCH_GLOBAL", 100)
WEB_FETCH_GLOBAL = _env_int("RB_WEB_FETCH_GLOBAL", 200)
TAVILY_DAILY_CREDITS = _env_int("RB_TAVILY_DAILY_CREDITS", 15)
SERPER_DAILY_CALLS = _env_int("RB_SERPER_DAILY_CALLS", 200)
WEB_DAILY_BUDGET_MICRO = int(_env_float("RB_WEB_DAILY_BUDGET_USD", 5.0) * 1_000_000)
WEB_CACHE_DB = os.environ.get("RB_WEB_CACHE_DB", "./web_cache.db")
WEB_SEARCH_TTL = _env_int("RB_WEB_SEARCH_TTL", 600)
WEB_FETCH_TTL = _env_int("RB_WEB_FETCH_TTL", 1800)
WEB_TRUNCATE = _env_int("RB_WEB_TRUNCATE", 12000)
WEB_CACHE_MAX_MB = _env_int("RB_WEB_CACHE_MAX_MB", 100)
WEB_UA = os.environ.get("RB_WEB_UA", "RobotBaseMCP/0.25.2 (+https://robotbase.cc/mcp)")
WEB_RATE_PER_MIN = _env_int("RB_WEB_RATE_PER_MIN", 20)
WEB_MAX_BYTES = 2 * 1024 * 1024          # 解压后正文上限 2 MB
WEB_TIMEOUT_CONNECT = 5.0                # 建连上限
WEB_TIMEOUT_TOTAL = 8.0                  # 直连单跳总预算（socket 级）
WEB_MAX_REDIRECTS = 3
WEB_SEARCH_MAX_BYTES = 3800              # web_search 结果 JSON 上限（<4KB）
WEB_MIN_CONTENT = 200                    # 直连抽取少于 200 字 → 视为抽取失败，走 provider

_WEB_TTLS = {"search": WEB_SEARCH_TTL, "fetch": WEB_FETCH_TTL}
_WEB_SEM = threading.BoundedSemaphore(8)       # 上游并发上限
_WEB_CTYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/json",
               "application/xml", "text/xml", "text/markdown")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class _WebFetchError(Exception):
    """抓取管线的可读错误（code 是给客户端的稳定错误码）。"""

    def __init__(self, code, detail=""):
        super().__init__(code + ((": " + str(detail)) if detail else ""))
        self.code = code
        self.detail = str(detail)


class WebRateLimited(Exception):
    """web 工具专属限速（20 次/分/IP），与整体 120/min 限速相互独立。"""


def _bj_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.gmtime(time.time() + 8 * 3600))


# ---------------------------------------------------------------- 缓存 / 花费 / credit
def init_web_db():
    """建 web 缓存、每日花费、Tavily credit、Serper 日调用、robots 缓存五张表；权限 600。"""
    try:
        con = _db(WEB_CACHE_DB)
        con.executescript("""
        CREATE TABLE IF NOT EXISTS web_cache(
            key TEXT PRIMARY KEY, ts REAL NOT NULL, payload TEXT NOT NULL,
            bytes INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS idx_web_cache_ts ON web_cache(ts);
        CREATE TABLE IF NOT EXISTS web_spend(
            day TEXT NOT NULL, kind TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
            est_cost_micro_usd INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day, kind));
        CREATE TABLE IF NOT EXISTS tavily_credit(
            day TEXT PRIMARY KEY, credits INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS serper_daily(
            day TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS robots_cache(
            host TEXT PRIMARY KEY, ts REAL NOT NULL, rules TEXT NOT NULL);
        """)
        con.commit()
        con.close()
    except Exception as exc:  # noqa: BLE001
        print("[web] cache db init failed: %s" % exc, flush=True)
        return
    try:
        os.chmod(WEB_CACHE_DB, 0o600)
    except OSError:
        pass


def _web_cache_get(kind, key):
    try:
        con = _db(WEB_CACHE_DB)
        row = con.execute("SELECT ts,payload FROM web_cache WHERE key=?", (key,)).fetchone()
        con.close()
        if row and (time.time() - float(row["ts"])) < _WEB_TTLS[kind]:
            return json.loads(row["payload"])
    except Exception:  # noqa: BLE001
        pass
    return None


def _web_cache_put(kind, key, payload):
    """只缓存成功结果；按 ts LRU 淘汰，整体控制在 WEB_CACHE_MAX_MB 内。"""
    try:
        raw = json.dumps(payload, ensure_ascii=False)
        con = _db(WEB_CACHE_DB)
        con.execute("INSERT INTO web_cache(key,ts,payload,bytes) VALUES(?,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET ts=excluded.ts,payload=excluded.payload,"
                    "bytes=excluded.bytes", (key, time.time(), raw, len(raw.encode())))
        total = con.execute("SELECT COALESCE(SUM(bytes),0) s FROM web_cache").fetchone()["s"]
        cap = WEB_CACHE_MAX_MB * 1024 * 1024
        if total > cap:
            target = int(cap * 0.9)
            for r in con.execute("SELECT key,bytes FROM web_cache ORDER BY ts ASC").fetchall():
                if total <= target:
                    break
                con.execute("DELETE FROM web_cache WHERE key=?", (r["key"],))
                total -= int(r["bytes"] or 0)
        con.commit()
        con.close()
    except Exception as exc:  # noqa: BLE001
        print("[web] cache write failed: %s" % exc, flush=True)


def _web_spend_add(kind, calls=1, est_micro=0):
    try:
        con = _db(WEB_CACHE_DB)
        con.execute("INSERT INTO web_spend(day,kind,calls,est_cost_micro_usd) VALUES(?,?,?,?) "
                    "ON CONFLICT(day,kind) DO UPDATE SET calls=calls+excluded.calls, "
                    "est_cost_micro_usd=est_cost_micro_usd+excluded.est_cost_micro_usd",
                    (_bj_day(), kind, int(calls), int(est_micro)))
        con.commit()
        con.close()
    except Exception:  # noqa: BLE001
        pass


def _web_budget_ok():
    """每日上游花费熔断（RB_WEB_DAILY_BUDGET_USD，默认 5 USD）。"""
    try:
        con = _db(WEB_CACHE_DB)
        row = con.execute("SELECT COALESCE(SUM(est_cost_micro_usd),0) s FROM web_spend WHERE day=?",
                          (_bj_day(),)).fetchone()
        con.close()
        return int(row["s"] or 0) < WEB_DAILY_BUDGET_MICRO
    except Exception:  # noqa: BLE001
        return True


def _tavily_credit_used():
    try:
        con = _db(WEB_CACHE_DB)
        row = con.execute("SELECT credits FROM tavily_credit WHERE day=?", (_bj_day(),)).fetchone()
        con.close()
        return int(row["credits"] or 0) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def _tavily_credit_take(n=1):
    """预占 Tavily credit（原子读改写）；超日上限返回 False（search 降级、fetch 报错）。"""
    if n <= 0:
        return True
    try:
        con = _db(WEB_CACHE_DB)
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT credits FROM tavily_credit WHERE day=?", (_bj_day(),)).fetchone()
        used = int(row["credits"] or 0) if row else 0
        if used + n > TAVILY_DAILY_CREDITS:
            con.rollback()
            con.close()
            return False
        con.execute("INSERT INTO tavily_credit(day,credits) VALUES(?,?) "
                    "ON CONFLICT(day) DO UPDATE SET credits=credits+?", (_bj_day(), n, n))
        con.commit()
        con.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _tavily_credit_refund(n=1):
    try:
        con = _db(WEB_CACHE_DB)
        con.execute("UPDATE tavily_credit SET credits=MAX(0,credits-?) WHERE day=?",
                    (int(n), _bj_day()))
        con.commit()
        con.close()
    except Exception:  # noqa: BLE001
        pass


def _serper_daily_used():
    try:
        con = _db(WEB_CACHE_DB)
        row = con.execute("SELECT calls FROM serper_daily WHERE day=?", (_bj_day(),)).fetchone()
        con.close()
        return int(row["calls"] or 0) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def _serper_daily_take():
    """预占一次 Serper 日调用额度（RB_SERPER_DAILY_CALLS，默认 200/天）。"""
    try:
        con = _db(WEB_CACHE_DB)
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT calls FROM serper_daily WHERE day=?", (_bj_day(),)).fetchone()
        used = int(row["calls"] or 0) if row else 0
        if used >= SERPER_DAILY_CALLS:
            con.rollback()
            con.close()
            return False
        con.execute("INSERT INTO serper_daily(day,calls) VALUES(?,1) "
                    "ON CONFLICT(day) DO UPDATE SET calls=calls+1", (_bj_day(),))
        con.commit()
        con.close()
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 独立免费额度桶
def _web_quota_cfg(name):
    if name == "web_search":
        return "ws", WEB_SEARCH_FREE, "__global_web_search__", WEB_SEARCH_GLOBAL
    return "wf", WEB_FETCH_FREE, "__global_web_fetch__", WEB_FETCH_GLOBAL


def _web_quota(name, consume=False):
    """返回 (allowed, used, limit)。web 桶与风控的 5 次/天桶完全独立。"""
    prefix, limit, gkey, glimit = _web_quota_cfg(name)
    h = prefix + ":" + hashlib.sha256(_quota_subject().encode()).hexdigest()[:16]
    d = _bj_day()
    con = _db(RISK_DB)
    try:
        row = con.execute("SELECT used FROM risk_quota WHERE day=? AND ip_hash=?", (d, h)).fetchone()
        used = int(row["used"]) if row else 0
        g = con.execute("SELECT used FROM risk_quota WHERE day=? AND ip_hash=?", (d, gkey)).fetchone()
        gused = int(g["used"]) if g else 0
        if gused >= glimit:
            return False, max(used, limit), limit
        if not consume:
            return used < limit, used, limit
        if used >= limit:
            return False, used, limit
        con.execute("INSERT INTO risk_quota(day,ip_hash,used) VALUES(?,?,1) "
                    "ON CONFLICT(day,ip_hash) DO UPDATE SET used=used+1", (d, h))
        con.execute("INSERT INTO risk_quota(day,ip_hash,used) VALUES(?,?,1) "
                    "ON CONFLICT(day,ip_hash) DO UPDATE SET used=used+1", (d, gkey))
        con.commit()
        return True, used + 1, limit
    finally:
        con.close()


def _web_gate(name):
    """web 工具门禁（只查不扣）：花费熔断 / 免费额度用尽 → 走既有 402 流程。"""
    if not _web_budget_ok():
        raise PaymentRequired(
            "Payment Required. The site-wide daily budget for web tools is used up (Beijing time, "
            "resets 00:00 Asia/Shanghai). Paid calls still work with an x402 payment payload in "
            "params._meta[\"x402/payment\"]; there is no API key.")
    ok, used, limit = _web_quota(name, False)
    if not ok:
        raise PaymentRequired(
            "Payment Required. The free web allowance for %s is %d calls/day per IP and is used up "
            "for today (Beijing time, resets 00:00 Asia/Shanghai). This bucket is separate from the "
            "risk-tool allowance. Pay in USDC with x402: read accepts[] from structuredContent, "
            "sign one EIP-3009 authorization, and retry this same call with it in "
            "params._meta[\"x402/payment\"]. There is no API key." % (name, limit))
    return _meter_payload(used, limit, pending=True)


_web_rl, _web_rl_lock = {}, threading.Lock()


def _web_rate_ok():
    ip = _quota_subject() or "unknown"
    now = time.time()
    with _web_rl_lock:
        win = _web_rl.setdefault(ip, [])
        win[:] = [t for t in win if now - t < 60]
        if len(win) >= WEB_RATE_PER_MIN:
            return False
        win.append(now)
    return True


def _call_web_tool(name, fn, args):
    """web 工具的免费档执行：额度门禁 → web 专属限速 → 执行 → 计费。"""
    gate = _web_gate(name)
    tier = gate.get("free_tier") or {}
    used_before, limit = int(tier.get("used_today", 0)), int(tier.get("limit_per_day", -1))
    if not _web_rate_ok():
        raise WebRateLimited("web tools are limited to %d calls/min per IP; retry after 60s"
                             % WEB_RATE_PER_MIN)
    out = fn(args or {})
    no_charge = isinstance(out, dict) and bool(out.pop("_no_charge", False))
    if limit < 0:
        meter = _meter_payload(0, -1)
    elif no_charge:
        meter = _meter_payload(used_before, limit, pending=True, charged=False)
    else:
        ok, used, _l = _web_quota(name, True)
        meter = _meter_payload(used if ok else used_before + 1, limit)
        meter["free_tier"]["charged_this_call"] = True
    if isinstance(out, dict):
        out["_metering"] = meter
    else:
        out = {"result": out, "_metering": meter}
    return out


# ---------------------------------------------------------------- SSRF 硬防护
def _ip_public(ip):
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return False
    if ip.version == 4 and ip in _CGNAT:
        return False
    return bool(ip.is_global)


def _safe_url(u):
    """连接前校验 + 全部 A/AAAA 解析校验；失败一律 _WebFetchError('invalid_url')。"""
    if not u or len(u) > 2048:
        raise _WebFetchError("invalid_url", "length")
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        raise _WebFetchError("invalid_url", "parse")
    if p.scheme not in ("http", "https"):
        raise _WebFetchError("invalid_url", "scheme")
    if p.username or p.password:
        raise _WebFetchError("invalid_url", "userinfo")
    host = (p.hostname or "").strip().rstrip(".")
    if not host:
        raise _WebFetchError("invalid_url", "host")
    hl = host.lower()
    if (hl == "localhost" or hl.endswith(".localhost") or hl.endswith(".local")
            or hl.endswith(".internal") or hl.endswith(".onion")):
        raise _WebFetchError("invalid_url", "host blocklist")
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        raise _WebFetchError("invalid_url", "port")
    if port not in (80, 443):
        raise _WebFetchError("invalid_url", "port")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise _WebFetchError("invalid_url", "dns")
    # 只保留公网地址：DNS 污染/轮换场景里常混入私网或保留地址（例如被污染的
    # en.wikipedia.org 会返回 2001::1），过滤掉它们但仍然只连接公网 IP；
    # 若一个公网地址都没有（loopback / private / link-local / 未指定地址），则硬拒绝。
    ips = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        except ValueError:
            continue
        if _ip_public(ip):
            ips.append(str(ip))
    if not ips:
        raise _WebFetchError("invalid_url", "non-public address")
    return p, ips


def _decode_body(raw, ctype):
    m = re.search(r"charset=([\w\-]+)", ctype or "", re.I)
    enc = m.group(1) if m else "utf-8"
    try:
        return raw.decode(enc, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """把 DNS 解析结果钉在连接层，避免解析后重绑定（HTTP）。"""

    def __init__(self, host, port, ip, timeout=WEB_TIMEOUT_TOTAL):
        super().__init__(host, port, timeout=timeout)
        self._pin_ip = ip
        self._create_connection = self._pinned_create

    def _pinned_create(self, address, timeout=None, source_address=None):
        sock = socket.create_connection((self._pin_ip, self.port), WEB_TIMEOUT_CONNECT, source_address)
        sock.settimeout(WEB_TIMEOUT_TOTAL)
        return sock


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """同上，HTTPS 仍用真实 host 做 SNI / 证书校验（只把 TCP 目标钉到 IP）。"""

    def __init__(self, host, port, ip, timeout=WEB_TIMEOUT_TOTAL):
        super().__init__(host, port, timeout=timeout)
        self._pin_ip = ip
        self._create_connection = self._pinned_create

    def _pinned_create(self, address, timeout=None, source_address=None):
        sock = socket.create_connection((self._pin_ip, self.port), WEB_TIMEOUT_CONNECT, source_address)
        sock.settimeout(WEB_TIMEOUT_TOTAL)
        return sock


def _direct_http_get(u, max_bytes=WEB_MAX_BYTES, allow_any_ctype=False):
    """直连 GET：逐跳 SSRF 校验、最多 3 跳重定向、gzip 解压后 ≤2 MB。"""
    current = u
    for _hop in range(WEB_MAX_REDIRECTS + 1):
        p, ips = _safe_url(current)
        host = p.hostname
        port = p.port or (443 if p.scheme == "https" else 80)
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        headers = {"User-Agent": WEB_UA,
                   "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                              "text/plain;q=0.8,text/markdown;q=0.7"),
                   "Accept-Encoding": "gzip",
                   "Accept-Language": "en-US,en;q=0.8,zh-CN;q=0.6"}
        cls = _PinnedHTTPSConnection if p.scheme == "https" else _PinnedHTTPConnection
        conn = cls(host, port, ips[0])
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            if status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location")
                if not loc:
                    raise _WebFetchError("fetch_failed", "redirect without location")
                current = urllib.parse.urljoin(current, loc)
                continue
            ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
            if status >= 400:
                raise _WebFetchError(
                    "blocked_by_site" if status in (401, 403, 429) else "fetch_failed",
                    "HTTP %d" % status)
            if ctype and not allow_any_ctype and not any(ctype.startswith(x) for x in _WEB_CTYPES):
                raise _WebFetchError("unsupported_content_type", ctype)
            raw = resp.read(max_bytes + 1)
            if (resp.getheader("Content-Encoding") or "").lower() == "gzip" and raw:
                try:
                    raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw, max_bytes + 1)
                except zlib.error:
                    raw = b""
            if len(raw) > max_bytes:
                raise _WebFetchError("too_large", "body")
            return {"final_url": current, "status": status, "content_type": ctype,
                    "body": _decode_body(raw, ctype)}
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    raise _WebFetchError("too_many_redirects", "")


# ---------------------------------------------------------------- robots.txt
_ROBOTS_TTL = 3600
_ROBOTS_MEM, _ROBOTS_LOCK = {}, threading.Lock()


def _robots_match(pattern, path):
    if not pattern:
        return False
    anchor = pattern.endswith("$")
    core = pattern[:-1] if anchor else pattern
    rx = re.escape(core).replace(r"\*", ".*")
    if anchor:
        rx += "$"
    try:
        return re.match(rx, path) is not None
    except re.error:
        return False


def _parse_robots(text):
    groups, agents, rules = [], [], []

    def _close():
        if agents:
            groups.append((list(agents), list(rules)))

    for line in (text or "").splitlines():
        s = line.split("#", 1)[0].strip()
        if ":" not in s:
            continue
        k, v = s.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if k == "user-agent":
            if rules:
                _close()
                agents, rules = [], []
            agents.append(v.lower())
        elif k in ("allow", "disallow") and agents:
            rules.append((k, v))
    _close()
    for target in ("robotbasemcp", "robotbase", "*"):
        for ags, rs in groups:
            if target in ags:
                return rs
    return []


def _robots_fetch(origin):
    try:
        d = _direct_http_get(origin + "/robots.txt", max_bytes=512 * 1024, allow_any_ctype=True)
    except _WebFetchError as exc:
        if exc.code == "blocked_by_site":
            return [("disallow", "/")]      # 401/403 视为全站禁止（RFC 9309）
        return []
    except Exception:  # noqa: BLE001
        return []
    return _parse_robots(d.get("body") or "")


def _robots_rules(origin):
    now = time.time()
    with _ROBOTS_LOCK:
        hit = _ROBOTS_MEM.get(origin)
        if hit and now - hit[0] < _ROBOTS_TTL:
            return hit[1]
    rules = None
    try:
        con = _db(WEB_CACHE_DB)
        row = con.execute("SELECT ts,rules FROM robots_cache WHERE host=?", (origin,)).fetchone()
        con.close()
        if row and now - float(row["ts"]) < _ROBOTS_TTL:
            rules = json.loads(row["rules"])
    except Exception:  # noqa: BLE001
        rules = None
    if rules is None:
        rules = _robots_fetch(origin)
        try:
            con = _db(WEB_CACHE_DB)
            con.execute("INSERT INTO robots_cache(host,ts,rules) VALUES(?,?,?) "
                        "ON CONFLICT(host) DO UPDATE SET ts=excluded.ts,rules=excluded.rules",
                        (origin, now, json.dumps(rules)))
            con.commit()
            con.close()
        except Exception:  # noqa: BLE001
            pass
    with _ROBOTS_LOCK:
        _ROBOTS_MEM[origin] = (now, rules)
        if len(_ROBOTS_MEM) > 256:
            for k in sorted(_ROBOTS_MEM, key=lambda k: _ROBOTS_MEM[k][0])[:64]:
                _ROBOTS_MEM.pop(k, None)
    return rules


def _robots_allowed(u):
    p = urllib.parse.urlsplit(u)
    origin = "%s://%s" % (p.scheme, p.netloc)
    rules = _robots_rules(origin)
    if not rules:
        return True
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    best, best_len = None, -1
    for kind, pat in rules:
        if pat and _robots_match(pat, path):
            score = len(pat)
            if score > best_len:
                best, best_len = kind, score
    return best != "disallow"


# ---------------------------------------------------------------- 正文抽取（纯标准库）
class _TextExtractor(html.parser.HTMLParser):
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form",
            "iframe", "svg", "canvas", "template", "button", "select", "input", "textarea",
            "dialog", "menu", "figure"}
    # 空元素（HTML 里没有结束标签）：绝不能在 skip 计数器上留残余，否则整页文本都会被吞掉
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
            "param", "source", "track", "wbr"}
    BLOCKS = {"p", "div", "section", "article", "main", "ul", "ol", "table", "tbody",
              "thead", "tr", "blockquote", "figcaption", "dt", "dd", "li"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.in_title = False
        self.title = []
        self.buf = []
        self.blocks = []
        self.prefix = ""
        self.pre = False
        self.pre_buf = []
        self.link = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "title":
            self.in_title = True
            return
        if tag in self.SKIP:
            if tag not in self.VOID:
                self.skip += 1
            return
        if self.skip:
            return
        if tag == "pre":
            self._flush()
            self.pre, self.pre_buf = True, []
            return
        if tag in ("h1", "h2", "h3"):
            self._flush()
            self.prefix = "#" * int(tag[1]) + " "
            return
        if tag == "li":
            self._flush()
            self.prefix = "- "
            return
        if tag == "br":
            self.buf.append(" ")
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if isinstance(href, str) and href.startswith(("http://", "https://")) and len(href) <= 500:
                self.link = (href, len(self.buf))
            return
        if tag in self.BLOCKS:
            self._flush()

    def handle_startendtag(self, tag, attrs):
        """自闭合标签（<input/>、<svg/>…）不能走 start+end 配对的 skip 计数。"""
        tag = tag.lower()
        if tag == "br":
            self.buf.append(" ")
            return
        if tag in self.SKIP:
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
            return
        if tag in self.SKIP:
            if self.skip:
                self.skip -= 1
            return
        if self.skip:
            return
        if tag == "pre":
            self._flush_pre()
            return
        if tag == "a" and self.link:
            href, start = self.link
            self.link = None
            label = "".join(self.buf[start:]).strip()
            if 0 < len(label) <= 100:
                self.buf[start:] = ["[%s](%s)" % (label, href)]
            return
        if tag in self.BLOCKS:
            self._flush()

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        elif self.skip:
            return
        elif self.pre:
            self.pre_buf.append(data)
        else:
            self.buf.append(data)

    def _flush(self):
        txt = " ".join(("".join(self.buf)).split())
        self.buf = []
        if txt:
            self.blocks.append((self.prefix + txt) if self.prefix else txt)
        self.prefix = ""

    def _flush_pre(self):
        txt = "\n".join(l.rstrip() for l in "".join(self.pre_buf).splitlines()).strip("\n")
        self.pre, self.pre_buf = False, []
        if txt.strip():
            self.blocks.append("```\n" + txt + "\n```")


def _strip_tags_fallback(raw):
    """兜底：解析器因畸形 HTML 拿不到正文时，用正则粗提文本（宁可粗糙也不能空）。"""
    s = re.sub(r"(?is)<(script|style|noscript|svg|template|nav|header|footer|aside|form|iframe)"
               r"\b.*?</\1\s*>", " ", raw or "")
    s = re.sub(r"(?is)<[^>]+>", "\n", s)
    s = html.unescape(s)
    lines = [" ".join(l.split()) for l in s.splitlines()]
    return "\n\n".join(l for l in lines if l)


def _html_to_text(raw):
    p = _TextExtractor()
    try:
        p.feed(raw or "")
        p.close()
    except Exception:  # noqa: BLE001
        pass
    p._flush()
    title = html.unescape("".join(p.title)).strip()
    content = "\n\n".join(b for b in p.blocks if b.strip())
    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    if len(content) < 50:
        fallback = _strip_tags_fallback(raw)
        if len(fallback) > len(content):
            content = fallback[:200000]
    return title, content


def _markdown_to_text(md):
    s = re.sub(r"```[^\n]*\n?", "", md or "")
    s = re.sub(r"^#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"^\s*[-*]\s+", "", s, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _clean_provider_text(s):
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = "\n".join(l.rstrip() for l in s.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


# ---------------------------------------------------------------- 搜索后端
def _http_json_auth(url, payload, key, timeout, headers=None):
    h = {"Content-Type": "application/json", "User-Agent": WEB_UA}
    if headers:
        h.update(headers)
    elif key:
        h["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode(errors="replace") or "{}")


def _tavily_search(query, count, freshness, site, lang):
    if not TAVILY_KEY:
        raise RuntimeError("tavily_not_configured")
    if not _tavily_credit_take(1):
        raise RuntimeError("tavily_daily_credit_exhausted")
    body = {"query": query, "search_depth": "basic",
            "max_results": max(1, min(10, count)),
            "include_answer": False, "include_raw_content": False}
    if site:
        body["include_domains"] = [site]
    if freshness in ("day", "week", "month", "year"):
        body["time_range"] = freshness
    ok = False
    try:
        data = _http_json_auth(TAVILY_SEARCH_URL, body, TAVILY_KEY, timeout=12)
        ok = True
    finally:
        if not ok:
            _tavily_credit_refund(1)
    _web_spend_add("search", 1, 6000)          # ≈$0.006/次（basic search）
    results = []
    for it in (data.get("results") or []):
        url = str(it.get("url") or "").strip()
        if not url:
            continue
        results.append({"title": str(it.get("title") or "").strip()[:200],
                        "url": url,
                        "snippet": str(it.get("content") or "").strip()[:500],
                        "published": it.get("published_date") or None})
    if not results:
        raise RuntimeError("tavily_empty")
    return results


def _serper_search(query, count, freshness, site, lang):
    """主通道：Serper（SERP 数据 B2B 服务，ToS 明确允许增值分发；与 Google 无关）。

    契约：POST https://google.serper.dev/search，X-API-KEY 头，num 硬性 ≤10，
    超时 5s，失败不重试（每次重试都多付一次且拉高延迟）；日调用上限 RB_SERPER_DAILY_CALLS。
    """
    if not SERPER_KEY:
        raise RuntimeError("serper_not_configured")
    if not _serper_daily_take():
        raise RuntimeError("serper_daily_call_cap")
    q = query + ((" site:" + site) if site else "")
    body = {"q": q, "num": max(1, min(10, count))}
    if lang:
        body["hl"] = lang
    if freshness in ("day", "week", "month", "year"):
        body["tbs"] = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}[freshness]
    data = _http_json_auth(SERPER_SEARCH_URL, body, SERPER_KEY, timeout=5,
                           headers={"X-API-KEY": SERPER_KEY})
    _web_spend_add("search", 1, 400)           # ≈$0.0004/次
    results = []
    for it in (data.get("organic") or []):
        url = str(it.get("link") or "").strip()
        if not url:
            continue
        results.append({"title": str(it.get("title") or "").strip()[:200], "url": url,
                        "snippet": str(it.get("snippet") or "").strip()[:500],
                        "published": it.get("date") or None})
    if not results:
        raise RuntimeError("serper_empty")
    return results


def _brave_search(query, count, freshness, site, lang):
    """备选：Brave Search API。很多网络（包括本项目的落地环境）到
    api.search.brave.com 不可达，所以超时固定 3s：失败必须快、被跳过，不能拖慢
    整条搜索链路。"""
    if not BRAVE_KEY:
        raise RuntimeError("brave_not_configured")
    params = {"q": query + ((" site:" + site) if site else ""),
              "count": str(max(1, min(10, count)))}
    if lang:
        params["search_lang"] = lang
    if freshness in ("day", "week", "month", "year"):
        params["freshness"] = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}[freshness]
    url = BRAVE_SEARCH_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Subscription-Token": BRAVE_KEY,
                                               "Accept": "application/json",
                                               "User-Agent": WEB_UA})
    with urllib.request.urlopen(req, timeout=3) as r:
        data = json.loads(r.read(2 * 1024 * 1024).decode(errors="replace") or "{}")
    results = []
    for it in (((data.get("web") or {}).get("results")) or []):
        u = str(it.get("url") or "").strip()
        if not u:
            continue
        results.append({"title": str(it.get("title") or "").strip()[:200], "url": u,
                        "snippet": str(it.get("description") or "").strip()[:500],
                        "published": it.get("page_age") or None})
    if not results:
        raise RuntimeError("brave_empty")
    _web_spend_add("search", 1, 0)
    return results


def _bing_rss_search(query, count, freshness, site, lang):
    q = query + ((" site:" + site) if site else "")
    params = {"q": q, "format": "rss", "count": str(max(10, count))}
    if lang:
        params["setlang"] = lang
    url = BING_RSS_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": WEB_UA,
                                               "Accept": "application/rss+xml, application/xml, text/xml"})
    with urllib.request.urlopen(req, timeout=8) as r:
        raw = r.read(2 * 1024 * 1024)
    root = ET.fromstring(raw)
    results = []
    for it in root.findall(".//item"):
        link = (it.findtext("link") or "").strip()
        title = (it.findtext("title") or "").strip()
        desc = it.findtext("description") or ""
        desc = html.unescape(re.sub(r"<[^>]+>", " ", desc))
        desc = " ".join(desc.split())
        pub = (it.findtext("pubDate") or "").strip() or None
        if link:
            results.append({"title": title[:200], "url": link,
                            "snippet": desc[:500], "published": pub})
    if not results:
        raise RuntimeError("bing_empty")
    # 兜底通道按验收口径返回 7–10 条（bing RSS 本身通常给 10 条）；不按 count 再切
    return results[:10]


def _normalize_results(results):
    seen, out = set(), []
    for r in results:
        u = (r.get("url") or "").strip()
        if not u.startswith(("http://", "https://")) or u in seen:
            continue
        seen.add(u)
        out.append(r)
    n = len(out)
    if n >= 4:
        domains = {}
        for r in out:
            try:
                dom = (urllib.parse.urlsplit(r["url"]).hostname or "").lower()
            except ValueError:
                dom = ""
            domains[dom] = domains.get(dom, 0) + 1
        top, cnt = max(domains.items(), key=lambda kv: kv[1])
        if cnt > max(2, int(n * 0.4)):
            keep, c, deduped = max(2, int(n * 0.4)), 0, []
            for r in out:
                try:
                    dom = (urllib.parse.urlsplit(r["url"]).hostname or "").lower()
                except ValueError:
                    dom = ""
                if dom == top:
                    c += 1
                    if c > keep:
                        continue
                deduped.append(r)
            out = deduped
    return out


def _cap_search_payload(payload, limit=WEB_SEARCH_MAX_BYTES):
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw) <= limit:
        return payload
    results = payload.get("results") or []
    for cap in (300, 220, 160, 120, 80, 40, 0):
        for r in results:
            r["snippet"] = (r.get("snippet") or "")[:cap]
        raw = json.dumps(payload, ensure_ascii=False)
        if len(raw) <= limit:
            payload["truncated"] = True
            return payload
    payload["truncated"] = True
    payload["results"] = results[:max(1, len(results) // 2)]
    return payload


def _search_key(query, count, freshness, site, lang):
    raw = json.dumps([query, count, freshness, site, lang], ensure_ascii=False, sort_keys=True)
    return "s:" + hashlib.sha256(raw.encode()).hexdigest()


def _search_error(q, code, message=""):
    out = {"query": q[:200], "count": 0, "results": [], "provider": None,
           "cached": False, "truncated": False, "error": code}
    if message:
        out["message"] = message
    out["_no_charge"] = True
    return out


def t_web_search(query=None, count=5, freshness=None, site=None, lang=None):
    """web_search 工具体：缓存 → Tavily（主）→ cn.bing RSS（兜底）。"""
    started = time.time()
    q = " ".join(str(query or "").split())[:200]
    if not q:
        return _search_error(q, "query_required", "query must be a non-empty string (1-200 chars)")
    try:
        c = max(1, min(10, int(count)))
    except (TypeError, ValueError):
        c = 5
    fr = str(freshness or "").strip().lower()
    fr = fr if fr in ("day", "week", "month", "year") else None
    st = str(site or "").strip().lower()
    if st:
        st = re.sub(r"^[a-z]+://", "", st).split("/")[0].split("?")[0]
        if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,250}\.[a-z]{2,}", st):
            return _search_error(q, "invalid_site", "site must be a bare domain like github.com")
    lg = re.sub(r"[^a-z0-9\-]", "", str(lang or "").strip().lower())[:20] or None
    key = _search_key(q, c, fr, st, lg)
    cached = _web_cache_get("search", key)
    if cached is not None:
        out = dict(cached)
        out["cached"] = True
        out["took_ms"] = int((time.time() - started) * 1000)
        return out
    # 主选 Serper → Tavily（日 15 credits 上限）→ Brave（3s 快速跳过）→ cn.bing RSS 兜底
    provider, results, errs = None, None, []
    if SERPER_KEY and _serper_daily_used() < SERPER_DAILY_CALLS:
        try:
            results = _serper_search(q, c, fr, st, lg)
            provider = "serper"
        except Exception as exc:  # noqa: BLE001
            errs.append("serper:%s" % type(exc).__name__)
    if results is None and TAVILY_KEY and _tavily_credit_used() < TAVILY_DAILY_CREDITS:
        try:
            results = _tavily_search(q, c, fr, st, lg)
            provider = "tavily"
        except Exception as exc:  # noqa: BLE001
            errs.append("tavily:%s" % type(exc).__name__)
    if results is None and BRAVE_KEY:
        try:
            results = _brave_search(q, c, fr, st, lg)
            provider = "brave"
        except Exception as exc:  # noqa: BLE001
            errs.append("brave:%s" % type(exc).__name__)
    if results is None:
        try:
            results = _bing_rss_search(q, c, fr, st, lg)
            provider = "bing_rss"
            _web_spend_add("search", 1, 0)
        except Exception as exc:  # noqa: BLE001
            errs.append("bing_rss:%s" % type(exc).__name__)
    if results is None:
        return _search_error(q, "search_failed",
                             "all search providers failed (%s); try again later" % ", ".join(errs))
    out = {"query": q, "count": 0, "results": _normalize_results(results),
           "provider": provider, "cached": False, "truncated": False,
           "took_ms": int((time.time() - started) * 1000)}
    out["count"] = len(out["results"])
    out = _cap_search_payload(out)
    if out["results"]:
        _web_cache_put("search", key, out)
    return out


# ---------------------------------------------------------------- 抓取管线
def _url_domain(u):
    try:
        p = urllib.parse.urlsplit(u if "://" in (u or "") else "http://" + (u or ""))
        return (p.hostname or "")[:200]
    except Exception:  # noqa: BLE001
        return ""


def _fetch_error(u, code, message=""):
    dom = _url_domain(u)
    out = {"url": dom, "final_url": dom, "status": 0, "title": "", "content": "",
           "chars": 0, "truncated": False, "fetched_at": _bj_iso(), "source": None,
           "cached": False, "error": code}
    if message:
        out["message"] = message
    out["_no_charge"] = True
    return out


def _fetch_key(u):
    p = urllib.parse.urlsplit(u)
    netloc = (p.hostname or "").lower()
    if p.port and p.port not in (80, 443):
        netloc += ":%d" % p.port
    norm = "%s://%s%s" % (p.scheme.lower(), netloc, p.path or "/")
    if p.query:
        norm += "?" + p.query
    return "f:" + hashlib.sha256(norm.encode()).hexdigest()


def _tavily_extract(u):
    if not TAVILY_KEY:
        raise RuntimeError("tavily_not_configured")
    if not _tavily_credit_take(1):
        raise RuntimeError("tavily_daily_credit_exhausted")
    ok = False
    try:
        data = _http_json_auth(TAVILY_EXTRACT_URL,
                               {"urls": [u], "extract_depth": "basic"}, TAVILY_KEY, timeout=25)
        ok = True
    finally:
        if not ok:
            _tavily_credit_refund(1)
    _web_spend_add("fetch", 1, 1600)           # ≈$0.0016/页（basic extract）
    results = data.get("results") or []
    raw = (results[0] or {}).get("raw_content") if results else ""
    if not raw or len(str(raw)) < 50:
        raise RuntimeError("tavily_extract_empty")
    return _clean_provider_text(str(raw))


def _apply_fetch_view(base, max_chars, fmt, cached, started):
    content = base.get("content") or ""
    if fmt == "text":
        content = _markdown_to_text(content)
    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars]
    out = {"url": base.get("url"), "final_url": base.get("final_url"),
           "status": base.get("status"), "title": base.get("title"), "content": content,
           "chars": len(content), "truncated": truncated,
           "fetched_at": base.get("fetched_at"), "source": base.get("source"),
           "cached": cached, "took_ms": int((time.time() - started) * 1000)}
    # 整体响应（含 metering 前）控制在 WEB_TRUNCATE 内，保证 JSON 不被拦腰截断
    # 2026-10-19 (ported QA P2-8): the old budget ignored max_chars, so 19k/20k requests were
    # silently shrunk to 9.7k/8.2k by the 0.8 loop. Give content+envelope enough room.
    budget = max(1000, max(WEB_TRUNCATE, int(max_chars) + 1500) - 600)
    while out["content"] and len(json.dumps(out, ensure_ascii=False)) > budget:
        out["content"] = out["content"][:int(len(out["content"]) * 0.8)]
        out["truncated"] = True
        out["chars"] = len(out["content"])
    return out


def t_web_fetch(url=None, max_chars=8000, format="markdown"):
    """web_fetch 工具体：缓存 → robots → 直连+抽取 → Tavily /extract 兜底。"""
    started = time.time()
    u = str(url or "").strip()
    try:
        mc = int(max_chars)
    except (TypeError, ValueError):
        mc = 8000
    # 2026-10-19 (ported QA P2-8): the documented cap is 12,000 chars per call; apply it here too.
    mc = max(2000, min(WEB_TRUNCATE, mc))
    fmt = "text" if str(format or "").strip().lower() == "text" else "markdown"
    if not u:
        return _fetch_error(u, "invalid_url", "url is required")
    if len(u) > 2048:
        return _fetch_error(u, "invalid_url", "url must be at most 2048 characters")
    try:
        p = urllib.parse.urlsplit(u)
        if p.scheme not in ("http", "https"):
            raise ValueError("scheme")
    except ValueError:
        return _fetch_error(u, "invalid_url", "only http(s) URLs are supported")
    key = _fetch_key(u)
    cached = _web_cache_get("fetch", key)
    if cached is not None:
        out = _apply_fetch_view(cached, mc, fmt, True, started)
        if getattr(_REQ, "payment", None):
            out["_no_charge"] = True          # 付费用户缓存命中不扣点
        return out
    got = _WEB_SEM.acquire(timeout=10)
    if not got:
        return _fetch_error(u, "busy", "too many web_fetch calls in flight; retry shortly")
    try:
        # ① robots.txt（命中 Disallow 不抓、不计费）
        try:
            if not _robots_allowed(u):
                return _fetch_error(u, "robots_disallowed",
                                    "this site's robots.txt disallows fetching this path")
        except _WebFetchError:
            return _fetch_error(u, "invalid_url")
        except Exception:  # noqa: BLE001
            pass
        source, title, content, final_url, status = None, "", "", u, 0
        direct_err = ""
        # ② 直连 + 标准库正文抽取
        try:
            d = _direct_http_get(u)
            ctype = d.get("content_type") or ""
            body = d.get("body") or ""
            final_url, status = d["final_url"], d["status"]
            if "html" in ctype or (not ctype and "<html" in body[:2000].lower()):
                title, content = _html_to_text(body)
            else:
                content = body
                first = content.strip().splitlines()[0][:200] if content.strip() else ""
                title = first
            if len(content) < WEB_MIN_CONTENT:
                raise _WebFetchError("content_too_short", "%d chars" % len(content))
            source = "direct"
        except _WebFetchError as exc:
            if exc.code == "invalid_url":
                return _fetch_error(u, "invalid_url", "URL rejected by the SSRF guard")
            if exc.code in ("unsupported_content_type", "too_large"):
                return _fetch_error(u, exc.code, "the target returned an unsupported document")
            # 2026-10-19 (ported QA P2-9): surface a target HTTP 4xx/5xx as http_<code>
            # instead of silently retrying through the provider fallback.
            _hd = re.search(r"HTTP\s+([45][0-9]{2})", str(getattr(exc, "detail", "") or ""))
            if _hd and exc.code in ("blocked_by_site", "fetch_failed"):
                return _fetch_error(u, "http_%s" % _hd.group(1),
                                    "the target returned HTTP %s; no fallback was attempted" % _hd.group(1))
            direct_err = exc.code
        except Exception as exc:  # noqa: BLE001
            direct_err = ("timeout" if isinstance(exc, (socket.timeout, TimeoutError))
                          or "timed out" in str(exc).lower() else type(exc).__name__)
        # ③ provider 兜底（被墙/直连失败）
        if source is None:
            try:
                content = _tavily_extract(u)
                source, status, final_url = "provider", 200, u
                title = ""
            except Exception as exc:  # noqa: BLE001
                reason = str(exc).lower()
                if "credit" in reason or "not_configured" in reason:
                    code = "provider_budget_exhausted"
                elif "timed out" in reason or "timeout" in reason or direct_err == "timeout":
                    code = "timeout"
                else:
                    code = "blocked_by_network"
                if code == "provider_budget_exhausted":
                    msg = ("the daily provider budget is exhausted; the direct fetch failed "
                           "(%s)" % (direct_err or "unknown"))
                elif code == "timeout":
                    msg = ("the fetch timed out (direct fetch: %s; the provider fallback also "
                           "timed out)" % (direct_err or "unknown"))
                else:
                    msg = ("this site is not reachable from this node and the provider fallback "
                           "failed")
                return _fetch_error(u, code, msg)
        base = {"url": u, "final_url": final_url, "status": status, "title": title,
                "content": content[:20000], "fetched_at": _bj_iso(), "source": source}
        _web_cache_put("fetch", key, base)
        if source == "direct":
            _web_spend_add("fetch", 1, 0)
        return _apply_fetch_view(base, mc, fmt, False, started)
    finally:
        try:
            _WEB_SEM.release()
        except ValueError:
            pass


# ---------------------------------------------------------------- 工具注册
_WEB_SEARCH_DESC = (
    "Search the live web and get ranked results with title, URL, snippet and published date. "
    "When to use: an agent needs current facts, news or links that are not on chain - 'what is "
    "the BTC fee market saying today', 'recent Zcash governance proposals', 'find the docs page "
    "for X'. Use it before web_fetch, then fetch the one URL that looks authoritative. "
    "Do not use: for on-chain facts (use the BTC/ETH/XMR/ZEC/TRON tools), or when you already "
    "have the target URL (that is web_fetch). "
    "Returns: {query, count, results:[{title,url,snippet,published}], provider "
    "(serper|tavily|brave|bing_rss), cached, took_ms, _metering}. Results may be cached for up "
    "to 10 minutes; a cached result still counts as one call against the free allowance. "
    "Metered: 20 free searches/day per IP (separate from the risk-tool allowance), then 1 "
    "credit per call over x402 - there is no API key; the 402 answer carries accepts[]. "
    "Max count is 10."
)
_WEB_FETCH_DESC = (
    "Fetch one public web page and return its readable text as clean markdown or plain text, with "
    "navigation, scripts, headers and footers stripped. "
    "When to use: you have a specific URL (from web_search or the user) and need what the page "
    "actually says - 'read this article', 'summarise this docs page', 'turn this URL into "
    "markdown'. "
    "Do not use: to crawl or mirror a site, to fetch a page behind a login or paywall, or to "
    "read API/RPC endpoints. "
    "Returns: {url, final_url, status, title, content, chars, truncated, fetched_at, source "
    "(direct|provider), cached, took_ms, _metering}. The response is capped at 12000 characters "
    "per call; raise max_chars (up to 20000) when you need more. Reads one public URL only, "
    "respects robots.txt, and does not bypass login or paywall. Direct fetch is tried first; "
    "pages blocked from this node are fetched through a provider and marked source=provider. "
    "Metered: 50 free fetches/day per IP (separate from the risk-tool allowance), then 1 "
    "credit per call over x402 - there is no API key; the 402 answer carries accepts[]."
)
WEB_TOOL_SPECS = [
    ("web_search", _WEB_SEARCH_DESC,
     {"type": "object",
      "properties": {
          "query": {"type": "string", "description": "Search query (1-200 characters)."},
          "count": {"type": "integer", "description": "Number of results to return, 1-10. Default 5."},
          "freshness": {"type": "string", "enum": ["day", "week", "month", "year"],
                        "description": "Optional freshness filter."},
          "site": {"type": "string",
                   "description": "Optional: restrict results to one domain, e.g. github.com."},
          "lang": {"type": "string",
                   "description": "Optional language preference, e.g. zh-cn or en."}},
      "required": ["query"], "additionalProperties": False},
     lambda a: t_web_search(a.get("query"), a.get("count", 5), a.get("freshness"),
                            a.get("site"), a.get("lang"))),
    ("web_fetch", _WEB_FETCH_DESC,
     {"type": "object",
      "properties": {
          "url": {"type": "string", "description": "The http(s) URL to read (max 2048 chars)."},
          "max_chars": {"type": "integer",
                        "description": "Truncate the returned content to this many characters (2000-20000). Default 8000."},
          "format": {"type": "string", "enum": ["markdown", "text"],
                     "description": "Output format. Default markdown."}},
      "required": ["url"], "additionalProperties": False},
     lambda a: t_web_fetch(a.get("url"), a.get("max_chars", 8000), a.get("format", "markdown"))),
]
TOOLS.extend(WEB_TOOL_SPECS)


def _metered_precheck(name, args):
    """计量工具的本地格式校验：失败不进门禁、不扣额度。web 工具做本地校验，其余复用风控校验。"""
    if name in WEB_TOOLS:
        a = args or {}
        if name == "web_search":
            q = str(a.get("query") or "").strip()
            if not q:
                return {"query": "", "count": 0, "results": [], "provider": None,
                        "cached": False, "error": "query_required",
                        "message": "query must be a non-empty string (1-200 characters)"}
            if len(q) > 200:
                return {"query": q[:200], "count": 0, "results": [], "provider": None,
                        "cached": False, "error": "query_too_long",
                        "message": "query must be at most 200 characters"}
        if name == "web_fetch":
            u = str(a.get("url") or "").strip()
            if not u:
                return {"url": "", "error": "invalid_url", "message": "url is required"}
            try:
                p = urllib.parse.urlsplit(u)
                if p.scheme not in ("http", "https"):
                    raise ValueError
            except ValueError:
                return {"url": _url_domain(u), "error": "invalid_url",
                        "message": "only http(s) URLs are supported"}
        return None
    return _risk_precheck(name, args)


def _quota_preview(name):
    if name in WEB_TOOLS:
        ok, used, limit = _web_quota(name, False)
        return used, limit
    _a, used, limit = _risk_quota(False)
    return used, limit


def _tool_output_limit(name):
    if name == "web_fetch":
        # room for 12,000 chars of content + metadata; the structural truncator keeps the
        # JSON valid even if a caller asks for more (ported QA P2-8).
        return max(WEB_TRUNCATE, 16000)
    if name == "web_search":
        return 4096
    return 3800


def _schema_normalize(sch):
    if isinstance(sch, dict) and sch.get("type") == "object" and "additionalProperties" not in sch:
        sch = dict(sch)
        sch["additionalProperties"] = False
    return sch


def _tool_meta(name, sch):
    """Annotations + normalized schema for tools/list (ported QA P2-4)."""
    open_world = (name.startswith("web_") or name.startswith("cyberlibrary_")
                  or name in RISK_TOOLS)
    return {
        "title": name.replace("_", " ").title(),
        "annotations": {"readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": bool(open_world)},
        "inputSchema": _schema_normalize(sch),
    }


def tools_list():
    out = []
    for n, d, sch, _fn in TOOLS:
        row = {"name": n, "description": d}
        row.update(_tool_meta(n, sch))
        out.append(row)
    return out


def call_tool(name, args):
    for n, _d, _s, fn in TOOLS:
        if n == name:
            if n not in METERED_TOOLS:
                return fn(args or {})
            # ---- x402：客户自带付款（信用包）。先入账，再执行工具。----
            # 注意顺序：付款成功才执行；执行失败不退款（客户买的是额度，不是这一次调用）。
            payment = getattr(_REQ, "payment", None)
            if payment and x402_pay is not None and x402_pay.enabled():
                if name in WEB_TOOLS and not _web_rate_ok():
                    raise WebRateLimited("web tools are limited to %d calls/min per IP; "
                                         "retry after 60s" % WEB_RATE_PER_MIN)
                res = x402_pay.try_pay(name, payment)
                if not res.get("ok"):
                    _REQ.x402_result = {"success": False,
                                        "errorReason": res.get("reason") or "invalid_payment",
                                        "network": res.get("network") or "",
                                        "payer": res.get("payer") or "",
                                        "transaction": ""}
                    # 错误码 + 详细原因一起交给 payment_required()：付款方要知道"为什么被拒"
                    raise PaymentRequired(json.dumps({
                        "reason": res.get("reason") or "invalid_payment",
                        "detail": res.get("detail") or "",
                        "network": res.get("network") or "",
                    }, ensure_ascii=False))
                net_used = res.get("network") or x402_pay.cfg().get("network", "")
                # 幂等：同一 payer 在 5 分钟内重复同一查询（典型场景：客户端超时后重试），
                # 直接回上一次的结果，不再扣信用。必须在余额检查之前，否则余额刚好吃到 0
                # 的重试会被误判成"额度用完"。
                _pd_key = (res["payer"], name,
                           json.dumps(args or {}, sort_keys=True, ensure_ascii=False))
                _pd_now = time.time()
                with _paid_dedup_lock:
                    _pd_hit = _paid_dedup.get(_pd_key)
                if _pd_hit and _pd_now - _pd_hit[0] < PAID_DEDUP_TTL:
                    _pd_left = x402_pay.balance(res["payer"])
                    _pd_out = json.loads(json.dumps(_pd_hit[1]))
                    _pd_info = {"payer": res["payer"], "credit_remaining": _pd_left,
                                "creditRemaining": _pd_left, "charged_this_call": False,
                                "deduplicated": True,
                                "first_answered_sec_ago": int(_pd_now - _pd_hit[0]),
                                "how_to_spend_credits": (
                                    "Identical query from the same payer within 5 minutes is answered "
                                    "from cache and does not spend a credit.")}
                    _pd_out["_x402"] = _pd_info
                    _REQ.x402_result = {"success": True, "transaction": res.get("tx", ""),
                                        "network": net_used, "payer": res["payer"],
                                        "credit_remaining": _pd_left, "replayed_payment": True}
                    return _pd_out
                if x402_pay.balance(res["payer"]) <= 0:
                    _REQ.x402_result = {"success": False, "errorReason": "insufficient_credit",
                                        "network": net_used, "payer": res["payer"],
                                        "transaction": res.get("tx", "")}
                    raise PaymentRequired(
                        json.dumps({
                            "reason": "insufficient_credit",
                            "detail": "付款地址 %s 的额度已用完，请购买新的信用包" % res["payer"],
                            "network": net_used,
                        }, ensure_ascii=False))
                # 2026-10-17：先执行工具，成功后才扣 1 次信用；异常或 _no_charge 不扣。
                out = fn(args or {})          # 异常直接上抛，不扣费
                no_charge = isinstance(out, dict) and bool(out.pop("_no_charge", False))
                if no_charge:
                    left = x402_pay.balance(res["payer"])
                else:
                    left = x402_pay.spend(res["payer"], name)
                    if left < 0:                # 并发下余额被抢光：结果仍返回，不倒扣
                        left = 0
                _REQ.x402_result = {"success": True, "transaction": res.get("tx", ""),
                                    "network": net_used,
                                    "payer": res["payer"], "credit_remaining": left, "creditRemaining": left}
                x402info = {"payer": res["payer"], "credit_remaining": left, "creditRemaining": left,
                            "charged_this_call": not no_charge,
                            "settled_tx": res.get("tx", ""),
                            "replayed_payment": bool(res.get("replayed")),
                            "how_to_spend_credits": (
                                "There is no API key. Keep sending this same payment payload in "
                                "params._meta[\"x402/payment\"] with each call: it will not be settled "
                                "again, it only spends one credit per successful call until the balance reaches 0.")}
                _pd_store = json.loads(json.dumps(out)) if isinstance(out, dict) else {"result": out}
                if isinstance(out, dict):
                    out["_x402"] = x402info
                else:
                    out = {"result": out, "_x402": x402info}
                with _paid_dedup_lock:
                    if len(_paid_dedup) >= PAID_DEDUP_MAX:
                        for _k in sorted(_paid_dedup, key=lambda k: _paid_dedup[k][0])[:64]:
                            _paid_dedup.pop(_k, None)
                    _paid_dedup[_pd_key] = (time.time(), _pd_store)
                return out
            pre = _metered_precheck(name, args or {})
            if pre is not None:                      # 本地校验失败：不扣费，也不受额度门禁影响
                _u, _l = _quota_preview(name)
                pre["_metering"] = (_meter_payload(_u, _l, pending=True, charged=False)
                                    if _l >= 0 else _meter_payload(0, -1))
                return pre

            if name in WEB_TOOLS:                    # web 工具：独立额度桶 + 独立限速，不复用风控去重
                return _call_web_tool(name, fn, args or {})

            # 去重按 IP 隔离：旧的全局键让不同 IP 在 5 分钟内重复同一查询都免费，
            # 等于把免费额度放大成"每个热门地址只算一次"。现在同一 IP 的重试仍免费，
            # 换一个 IP 就是独立的一次调用。
            _ip_tag = hashlib.sha256(_client_ip().encode()).hexdigest()[:16]
            dedup_key = (name, _ip_tag, json.dumps(args or {}, sort_keys=True, ensure_ascii=False))
            now_ts = time.time()
            with _risk_dedup_lock:
                _hit = _risk_dedup.get(dedup_key)
            _age = (now_ts - _hit[0]) if _hit else None
            if _hit and _age is not None and _age < RISK_DEDUP_TTL:
                # 同一次查询的重复提交（客户端超时后重试）：回同一份结论，**不再扣费**
                _a, _u, _l = _risk_quota(False)
                _m = (_meter_payload(_u, _l, pending=True, charged=False)
                      if _l >= 0 else _meter_payload(0, -1))
                if _l >= 0:
                    _m["free_tier"]["deduplicated"] = True
                    _m["free_tier"]["first_answered_sec_ago"] = int(_age)
                _cached = dict(_hit[1])
                _cached["_repeat_of_recent_call"] = True
                _cached["_metering"] = _m
                return _cached

            gate = _risk_gate()                      # 额度用尽则抛 PaymentRequired，不消耗上游
            tier = gate.get("free_tier") or {}
            used_before, limit = tier.get("used_today", 0), tier.get("limit_per_day", -1)
            out = fn(args or {})
            charged = True
            if isinstance(out, dict) and out.pop("_no_charge", False):
                charged = False
                meter = (_meter_payload(used_before, limit, pending=True, charged=False)
                         if limit >= 0 else _meter_payload(0, -1))
            else:
                meter = _risk_commit(used_before, limit)
                if limit >= 0:
                    meter["free_tier"]["charged_this_call"] = True
            if charged:
                # 只把「扣过费的真实结论」放进去重缓存：重试时可以直接复用
                _snap = dict(out) if isinstance(out, dict) else {"result": out}
                _snap.pop("_metering", None)
                with _risk_dedup_lock:
                    if len(_risk_dedup) >= RISK_DEDUP_MAX:
                        for _k in sorted(_risk_dedup, key=lambda k: _risk_dedup[k][0])[:64]:
                            _risk_dedup.pop(_k, None)
                    _risk_dedup[dedup_key] = (now_ts, _snap)
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
        # 握手是唯一能拿到客户端自报名的地方，记进审计 + journald，之后按名字分组。
        ci = params.get("clientInfo") or {}
        cname = str(ci.get("name") or "").strip()
        cver = str(ci.get("version") or "").strip()
        if cname:
            _REQ.client = (cname + ("/" + cver if cver else ""))[:80]
            print("[mcp] initialize client=%s proto=%s ua=%r" % (
                _REQ.client, params.get("protocolVersion"), getattr(_REQ, "ua", "")[:90]), flush=True)
            log_client(_client_ip(), _REQ.client, getattr(_REQ, "ua", ""), params.get("protocolVersion"))
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": ("RobotBase is the semantic verification and risk-intelligence surface for "
                             "Web3 agents. Call list_chains or robotbase_capabilities first to see which "
                             "chains are live today (BTC/ETH/XMR/ZEC/TRON) and what each node can and "
                             "cannot answer. Every tool is a read-only query: none of them sign, trade, "
                             "move funds or hold keys, and RobotBase operates no mining pool. For payment "
                             "safety use tron_check_address_risk before accepting USDT and "
                             "tron_verify_usdt_transfer before releasing value. Those two risk tools are "
                             "metered: a free daily allowance per IP, then 0.01 USDC per call paid over "
                             "x402 by the caller's own wallet - there is no API key, no account and no "
                             "subscription of any kind. x402 settles in USDC on Base or Solana, but that "
                             "is a payment rail only: Base and Solana data tools were retired on "
                             "2026-10-18 and no Base or Solana chain data is served. When the allowance "
                             "runs out the tool answers Payment Required with the machine-readable terms "
                             "in structuredContent.accepts[]; settle a credit pack once and keep sending "
                             "the same payment payload so it decrements credit instead of charging "
                             "again. Every answer carries its own _metering block. Two high-frequency "
                             "web tools are exposed on the same endpoint: web_search returns ranked web "
                             "results (provider serper, with tavily/brave/bing_rss fallbacks; cached up "
                             "to 10 minutes) and web_fetch returns readable markdown/text of one public "
                             "URL (robots.txt-respecting, SSRF-guarded, no login or paywall bypass, capped "
                             "at 12000 characters per call). They have their own free daily allowance per "
                             "IP (web_search 20, web_fetch 50, reset 00:00 Asia/Shanghai, separate from "
                             "the risk allowance) and are metered at 1 credit each. Metering is safe to retry: "
                             "repeating the exact same risk query within 5 minutes is answered from cache "
                             "with charged_this_call=false, and a locally invalid argument is rejected "
                             "without consuming a free call.")}}
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
        # x402 付款载荷按 MCP 传输规范放在 params._meta["x402/payment"]
        _REQ.payment = (params.get("_meta") or {}).get("x402/payment") or None
        _REQ.x402_result = None
        try:
            out = call_tool(name, params.get("arguments") or {})
            # 2026-10-16：核验类工具的成功结果落台账（付款事实，供可信度评分用）。
            # 只做加法：任何异常都吞掉，绝不改变工具返回。
            if verify_ledger is not None:
                try:
                    verify_ledger.record(name, params.get('arguments') or {}, out)
                except Exception:  # noqa: BLE001
                    pass
            # 2026-10-19 (ported QA P1-10): one consistent rule for every tool: a payload
            # carrying `error` (or ok=false from a node/contract read) is an error.
            _is_err = False
            if isinstance(out, dict):
                if out.get("error"):
                    _is_err = True
                if out.get("ok") is False:
                    _is_err = True
            elif isinstance(out, str) and out.strip().lower().startswith("error:"):
                _is_err = True
            if _is_err:
                _REQ.err = str(out.get("error"))
            result = {"content": [{"type": "text", "text": truncate(out, _tool_output_limit(name))}], "isError": _is_err}
            if _REQ.x402_result:      # 结算成功：按规范回 payment-response
                result["_meta"] = {"x402/payment-response": _REQ.x402_result}
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except PaymentRequired as pay:
            _REQ.err = "payment_required: %s" % pay
            # 有 x402 能力时返回规范形状（structuredContent + content[0].text 完全一致），
            # 否则退回旧的 JSON 形状，保证老客户端不受影响。
            if x402_pay is not None and x402_pay.enabled():
                _pr = x402_pay.payment_required(name, str(pay))
                if _REQ.x402_result:      # 付款失败也回 payment-response（success=false）
                    _pr["_meta"] = {"x402/payment-response": _REQ.x402_result}
                return {"jsonrpc": "2.0", "id": mid, "result": _pr}
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(
                    {"error": "payment_required", "tool": name, "message": str(pay),
                     "free_per_day": RISK_FREE_PER_DAY, "top_up": TOPUP_URL},
                    ensure_ascii=False)}],
                "isError": True}}
        except WebRateLimited as rl:
            _REQ.err = "rate_limited: web tools"
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(
                    {"error": "rate_limited", "message": str(rl), "retry_after_sec": 60})}],
                "isError": True}}
        except Exception as exc:  # noqa: BLE001
            _REQ.err = "%s: %s" % (type(exc).__name__, exc)
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"error: {exc}"[:300]}],
                                                            "isError": True}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}


DOC_TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RobotBase MCP · the semantic risk &amp; verification layer for Web3 agents</title>
<meta name="description" content="One read-only MCP endpoint where an autonomous agent verifies that an on-chain payment settled and prices the next one. Bitcoin, Ethereum, Monero, Zcash and Tron are live, read from nodes we run ourselves. No public RPC, no custody, no tracking.">
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
</style>
<!-- Google tag (gtag.js) — 流量监控 -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-KQFLDYBGGC"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', 'G-KQFLDYBGGC');
</script>
</head><body><div class="wrap">
<h1>RobotBase MCP Server</h1>
<div class="tag">Read-only MCP · BTC / ETH / XMR / ZEC / TRON live · no RPC endpoint · no custody</div>
<div class="sub">RobotBase is the semantic risk and verification layer for Web3 agents: one endpoint where an autonomous agent checks whether a payment actually settled on Bitcoin, prices the next transaction, reads the Zcash shielded-pool distribution, screens a TRON address or confirms a USDT-TRC20 transfer, and verifies that a node really is synced - straight from bare-metal nodes we operate ourselves. Five chains are live: Bitcoin, Ethereum, Monero, Zcash and Tron; Tron runs on a self-hosted lite full node (current state plus the most recent ~6 days) and says so rather than guessing. Base and Solana data tools were retired on 2026-10-18, so the five-chain surface is what this endpoint serves. RobotBase publishes no RPC endpoint of any kind: this MCP surface is the only read path. Every tool is read-only, none touch trading or funds, and RobotBase operates no mining pool.</div>

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
                      gateway (/mcp reverse proxy) ──► MCP tool service
                        ├── BTC   full node RPC + electrs (address/tx index)
                        ├── ETH   Reth execution + Lighthouse consensus
                        ├── XMR   monerod full node
                        ├── ZEC   Zebra full node (incl. value-pool / shielded supply)
                        ├── TRON  java-tron lite full node (tip state + a recent ~6 day window)
                        └── gateway service aggregation (status pages / dashboards)
                        └── web : Serper / Tavily / Brave / cn.bing RSS search,
                                  direct fetch + Tavily /extract fallback (SSRF-guarded)</pre></div>

<h2>Rate limits and privacy</h2>
<div class="card"><div class="grid">
<div><div class="muted">Verification tools</div><div><b>Free</b> - no key, no signup</div></div>
<div><div class="muted">Risk tools</div><div><b>5 free/day per IP</b>, then <b>$0.01</b> per call by x402</div></div>
<div><div class="muted">Web tools</div><div><b>20 searches + 50 fetches/day per IP</b> free (separate buckets); <b>20 req/min/IP</b>; then <b>$0.01</b> per call by x402</div></div>
<div><div class="muted">Payment</div><div>USDC on Base or Solana via x402 - <b>payment rail only, no Base/Solana chain data</b>; no API key, no subscription</div></div>
<div><div class="muted">Anonymous</div><div><b>120 req/min/IP</b></div></div>
<div><div class="muted">Privacy</div><div>Audit keeps only the first 16 chars of the IP SHA256 — <b>query arguments are never logged</b></div></div>
<div><div class="muted">Data</div><div>Read-only on-chain data; no trading, signing or custody</div></div>
</div></div>

<h2>Risk intelligence (metered)</h2>
<div class="card">
  <p class="muted" style="margin-top:0">Two tools answer questions a free block explorer cannot:
  is this counterparty tainted, and did the USDT really arrive irreversibly. They are
  aggregations over public datasets (GoPlus Labs + TronGrid), read-only, no keys, no custody.</p>
<ul style="margin:0;padding-left:20px">
<li><code>tron_check_address_risk</code> - judicial / black-market labels, account age, 0-100 safety score</li>
<li><code>tron_verify_usdt_transfer</code> - finalized?, amount, sender, recipient, matched against what you expect</li>
</ul>
  <p class="muted" style="margin-bottom:0">Free allowance: <b>__RISK_FREE__ calls per day per IP</b>. After that the
  tool answers <code>Payment Required</code> with machine-readable terms: 0.01 USDC per call, USDC on
  Base or Solana, paid straight from your agent's wallet. There is no API key to buy - settle a credit
  pack once and keep sending the same payment payload so it decrements credit instead of charging again.
  Payment settles in USDC on Base or Solana via x402; RobotBase does not serve Base or Solana chain data.</p>
</div>

<h2>Web tools (high-frequency)</h2>
<div class="card">
  <p class="muted" style="margin-top:0">Two generic web tools give an agent current facts that
  are not on chain - same endpoint, same x402 flow:</p>
<ul style="margin:0;padding-left:20px">
<li><code>web_search</code> - ranked web results (title, URL, snippet, published date); Serper-backed
with Tavily / Brave / cn.bing RSS fallbacks; results may be cached for up to 10 minutes</li>
<li><code>web_fetch</code> - readable text/markdown of <b>one</b> public URL; respects robots.txt,
no login/paywall bypass; direct fetch first, provider fallback for pages blocked from this node;
response capped at 12000 characters</li>
</ul>
  <p class="muted" style="margin-bottom:0">Independent free allowance: <b>20 searches and 50 fetches
  per day per IP</b> (separate from the risk-tool allowance), then <b>$0.01 per call</b> by x402.
  A cached search still counts as one call against the free allowance; a paid cache hit does not
  spend a credit. All fetching is SSRF-guarded: private, loopback, link-local and cloud-metadata
  addresses are rejected, redirects are re-checked hop by hop, and responses are capped at 2 MB.</p>
</div>

<h2 id="pricing">Pricing</h2>
<div class="card"><div class="grid">
<div><div class="muted">Verification tools (41)</div><div><b>$0</b> — free forever, no key, no signup</div></div>
<div><div class="muted">Risk intelligence (2 tools)</div><div><b>__RISK_FREE__ calls/day per IP free</b>, then <b>$0.01</b> per call</div></div>
<div><div class="muted">Web tools (2 tools)</div><div><b>20 searches + 50 fetches/day per IP free</b> (separate allowance), then <b>$0.01</b> per call</div></div>
<div><div class="muted">How you pay</div><div><b>x402</b> — USDC on Base or Solana, paid by the caller's own wallet; <b>payment rail only</b>, no Base/Solana chain data</div></div>
<div><div class="muted">Credit packs</div><div><b>10 calls / 0.10 USDC</b> · <b>100 calls / 1.00 USDC</b> — live terms in <code>accepts[]</code></div></div>
</div>
<p class="muted" style="margin-bottom:0"><b>No API keys are issued by robotbase.cc</b> — no key,
no account, no subscription. Payment is per call, bought in prepaid credit packs; the authoritative
pack sizes, assets and receiving addresses are the live ones in <code>structuredContent.accepts[]</code>
of any 402 answer. Nothing is metered by surprise: every risk-tool answer carries its own
<code>_metering</code> block with the allowance and the reset time (00:00 Asia/Shanghai).</p>
</div>

<h2>Example prompts</h2>
<div class="card"><ul style="margin:0;padding-left:20px">
<li>"What is the cheapest BTC fee right now?" → <code>btc_get_fee_rates</code></li>
<li>"Did this txid actually confirm, and in which block?" → <code>btc_get_transaction</code></li>
<li>"How much ZEC is in the Zcash shielded pools?" → <code>zec_get_pool_status</code></li>
<li>"Is the Monero node synced, and how deep is its txpool?" → <code>xmr_get_node_status</code></li>
<li>"What is the BTC fee market saying today?" → <code>web_search</code>, then <code>web_fetch</code> on the best result</li>
<li>"Turn this article URL into clean markdown" → <code>web_fetch</code></li>
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
                                                               "btc_get_transaction", "btc_get_block", "btc_get_address",
                                                               "btc_get_network_stats", "broadcast_raw_transaction"]),
        ("③ Ethereum (ETH) — settlement, fees and contract reads", ["eth_get_transaction", "eth_get_fee_rates",
                                                                     "eth_get_account", "eth_read_contract",
                                                                     "eth_get_block", "eth_get_logs"]),
        ("④ Zcash (ZEC) — shielded pools & settlement", ["zec_get_pool_status", "zec_get_recent_blocks",
                                                          "zec_get_transaction", "zec_get_treestate",
                                                          "zec_get_network_stats"]),
        ("⑤ Monero (XMR) — node, fee, transaction and payment proof", ["xmr_get_node_status", "xmr_get_fee_rates",
                                                                        "xmr_get_mempool_state", "xmr_get_block",
                                                                        "xmr_get_recent_blocks", "xmr_get_transaction",
                                                                        "xmr_get_network_stats", "xmr_verify_payment",
                                                                        "xmr_get_output_distribution"]),
        ("⑥ Tron — node, contract, history and governance", ["tron_get_node_status", "tron_get_current_block",
                                                              "tron_get_block", "tron_get_transaction",
                                                              "tron_get_account", "tron_get_contract",
                                                              "tron_read_contract", "tron_get_chain_economics",
                                                              "tron_list_witnesses", "tron_list_proposals"]),
        ("⑦ Risk intelligence — metered", ["tron_check_address_risk", "tron_verify_usdt_transfer"]),
        ("⑧ Web tools (high-frequency)", ["web_search", "web_fetch"]),
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
        "title": "RobotBase MCP Server - search, read, verify and pay from one read-only endpoint",
        "version": SERVER_INFO["version"],
        "description": "One read-only MCP endpoint for agents to search the web, read any page, verify "
                       "on-chain payments and pay per call - no API keys, no accounts, no custody. "
                       "Five chains are live, each read from mainnet nodes we run ourselves: "
                       "Bitcoin (fees, mempool state with a fee-rate histogram and RBF/CPFP feasibility, "
                       "tx/block/address verification, optional raw-tx relay); Ethereum (Reth execution + "
                       "Lighthouse consensus, settlement checks with ERC-20 log decoding, fee market, "
                       "blocks, event logs, account and contract reads); Monero (node telemetry, dynamic "
                       "fees, mempool, blocks, transaction lookup, ring-size output distribution, and "
                       "cryptographic tx-proof payment verification); Zcash (all six shielded pools, "
                       "shielded commitment-tree anchors, transaction checks, network and funding-stream "
                       "stats); and Tron (node status, blocks and history, transactions with TRC-20 "
                       "decoding, account permissions, contract reads, chain economics, super "
                       "representatives and governance proposals). Risk intelligence is light aggregation "
                       "over public datasets: TRON address risk and TRC-20 USDT settlement verification. "
                       "RobotBase publishes no RPC endpoint of any kind. Verification tools are free; "
                       "risk tools include a daily free allowance, then 0.01 USDC per call settled over "
                       "x402 by the caller's own wallet - x402 runs on Base or Solana as a payment rail "
                       "only, and no Base or Solana chain data is served. Ten web tools are "
                       "exposed on the hosted endpoint: web_search returns ranked web results "
                       "(self-hosted SearXNG primary with Serper/Tavily/Brave/cn.bing RSS fallbacks, "
                       "cached up to 10 minutes) and web_fetch returns readable markdown/text of one "
                       "public URL with optional OCR (robots.txt-respecting, SSRF-guarded, no login "
                       "or paywall bypass, provider fallback for pages blocked from this node, capped "
                       "at 12000 characters); web_screenshot, web_map, web_fetch_batch, web_feed, "
                       "web_metadata, web_crawl, web_extract and web_act add screenshots, URL "
                       "discovery, batch reads, feeds, metadata, same-host crawling, structured "
                       "extraction and limited browser interaction. Web tools share a free daily "
                       "allowance per IP (search 20, fetch-family 50) and are metered from 1 to 3 "
                       "x402 credits per call. The hosted endpoint adds sixteen CyberLibrary book "
                       "tools, for 72 hosted tools in total; this repository is the 45-tool "
                       "reference implementation. No API key, no account and no subscription are "
                       "offered.",
        "homepage": "https://robotbase.cc/mcp",
        "transport": {"type": "streamable-http", "url": "https://robotbase.cc/mcp"},
        "endpoints": {
            "tools": "https://robotbase.cc/mcp/tools",
            "stats": "https://robotbase.cc/mcp/stats?hours=24",
            "credits": "https://robotbase.cc/mcp/credits?payer=<address>",
        },
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "auth": {"type": "none", "optional": "x402 (USDC) for metered tools; no API keys are issued"},
        "rateLimits": {"anonymous": "120/min per IP",
                       "web_tools": "20/min per IP (separate from the 120/min overall limit)",
                       "note": "no keyed tier exists"},
        "tags": ["bitcoin", "ethereum", "monero", "zcash", "tron", "blockchain-data",
                 "onchain", "verification", "risk-intelligence", "aml", "rug-pull", "honeypot", "evm",
                 "usdt", "mempool",
                 "fees", "privacy-coins", "read-only", "mcp-server", "web3-agents",
                 "web-search", "web-fetch", "search", "fetch", "web-screenshot", "web-map",
                 "web-crawl", "rss", "books", "x402"],
        "pricing": {"verification_tools": "free", "risk_tools": "%d free calls/day per IP, then 0.01 USDC per call" % RISK_FREE_PER_DAY,
                    "web_tools": "20 searches + 50 fetches/day per IP free (separate buckets), then 0.01 USDC per credit over x402 (web_search 1, web_fetch 1)",
                    "payment": "x402 (HTTP 402) in USDC on Base or Solana; credit packs 10 calls / 0.10 USDC and 100 calls / 1.00 USDC; payment rail only - no Base or Solana chain data",
                    "api_key": "not offered", "subscription": "not offered",
                    "top_up": TOPUP_URL},
        "tools": [{"name": n, "description": desc[n]} for n, _d, _s, _f in TOOLS],
    }


# ============================================================ 2026-09-27 三链扩展（BTC/XMR/ZEC）
XMR_HELPER = os.environ.get("RB_XMR_HELPER", "http://127.0.0.1:18085/rpc")
XMR_ATOMIC = 1e12                     # 1 XMR = 10^12 原子单位
XMR_FEE_PRIORITY_NAMES = ("default", "slow", "normal", "fast", "fastest")


def _xmr_rpc(method, params=None, timeout=20):
    """经只读 helper 调 monerod。helper 只放行白名单里的只读方法。"""
    d = _http_json(XMR_HELPER, {"method": method, "params": params or {}}, timeout=timeout)
    return (d or {}).get("result")


def _xmr(h):
    """把原子单位换算成 XMR（保留 12 位小数，去掉浮点噪声）。"""
    try:
        return round(int(h) / XMR_ATOMIC, 12)
    except (TypeError, ValueError):
        return None


def _xmr_tip():
    """返回 (末块高度, 末块头)。

    ⚠️ monerod 的 `get_info.height` 与 `get_block_count.count` 返回的是**链长度**，
    实测恒等于 `get_last_block_header.block_header.height + 1`（那个高度上的区块并不存在）。
    对外报高度必须用区块头，否则会永远偏大 1。
    """
    lb = _xmr_rpc("get_last_block_header")
    hdr = (lb or {}).get("block_header") or {}
    if hdr.get("height") is not None:
        return int(hdr["height"]), hdr
    info = _xmr_rpc("get_info") or {}
    h = info.get("height")
    return (int(h) - 1 if h else None), {}


def t_xmr_fee_rates():
    """Monero 动态手续费：按优先级分档，直接来自我们自己的 monerod。"""
    r = cached("xmr_fee", 15, lambda: _xmr_rpc("get_fee_estimate"))
    if not r:
        return {"available": False, "reason": "monerod helper RPC unreachable"}
    fees = r.get("fees") or ([r["fee"]] if r.get("fee") else [])
    tiers = []
    for i, f in enumerate(fees):
        tiers.append({"priority": XMR_FEE_PRIORITY_NAMES[i] if i < len(XMR_FEE_PRIORITY_NAMES)
                      else "tier_%d" % (i + 1),
                      "fee_per_kb_atomic": f, "fee_per_kb_xmr": _xmr(f)})
    return {
        "baseline_fee_per_kb_atomic": r.get("fee"),
        "baseline_fee_per_kb_xmr": _xmr(r.get("fee")),
        "quantization_mask": r.get("quantization_mask"),
        "priority_tiers": tiers,
        "how_to_read": ("tiers are in ascending priority order; a transaction pays "
                        "fee_per_kb x its size in kB. Monero has no EIP-1559 style fee market - "
                        "the median block is far below the size limit, so the baseline tier "
                        "normally confirms in the next block."),
        "network": "mainnet",
        "source": "our monerod get_fee_estimate (read-only helper)",
    }


def t_xmr_mempool_state():
    """Monero 内存池状态：待确认交易、字节量、手续费总量、积压时间。"""
    raw = cached("xmr_pool", 10, lambda: _xmr_rpc("get_transaction_pool_stats"))
    if not raw:
        return {"available": False, "reason": "monerod helper RPC unreachable"}
    # legacy 端点的负载嵌在 pool_stats 下
    r = raw.get("pool_stats") or raw
    txs = r.get("txs_total") or 0
    oldest = r.get("oldest")
    age = int(time.time() - oldest) if oldest else None
    return {
        "pending_txs": txs,
        "bytes_total": r.get("bytes_total"),
        "fee_total_atomic": r.get("fee_total"),
        "fee_total_xmr": _xmr(r.get("fee_total")),
        "oldest_pending_age_s": age,
        "double_spends": r.get("num_double_spends"),
        "failing": r.get("num_failing"),
        "not_relayed": r.get("num_not_relayed"),
        "tx_size_median_bytes": r.get("bytes_med"),
        "tx_size_max_bytes": r.get("bytes_max"),
        "congestion": ("clear" if txs < 50 else "busy" if txs < 500 else "heavy"),
        "how_to_read": ("Monero blocks are effectively never full, so a non-zero pool usually means "
                        "transactions waiting for the next 2-minute block, not a backlog."),
        "source": "our monerod get_transaction_pool_stats (read-only helper)",
    }


def t_xmr_block(height=None, block_hash=None):
    """按高度或哈希取一个 Monero 区块头。"""
    if block_hash:
        r = _xmr_rpc("get_block_header_by_hash", {"hash": str(block_hash).strip()})
    else:
        try:
            h = int(height)
        except (TypeError, ValueError):
            return {"error": "provide either height (integer) or block_hash (64 hex chars)"}
        r = _xmr_rpc("get_block_header_by_height", {"height": h})
    b = (r or {}).get("block_header") or {}
    if not b:
        return {"found": False, "reason": "no block header returned"}
    return {
        "found": True,
        "height": b.get("height"), "hash": b.get("hash"), "prev_hash": b.get("prev_hash"),
        "timestamp": b.get("timestamp"),
        "age_s": int(time.time() - b["timestamp"]) if b.get("timestamp") else None,
        "tx_count": b.get("num_txes"), "reward_xmr": _xmr(b.get("reward")),
        "difficulty": b.get("difficulty"), "block_weight": b.get("block_weight"),
        "block_size_bytes": b.get("block_size"),
        "major_version": b.get("major_version"), "minor_version": b.get("minor_version"),
        "orphan": bool(b.get("orphan_status")),
        "source": "our monerod block header (read-only helper)",
    }


def t_xmr_recent_blocks(limit=10):
    """最近 N 个 Monero 区块头，用于看出块是否正常。"""
    try:
        n = max(1, min(50, int(limit or 10)))
    except (TypeError, ValueError):
        n = 10
    tip, _hdr = cached("xmr_tip", 10, _xmr_tip)
    if not tip:
        return {"available": False, "reason": "monerod helper RPC unreachable"}
    r = _xmr_rpc("get_block_headers_range",
                 {"start_height": max(0, int(tip) - n + 1), "end_height": int(tip)})
    hs = (r or {}).get("headers") or []
    out = [{"height": b.get("height"), "hash": b.get("hash"), "timestamp": b.get("timestamp"),
            "tx_count": b.get("num_txes"), "reward_xmr": _xmr(b.get("reward")),
            "difficulty": b.get("difficulty"), "weight": b.get("block_weight")} for b in hs]
    gaps = []
    for a, b in zip(hs, hs[1:]):
        if a.get("timestamp") and b.get("timestamp"):
            gaps.append(b["timestamp"] - a["timestamp"])
    return {
        "tip_height": tip, "requested": n, "returned": len(out),
        "avg_block_interval_s": round(sum(gaps) / len(gaps)) if gaps else None,
        "target_interval_s": 120,
        "blocks": list(reversed(out))[:n],
        "source": "our monerod get_block_headers_range (read-only helper)",
    }


def t_xmr_transaction(txid):
    """按 txid 查一笔 Monero 交易是否上链、进了哪个块、有多少确认。

    刻意不返回金额与收发双方——那不是我们查不到，而是 Monero 协议本身就不公开。
    """
    t = str(txid or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", t):
        return {"error": "not a Monero transaction id (expected 64 hex chars)"}
    r = _xmr_rpc("get_transactions", {"txs_hashes": [t], "decode_as_json": True})
    txs = (r or {}).get("txs") or []
    if not txs:
        return {"found": False, "txid": t,
                "note": "not found on the canonical chain or in our mempool"}
    x = txs[0]
    js = x.get("as_json")
    try:
        j = json.loads(js) if isinstance(js, str) else (js or {})
    except Exception:  # noqa: BLE001
        j = {}
    vin = j.get("vin") or []
    ring = None
    if vin and isinstance(vin[0], dict) and isinstance(vin[0].get("key"), dict):
        ring = len(vin[0]["key"].get("key_offsets") or []) or None
    return {
        "found": True, "txid": x.get("tx_hash") or t,
        "confirmed": not x.get("in_pool"),
        "in_mempool": bool(x.get("in_pool")),
        "block_height": x.get("block_height"),
        "block_timestamp": x.get("block_timestamp"),
        "confirmations": x.get("confirmations"),
        "double_spend_seen": bool(x.get("double_spend_seen")),
        "size_bytes": len(x.get("as_hex") or "") // 2 or None,
        "inputs": len(vin), "outputs": len(j.get("vout") or []),
        "ring_size_per_input": ring,
        "tx_version": j.get("version"), "unlock_time": j.get("unlock_time"),
        "not_disclosed_by_protocol": ["amount", "sender address", "recipient address", "ring members"],
        "how_to_read": ("Monero hides amount and both parties by design; only the recipient (or someone "
                        "given the transaction key) can prove what was paid. This tool answers "
                        "'is it mined and how deep', not 'how much and to whom'."),
        "source": "our monerod get_transactions (read-only helper)",
    }


def t_xmr_network_stats():
    """Monero 全网状态：算力、发行量、协议版本与下次硬分叉。"""
    info = cached("xmr_info", 10, lambda: _xmr_rpc("get_info")) or {}
    hf = cached("xmr_hf", 60, lambda: _xmr_rpc("hard_fork_info")) or {}
    if not info:
        return {"available": False, "reason": "monerod helper RPC unreachable"}
    height, _hdr = _xmr_tip()
    height = int(height or 0)
    emission = cached("xmr_emission", 300, lambda: _xmr_rpc(
        "get_coinbase_tx_sum", {"height": max(0, height - 720), "count": 720})) or {}
    diff = info.get("difficulty") or 0
    return {
        "height": height,
        "network_hashrate_hs": round(diff / 120) if diff else None,   # 目标出块 120 秒
        "difficulty": diff,
        "block_size_median": info.get("block_size_median"),
        "block_size_limit": info.get("block_size_limit"),
        "block_weight_median": info.get("block_weight_median"),
        "block_weight_limit": info.get("block_weight_limit"),
        "emission_last_720_blocks_xmr": _xmr(emission.get("emission_amount")),
        "fees_last_720_blocks_xmr": _xmr(emission.get("fee_amount")),
        "hard_fork_version": hf.get("version"),
        "hard_fork_voting": hf.get("voting"),
        "next_fork_earliest_height": hf.get("earliest_height"),
        "fork_window": hf.get("window"),
        "client_version": info.get("version"),
        "mainnet": bool(info.get("mainnet")),
        "source": "our monerod get_info + hard_fork_info + get_coinbase_tx_sum",
    }


def t_xmr_node_status():
    """Monero node state straight from our monerod helper RPC."""
    # 注意：缓存键 "xmr_info" 存的是 _xmr_rpc 的结果本体（不是 helper 的外层封装），
    # t_xmr_network_stats 复用同一个键，两边形状必须一致，否则会互相污染。
    r = cached("xmr_info", 10, lambda: _xmr_rpc("get_info")) or {}
    if not r:
        return {"available": False, "reason": "monerod helper RPC unreachable"}
    tip, hdr = cached("xmr_tip", 10, _xmr_tip)
    return {
        # 对外一律报真实末块高度；monerod 的 self-reported height 是链长度（+1）
        "height": tip if tip else r.get("height"),
        "height_reported_by_get_info": r.get("height"),
        "top_block_hash": hdr.get("hash"),
        "target_height": r.get("target_height"),
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


# ---------- XMR tx-proof 付款验证（RobotBase 自己的钱包 RPC，与 xmr.pub 实例完全分离）----------
# 服务：9181 上 robotbase-xmr-wallet-rpc.service，独立端口 18091、独立凭据、独立空钱包。
# 它只做一件事：按收款方提供的 tx key / tx proof 做密码学校验，不托管资金、不持有收款密钥。
#
# ⚠️ 两个必须记住的实现约束（都是踩过的坑）：
#   1. monero-wallet-rpc 用 **HTTP Digest**，不是 Basic —— 用 Basic 永远 401。
#   2. 它的 digest nonce **绑定在 TCP 连接上**：必须"同一条连接"先拿 401 挑战、
#      再在同一连接上发认证请求。用 urllib 那种"发一次失败、再新开连接重试"的做法
#      即使凭据完全正确也会一直 401。所以这里用 http.client.HTTPConnection 复用连接。
XMR_WALLET_CFG = os.environ.get("RB_XMR_WALLET_CFG", "")


def _xmr_wallet_cfg():
    try:
        with open(XMR_WALLET_CFG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"enabled": False}


def _xmr_wallet_rpc(method, params=None, cfg=None):
    """向 RobotBase 自己的 monero-wallet-rpc 发一次 JSON-RPC（自动完成 Digest 握手）。"""
    cfg = cfg or _xmr_wallet_cfg()
    if not cfg.get("enabled"):
        raise RuntimeError("xmr wallet rpc not configured")
    u = urllib.parse.urlparse(cfg["url"])
    host, port, path = u.hostname, u.port or 80, u.path or "/json_rpc"
    user, pw = cfg["user"], cfg["password"]
    body = json.dumps({"jsonrpc": "2.0", "id": "0", "method": method,
                       "params": params or {}}).encode()
    conn = http.client.HTTPConnection(host, port, timeout=int(cfg.get("timeout", 30)))
    try:
        # 第一次：拿 401 挑战（nonce 与这条连接绑定）
        conn.request("POST", path, body, {"Content-Type": "application/json",
                                          "Content-Length": str(len(body))})
        r = conn.getresponse()
        r.read()
        if r.status == 200:                     # 未启用鉴权时直接返回
            pass
        chal = r.getheader("WWW-Authenticate") or ""
        if r.status != 401:
            raise RuntimeError("wallet rpc unexpected status %s" % r.status)
        m = re.search(r'realm="?([^",]+)"?', chal)
        n = re.search(r'nonce="?([^",]+)"?', chal)
        q = re.search(r'qop="?([^",]+)"?', chal)
        realm, nonce, qop = (m.group(1) if m else ""), (n.group(1) if n else ""), (q.group(1) if q else "auth")
        ha1 = hashlib.md5(("%s:%s:%s" % (user, realm, pw)).encode()).hexdigest()
        ha2 = hashlib.md5(("POST:%s" % path).encode()).hexdigest()
        cnonce = hashlib.sha1(os.urandom(16)).hexdigest()[:16]
        nc = "00000001"
        resp = hashlib.md5(("%s:%s:%s:%s:%s:%s" % (ha1, nonce, nc, cnonce, qop, ha2)).encode()).hexdigest()
        auth = ('Digest username="%s", realm="%s", nonce="%s", uri="%s", cnonce="%s", nc=%s, '
                'qop=%s, response="%s", algorithm=MD5') % (user, realm, nonce, path, cnonce, nc, qop, resp)
        # 第二次：**同一条连接**发认证请求
        conn.request("POST", path, body, {"Content-Type": "application/json",
                                          "Authorization": auth,
                                          "Content-Length": str(len(body))})
        r2 = conn.getresponse()
        raw = r2.read()
        if r2.status != 200:
            raise RuntimeError("wallet rpc auth failed (HTTP %s)" % r2.status)
        d = json.loads(raw or b"{}")
        if d.get("error"):
            raise RuntimeError(str(d["error"].get("message") or d["error"])[:200])
        return d.get("result") or {}
    finally:
        conn.close()


def t_xmr_verify_payment(txid, address, tx_key=None, signature=None, message=None):
    """用收款方提供的 tx key 或 tx proof，校验一笔 Monero 付款。

    这不是"我们索引了门罗链"——门罗的金额与收发双方在协议层就不可见。
    这里做的是：收款方把只有他能提供的凭据交给我们，我们用密码学替他验一次。
    """
    t = str(txid or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", t):
        return {"error": "not a Monero transaction id (expected 64 hex chars)"}
    addr = str(address or "").strip()
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{95,106}", addr):
        return {"error": "not a Monero address (expected a 95-106 char base58 address)"}
    if not (tx_key or signature):
        return {"error": "provide either tx_key (from your own wallet) or a tx proof signature",
                "how_to": ("Receive with Monero: in your wallet run 'get_tx_key <txid>' to obtain the tx key, "
                           "or 'get_tx_proof <txid> <address>' to obtain a signature. Both are safe to share - "
                           "they prove this one payment and cannot be used to spend or to scan other payments.")}
    try:
        if tx_key:
            r = _xmr_wallet_rpc("check_tx_key", {"txid": t, "tx_key": str(tx_key).strip(), "address": addr})
            mode = "check_tx_key"
        else:
            p = {"txid": t, "address": addr, "signature": str(signature).strip()}
            if message:
                p["message"] = str(message)
            r = _xmr_wallet_rpc("check_tx_proof", p)
            mode = "check_tx_proof"
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        low = msg.lower()
        # 2026-10-19 (ported QA P2-13): a rejected proof is not "wallet rpc unavailable".
        if any(k in low for k in ("tx key", "tx_key", "signature", "proof", "wrong", "mismatch",
                                  "invalid", "failed")):
            return {"ok": False, "verified": False, "txid": t, "address": addr,
                    "error": "proof_check_failed",
                    "detail": msg[:200],
                    "how_to_read": ("The transaction key / proof does not match this transaction and "
                                    "address. Re-run get_tx_key / get_tx_proof for THIS txid and address.")}
        return {"ok": False, "txid": t, "error": "wallet rpc unavailable: %s" % msg[:160]}
    conf = r.get("confirmations")
    received = r.get("received")
    good = isinstance(conf, int) and conf > 0 and isinstance(received, int) and received > 0
    out = {
        "ok": True, "verified": bool(good), "mode": mode, "txid": t, "address": addr,
        "confirmations": conf,
        "received_atomic": received,
        "received_xmr": round(received / XMR_ATOMIC, 12) if isinstance(received, int) else None,
        "in_pool": r.get("in_pool"),
        "how_to_read": ("verified=true means this transaction really pays this address the amount shown. "
                        "A false confirmations / zero received means the tx key or proof does not match this "
                        "address and transaction."),
        "source": "RobotBase's own monero-wallet-rpc doing a cryptographic tx-proof check (read-only)",
    }
    if not good:
        out["note"] = ("the proof material does not confirm this payment for this address "
                       "(wrong tx key, wrong address, or the transaction is not yet mined)")
    return out


# ============================================================ 2026-10-16 TRON 节点工具面
# 数据来自自建的 java-tron lite full node（RB_TRON_NODE）。
#
# ⚠️ 能力边界：这是一个 lite full node，java-tron 的「历史查询 API」被整块关闭 ——
#    getblockbynum / getblockbyid / gettransactionbyid / gettransactioninfobyid /
#    getblockbylatestnum / getblockbylimitnext 一律返回
#    "this API is closed because this node is a lite fullnode"（实测连 tip-1 都是）。
#    因此本节点**不能**用于核验历史交易到账，那件事仍只能靠 TronGrid 这类索引服务。
#    （把 openHistoryQueryWhenLiteFN 打开也解决不了：官方注释明确写了
#     "above APIs may return null even if blocks and transactions actually are on the
#      blockchain" —— 对到账核验等于制造假信号，故不开启。）
#
# 真正可用（逐条实测）：当前块、账户与资源、合约元数据、**合约只读调用**、
# 链上经济参数、SR 列表、节点自身状态。
TRON_NODE = os.environ.get("RB_TRON_NODE", "http://127.0.0.1:8090")
TRON_SOLIDITY = os.environ.get("RB_TRON_SOLIDITY", "http://127.0.0.1:8091")
TRON_USDT_HEX = "41a614f803b6fd780986a42c78ec9c7f77e6ded13c"
TRON_SUN = 1_000_000
TRON_ZERO_ADDR = "410000000000000000000000000000000000000000"
TRON_TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TRON_WINDOW_HINT = 273000        # 2026-10-19 实测约 9.5 天；实际边界由 _tron_window() 懒探测


def _b58decode(s):
    n = 0
    for ch in s:
        n = n * 58 + _B58_ALPHABET.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    pad = 0
    for ch in s:
        if ch == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + raw


def _tron_hex(addr):
    """接受 base58（T…）或 hex（41…），统一返回 41 开头的 21 字节 hex；非法返回空串。"""
    a = (addr or "").strip()
    if not a:
        return ""
    if a.lower().startswith("41") and len(a) == 42:
        try:
            bytes.fromhex(a)
            return a.lower()
        except ValueError:
            return ""
    if a.startswith("T") and 33 <= len(a) <= 35:
        try:
            raw = _b58decode(a)
            if len(raw) == 25 and hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4] == raw[21:]:
                return raw[:21].hex()
        except Exception:  # noqa: BLE001
            return ""
    return ""


def _tron_b58(h):
    try:
        raw = bytes.fromhex(h)
    except Exception:  # noqa: BLE001
        return ""
    if len(raw) != 21:
        return ""
    return _b58encode(raw + hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4])


def _tron_addr_normalize(addr):
    """Return canonical base58 TRON address, or None if the address is invalid.

    2026-10-19 (ported QA P0-1): risk scoring used a regex only, so a base58 string with
    a broken checksum was scored `low / 100` and charged. We now verify the checksum
    (and accept the 41-hex form used by node RPC).
    """
    a = (addr or "").strip()
    if re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", a):
        try:
            raw = _b58decode(a)
        except Exception:  # noqa: BLE001
            return None
        if len(raw) == 25 and hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4] == raw[21:]:
            return a
        return None
    if re.fullmatch(r"(?:41)?[0-9a-fA-F]{40}", a):
        hx = a.lower()
        if not hx.startswith("41"):
            hx = "41" + hx
        return _tron_b58(hx)
    return None


def _tron(method, params=None, timeout=15):
    return _http_json(TRON_NODE + "/" + method, params or {}, timeout=timeout)


def _tron_tip():
    return cached("tron_tip", 5, lambda: _tron("wallet/getnowblock"))


def _tron_window():
    """懒探测本节点的历史保留窗口：二分出最老的可用区块，缓存 1 小时。

    节点是 lite full node，同步自 checkpoint，因此只保留最近一段历史。
    对外必须如实给出这个边界——查不到 ≠ 不存在。
    """
    def probe():
        tip = ((_tron_tip() or {}).get("block_header") or {}).get("raw_data", {}).get("number")
        if not tip:
            return {}
        lo, hi = 0, int(tip)          # lo 不可用，hi 可用
        if not (_tron("wallet/getblockbynum", {"num": hi}) or {}).get("block_header"):
            return {}
        for _ in range(24):
            mid = (lo + hi) // 2
            ok = bool((_tron("wallet/getblockbynum", {"num": mid}) or {}).get("block_header"))
            if ok:
                hi = mid
            else:
                lo = mid
            if hi - lo <= 1:
                break
        oldest = hi
        # 用最老与最新块的时间戳算实际跨度
        try:
            b_old = (_tron("wallet/getblockbynum", {"num": oldest}) or {}).get("block_header", {}).get("raw_data", {})
            b_tip = (_tron("wallet/getblockbynum", {"num": tip}) or {}).get("block_header", {}).get("raw_data", {})
            span_s = int((b_tip.get("timestamp", 0) - b_old.get("timestamp", 0)) / 1000)
        except Exception:  # noqa: BLE001
            span_s = None
        return {"oldest_available_block": oldest, "tip": int(tip),
                "window_blocks": int(tip) - oldest,
                "window_days": round(span_s / 86400, 2) if span_s else None}
    return cached("tron_window", 300, probe) or {}   # 2026-10-19: 5 min, was 1 h (stale tip)


def _tron_tx_found(txid):
    """返回 (tx, info)。tx 来自 gettransactionbyid，info 来自 gettransactioninfobyid。"""
    tx = _tron("wallet/gettransactionbyid", {"value": txid}) or {}
    info = _tron("wallet/gettransactioninfobyid", {"value": txid}) or {}
    return tx, info


def _tron_decode_trc20_logs(info):
    """从 receipt 的 log 里解出 TRC-20 Transfer 事件（from / to / 金额）。"""
    out = []
    for lg in (info.get("log") or []):
        topics = lg.get("topics") or []
        if not topics or topics[0] != TRON_TRANSFER_TOPIC or len(topics) < 3:
            continue
        try:
            amount = int(lg.get("data") or "0", 16)
        except ValueError:
            amount = None
        out.append({
            "token": _tron_b58("41" + (lg.get("address") or "").rjust(40, "0")[-40:]),
            "from": _tron_b58("41" + topics[1][-40:]),
            "to": _tron_b58("41" + topics[2][-40:]),
            "amount_atomic": amount,
            "amount_human": round(amount / 10 ** 6, 6) if amount is not None else None,
            "decimals_assumed": 6,
        })
    return out


def _tron_parse_blockref(v):
    """nodeinfo 的 block / solidityBlock 是 "Num:87146764,ID:<hash>" 这样的字符串。"""
    if isinstance(v, dict):
        return v.get("number"), v.get("id") or v.get("ID")
    if isinstance(v, str):
        num, _, rest = v.partition(",")
        n = num.split(":", 1)[-1].strip()
        bid = rest.split(":", 1)[-1].strip() if ":" in rest else ""
        try:
            return int(n), bid
        except ValueError:
            return None, bid
    return None, None


def _tron_abi_param_address(a):
    h = _tron_hex(a)
    return h[2:].rjust(64, "0") if h else ""


def _tron_abi_decode(hx, kind):
    if not hx or not isinstance(hx, str):
        return None
    try:
        if kind == "address":
            return _tron_b58("41" + hx[-40:])
        if kind == "bool":
            return bool(int(hx, 16))
        if kind == "string":
            off = int(hx[0:64], 16) * 2
            ln = int(hx[off:off + 64], 16)
            return bytes.fromhex(hx[off + 64: off + 64 + ln * 2]).decode("utf-8", "replace")
        return int(hx, 16)
    except Exception:  # noqa: BLE001
        return None


def t_tron_node_status():
    """TRON 节点自身状态：当前高度、固化高度、落后块数、peer、客户端版本。"""
    b = _tron_tip()
    if not b or not b.get("block_header"):
        return {"available": False, "reason": "java-tron HTTP API unreachable"}
    hdr = b["block_header"]["raw_data"]
    ni = cached("tron_nodeinfo", 10, lambda: _tron("wallet/getnodeinfo")) or {}
    # 注意：nodeinfo 里的 block / solidityBlock 是 "Num:123,ID:abc" 形式的**字符串**，不是对象
    sol_num, _sol_id = _tron_parse_blockref(ni.get("solidityBlock"))
    tip = hdr.get("number")
    return {
        "ok": True,
        "height": tip,
        "solidity_height": sol_num,
        "solidity_lag_blocks": (tip - sol_num) if (tip and sol_num) else None,
        "timestamp_ms": hdr.get("timestamp"),
        "age_s": int(time.time() - (hdr.get("timestamp") or 0) / 1000) if hdr.get("timestamp") else None,
        "tx_in_tip_block": len(b.get("transactions") or []),
        "peer_count": len(ni.get("peerList") or []),
        "active_connections": ni.get("activeConnectCount"),
        "passive_connections": ni.get("passiveConnectCount"),
        "client_version": (ni.get("configNodeInfo") or {}).get("codeVersion"),
        "retention_window": _tron_window() or {"note": "not yet probed"},
        "history_api": "open within the retention window (lite full node)",
        "source": "our java-tron wallet/getnowblock + getnodeinfo",
    }


def t_tron_block(height=None, block_id=None):
    """按块号或区块 ID 取一个 TRON 区块（仅限本节点的历史窗口）。

    查不到时绝不回答"不存在"——lite 节点只保留最近一段历史，边界外一律 EMPTY。
    """
    if block_id:
        b = _tron("wallet/getblockbyid", {"value": str(block_id).strip()}) or {}
        asked = {"block_id": str(block_id).strip()}
    else:
        try:
            h = int(height)
        except (TypeError, ValueError):
            return {"error": "provide height (integer) or block_id"}
        b = _tron("wallet/getblockbynum", {"num": h}) or {}
        asked = {"height": h}
    if not b or not b.get("block_header"):
        return {
            "found": False, **asked, "reason": "empty_result_from_lite_node",
            "important": ("This does NOT mean the block does not exist. Our Tron node is a lite full node "
                          "retaining only a recent window of history, so anything older returns empty. "
                          "Treat an empty result as 'outside our window', never as proof of absence."),
            "retention_window": _tron_window() or {"note": "not yet probed"},
        }
    hdr = b["block_header"]["raw_data"]
    txs = b.get("transactions") or []
    kinds = {}
    for t in txs:
        for c in ((t.get("raw_data") or {}).get("contract") or []):
            k = (c.get("type") or "?").replace("Contract", "")
            kinds[k] = kinds.get(k, 0) + 1
    return {
        "found": True,
        "number": hdr.get("number"), "block_id": b.get("blockID"),
        "parent_hash": hdr.get("parentHash"), "timestamp_ms": hdr.get("timestamp"),
        "tx_count": len(txs), "tx_by_type": kinds,
        "producer_address": _tron_b58(hdr.get("witness_address") or ""),
        "tx_ids": [t.get("txID") for t in txs][:25],
        "retention_window": _tron_window() or None,
        "source": "our java-tron wallet/getblockbynum|getblockbyid",
    }


def t_tron_transaction(txid):
    """按 txid 查一笔 TRON 交易：状态、能量消耗、以及解出的 TRC-20 转账明细。

    这是核验 USDT 到账的主力工具——我们自己节点就能给出 from / to / 金额；
    但同样受历史窗口限制：查不到必须回落索引服务，不能当作"没到账"。
    """
    t = str(txid or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", t):
        return {"error": "not a TRON transaction id (expected 64 hex chars)"}
    tx = _tron("wallet/gettransactionbyid", {"value": t}) or {}
    info = _tron("wallet/gettransactioninfobyid", {"value": t}) or {}
    if not tx and not info:
        return {
            "found": False, "txid": t, "reason": "empty_result_from_lite_node",
            "important": ("This does NOT mean the transaction does not exist. Our Tron node is a lite full "
                          "node keeping only a recent window; older transactions return empty. Fall back to an "
                          "indexer (TronGrid) before telling anyone a payment did not arrive."),
            "retention_window": _tron_window() or {"note": "not yet probed"},
        }
    raw = tx.get("raw_data") or {}
    contracts = raw.get("contract") or []
    ctype, cval = None, {}
    if contracts:
        ctype = (contracts[0].get("type") or "").replace("Contract", "")
        cval = (contracts[0].get("parameter") or {}).get("value") or {}
    receipt = info.get("receipt") or {}
    res = receipt.get("result")
    out = {
        "found": True, "txid": tx.get("txID") or t,
        "block_number": info.get("blockNumber"),
        "block_timestamp_ms": info.get("blockTimeStamp"),
        "status": {"SUCCESS": "success", "REVERT": "reverted"}.get(res, res if res else "unknown"),
        "contract_type": ctype,
        "trc20_transfers": _tron_decode_trc20_logs(info),
        "energy_used": receipt.get("energy_usage_total"),
        "net_used": receipt.get("net_usage"),
    }
    for key, label in (("owner_address", "owner_address"), ("to_address", "to_address"),
                       ("contract_address", "contract_address")):
        v = cval.get(key)
        out[label] = _tron_b58("41" + v.rjust(40, "0")[-40:]) if v else None
    if cval.get("amount"):
        out["amount_trx"] = round(int(cval["amount"]) / TRON_SUN, 6)
    tip = ((_tron_tip() or {}).get("block_header") or {}).get("raw_data", {}).get("number")
    if tip and info.get("blockNumber"):
        # 2026-10-19 (ported QA P2-16): the tip block counts as 1 confirmation (was off by one).
        out["confirmations"] = int(tip) - int(info["blockNumber"]) + 1
    try:
        sinfo = _http_json(TRON_SOLIDITY + "/walletsolidity/gettransactioninfobyid",
                           {"value": t}, timeout=10) or {}
        out["finalized"] = bool(sinfo.get("blockNumber"))
    except Exception:  # noqa: BLE001
        out["finalized"] = None
    out["retention_window"] = _tron_window() or None
    out["how_to_read"] = ("finalized=true means it is on the solidified chain and cannot be reverted. "
                          "trc20_transfers[] is decoded from the receipt logs - for USDT that is the amount, "
                          "sender and recipient, read straight off our own node.")
    out["source"] = "our java-tron gettransactionbyid + gettransactioninfobyid"
    return out


def t_tron_current_block():
    """当前（最新）区块。**只能取 tip** —— 本节点不提供历史区块。"""
    b = _tron_tip()
    if not b or not b.get("block_header"):
        return {"available": False, "reason": "java-tron HTTP API unreachable"}
    hdr = b["block_header"]["raw_data"]
    txs = b.get("transactions") or []
    kinds = {}
    for t in txs:
        for c in ((t.get("raw_data") or {}).get("contract") or []):
            k = (c.get("type") or "?").replace("Contract", "")
            kinds[k] = kinds.get(k, 0) + 1
    return {
        "number": hdr.get("number"),
        "block_id": b.get("blockID"),
        "parent_hash": hdr.get("parentHash"),
        "timestamp_ms": hdr.get("timestamp"),
        "age_s": int(time.time() - (hdr.get("timestamp") or 0) / 1000) if hdr.get("timestamp") else None,
        "tx_count": len(txs),
        "tx_by_type": kinds,
        "producer_address": _tron_b58(hdr.get("witness_address") or ""),
        "scope": "current block only - historical blocks are closed on this lite full node",
        "source": "our java-tron wallet/getnowblock",
    }


def t_tron_account(address):
    """TRX 余额、能量/带宽资源、是否超级代表、账户年龄。"""
    h = _tron_hex(address)
    if not h:
        return {"error": "not a TRON address (expected base58 T... or 41-hex)"}
    a = _tron("wallet/getaccount", {"address": h}) or {}
    res = _tron("wallet/getaccountresource", {"address": h}) or {}
    if not a or not a.get("address"):
        return {"found": False, "address": _tron_b58(h),
                "note": "account is not activated on chain (no TRX ever received)"}
    ar = a.get("account_resource") or {}
    unfreeze = _tron("wallet/getavailableunfreezecount", {"owner_address": h}) or {}
    # 2026-10-19 (ported QA P1-8): java-tron `getaccount` omits `code`; contract accounts
    # (e.g. USDT) were reported as has_code=false. Confirm via getcontract.
    has_code = bool(a.get("code"))
    if not has_code and (a.get("account_name") or a.get("balance")):
        try:
            ctr = _tron("wallet/getcontract", {"value": h}) or {}
            has_code = bool(ctr.get("bytecode") or ctr.get("code_hash") or ctr.get("contract_address"))
        except Exception:  # noqa: BLE001
            pass
    # 2026-10-16 加深：账户权限结构（多签）与委托资源索引
    perms = {}
    for pname in ("owner_permission", "witness_permission", "active_permission"):
        v = a.get(pname)
        if not v:
            continue
        if isinstance(v, list):
            v = v[0] if v else None
        if not isinstance(v, dict):
            continue
        perms[pname] = {
            "permission_name": v.get("permission_name"),
            "threshold": v.get("threshold"),
            "keys": [{"address": _tron_b58("41" + (k.get("address") or "").rjust(40, "0")[-40:]),
                      "weight": k.get("weight")} for k in (v.get("keys") or [])],
        }
    deleg = {}
    try:
        idx = _tron("wallet/getdelegatedresourceaccountindex", {"value": h}) or {}
        accts = idx.get("account") or []
        deleg["counterparties"] = len(accts)
        deleg["counterparties_sample"] = [
            _tron_b58("41" + (x or "").rjust(40, "0")[-40:]) for x in accts[:5]]
    except Exception:  # noqa: BLE001
        deleg["counterparties"] = None
    return {
        "found": True,
        "address": _tron_b58(h),
        "name": bytes.fromhex(a["account_name"]).decode("utf-8", "replace") if a.get("account_name") else None,
        "trx_balance": round((a.get("balance") or 0) / TRON_SUN, 6),
        "created_ms": a.get("create_time"),
        "last_operation_ms": a.get("latest_opration_time"),
        "is_witness": bool(a.get("is_witness")),
        "has_code": has_code,
        "is_contract": has_code,
        "energy": {"used": ar.get("energy_usage"), "limit": res.get("EnergyLimit"),
                   "total_limit": res.get("TotalEnergyLimit")},
        "bandwidth": {"free_used": res.get("freeNetUsed"), "free_limit": res.get("freeNetLimit"),
                      "net_limit": res.get("NetLimit")},
        "available_unfreeze_count": unfreeze.get("count"),
        "permissions": perms or None,
        "multisig": bool(perms and any(len(v["keys"]) > 1 or (v.get("threshold") or 1) > 1
                                       for v in perms.values())),
        "delegation": deleg,
        "source": "our java-tron getaccount + getaccountresource + getdelegatedresourceaccountindex",
    }


def t_tron_contract(address):
    """合约元数据：是否已公布 ABI、名称、创建者、函数列表、TRC-20 判定。"""
    h = _tron_hex(address)
    if not h:
        return {"error": "not a TRON address (expected base58 T... or 41-hex)"}
    c = _tron("wallet/getcontract", {"value": h}) or {}
    if not c or not c.get("contract_address"):
        return {"found": False, "address": _tron_b58(h),
                "note": "no contract at this address (or it is a plain account)"}
    entries = ((c.get("abi") or {}).get("entrys") or [])
    fns = sorted({e.get("name") for e in entries if e.get("type") == "Function" and e.get("name")})
    trc20 = {"balanceOf", "transfer", "totalSupply", "transferFrom", "approve", "allowance"}.issubset(set(fns))
    return {
        "found": True,
        "address": _tron_b58(h),
        "name": c.get("name"),
        "creator": _tron_b58(c.get("origin_address") or ""),
        "has_abi": bool(entries),
        "abi_entry_count": len(entries),
        "is_trc20": trc20,
        "functions": fns[:40],
        "code_hash": c.get("code_hash"),
        "consume_user_resource_percent": c.get("consume_user_resource_percent"),
        "origin_energy_limit": c.get("origin_energy_limit"),
        "how_to_read": ("has_abi=true means the ABI is published on chain, so tron_read_contract can "
                        "call it. is_trc20 is inferred from the function set, not from a registry."),
        "source": "our java-tron wallet/getcontract",
    }


TRC20_ARITY = {"balanceOf": 1, "allowance": 2, "decimals": 0, "symbol": 0, "name": 0, "totalSupply": 0}


def t_tron_read_contract(contract, function, argument=None, argument2=None,
                         parameter=None, output_type=None):
    """合约**只读调用**（constant call，不改任何状态）。

    TRC-20 常用函数有语法糖（balanceOf/allowance/decimals/symbol/name/totalSupply），
    也接受原始 function_selector + ABI 编码的 parameter。
    """
    ch = _tron_hex(contract)
    if not ch:
        return {"error": "not a TRON address (expected base58 T... or 41-hex)"}
    fn = (function or "").strip()
    if not fn:
        return {"error": "function required, e.g. balanceOf(address) or 'balanceOf' with argument=<address>"}
    base = fn.split("(")[0]
    if base in TRC20_ARITY:
        arity = TRC20_ARITY[base]
        selector = base + "(" + ",".join(["address"] * arity) + ")"
        parts = []
        for arg in (argument, argument2)[:arity]:
            p = _tron_abi_param_address(arg)
            if not p:
                return {"error": "%s needs %d TRON address argument(s)" % (base, arity)}
            parts.append(p)
        param = "".join(parts)
        kind = output_type or ("string" if base in ("symbol", "name") else "uint256")
    else:
        selector = fn
        param = parameter or ""
        kind = output_type or "uint256"
    r = _tron("wallet/triggerconstantcontract", {
        "owner_address": TRON_ZERO_ADDR, "contract_address": ch,
        "function_selector": selector, "parameter": param, "visible": False}) or {}
    cr = r.get("constant_result") or []
    res = r.get("result") or {}
    msg_raw = res.get("message")
    msg = ""
    if msg_raw:
        try:
            msg = bytes.fromhex(str(msg_raw)).decode("utf-8", "replace")
        except (ValueError, TypeError):
            msg = str(msg_raw)
    # 2026-10-19 (ported QA P1-7): java-tron sets result.result=true even when the call
    # reverted and returns message="REVERT opcode executed" + empty constant_result.
    reverted = ("REVERT" in msg.upper()) or (not cr) or (not str(cr[0] if cr else ""))
    if reverted:
        return {"ok": False, "revert": "REVERT" in msg.upper(), "contract": _tron_b58(ch),
                "function": selector,
                "error": msg or "constant call returned no value",
                "result_code": res.get("code"),
                "hint": "the function may not exist on this contract, or the call reverted; "
                        "ok:false means the value is not trustworthy"}
    raw = cr[0]
    val = _tron_abi_decode(raw, kind)
    out = {"ok": True, "contract": _tron_b58(ch), "function": selector,
           "output_type": kind, "value": val, "raw": raw,
           "energy_used": r.get("energy_used"),
           "source": "our java-tron wallet/triggerconstantcontract (read-only, no state change)"}
    if base in ("balanceOf", "totalSupply", "allowance") and isinstance(val, int):
        out["value_human"] = round(val / 10 ** 6, 6)
        out["decimals_assumed"] = 6
    if base == "balanceOf" and argument:
        out["holder"] = _tron_b58(_tron_hex(argument))
    return out


def t_tron_chain_economics():
    """TRON 链上经济参数：能量单价、交易费、账户创建费、SR 出块奖励、下次维护。"""
    cp = cached("tron_chainparams", 300, lambda: _tron("wallet/getchainparameters")) or {}
    params = {p.get("key"): p.get("value") for p in (cp.get("chainParameter") or [])}
    nm = _tron("wallet/getnextmaintenancetime") or {}
    ep = cached("tron_energyprice", 300, lambda: _tron("wallet/getenergyprices")) or {}
    tail = (ep.get("prices") or "").split(",")[-1]
    return {
        "energy_fee_sun_per_unit": params.get("getEnergyFee"),
        "transaction_fee_sun": params.get("getTransactionFee"),
        "create_account_fee_sun": params.get("getCreateAccountFee"),
        "create_account_fee_trx": round((params.get("getCreateAccountFee") or 0) / TRON_SUN, 6),
        "witness_pay_per_block_sun": params.get("getWitnessPayPerBlock"),
        "witness_pay_per_block_trx": round((params.get("getWitnessPayPerBlock") or 0) / TRON_SUN, 6),
        "total_energy_limit": params.get("getTotalEnergyLimit"),
        "total_bandwidth_limit": params.get("getTotalNetLimit"),
        "maintenance_interval_s": params.get("getMaintenanceTimeInterval"),
        "next_maintenance_ms": nm.get("num"),
        "unfreeze_delay_days": params.get("getUnfreezeDelayDays"),
        "allow_creation_of_contracts": params.get("getAllowCreationOfContracts"),
        "latest_energy_price": tail.split(":")[-1] if tail else None,
        "how_to_read": ("cost of a TRC-20 transfer = energy consumed by the contract x "
                        "energy_fee_sun_per_unit, paid in TRX unless the sender holds staked energy. "
                        "A plain TRX transfer burns bandwidth instead of energy."),
        "source": "our java-tron getchainparameters + getenergyprices + getnextmaintenancetime",
    }


def t_tron_list_witnesses(limit=30):
    """超级代表（SR）与候选节点：出块数、漏块数、票数、官网。"""
    try:
        n = max(1, min(200, int(limit or 30)))
    except (TypeError, ValueError):
        n = 30
    w = cached("tron_witnesses", 60, lambda: _tron("wallet/listwitnesses")) or {}
    ws = w.get("witnesses") or []
    if not ws:
        return {"available": False, "reason": "witness list unavailable"}
    active = [x for x in ws if x.get("isJobs")]
    top = sorted(ws, key=lambda y: -(y.get("totalProduced") or 0))[:n]
    return {
        "registered_witnesses": len(ws),
        "active_block_producers": len(active),
        "top_by_blocks_produced": [
            {"address": _tron_b58(x.get("address") or ""), "url": x.get("url"),
             "blocks_produced": x.get("totalProduced"), "blocks_missed": x.get("totalMissed"),
             "votes": x.get("voteCount"), "in_active_set": bool(x.get("isJobs"))} for x in top],
        "source": "our java-tron wallet/listwitnesses",
    }


# ============================================================ 2026-10-16 ETH 工具面
# 数据来自自建的 Reth 执行层（RB_ETH_EXEC_RPC）与 Lighthouse 共识层（RB_ETH_CONS_RPC）。
# 最终性用 Reth 的 "finalized" 区块标签判定（一行调用，比自己去算 epoch 可靠）。
ETH_EXEC = os.environ.get("RB_ETH_EXEC_RPC", "http://127.0.0.1:8545")
ETH_CONS = os.environ.get("RB_ETH_CONS_RPC", "http://127.0.0.1:5052")
ETH_WEI = 10 ** 18
ERC20_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# 常见代币的 decimals 兜底（查不到 decimals() 时用）；未命中就如实标注未知
ETH_TOKEN_DECIMALS = {
    "0xdac17f958d2ee523a2206206994597c13d831ec7": 6,    # USDT
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6,    # USDC
    "0x6b175474e89094c44da98b954eedeac495271d0f": 18,   # DAI
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": 18,   # WETH
}


def _eth(method, params=None, timeout=15):
    d = _http_json(ETH_EXEC, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []},
                   timeout=timeout)
    if isinstance(d, dict) and d.get("error"):
        raise RuntimeError(str(d["error"])[:160])
    return (d or {}).get("result")


def _eth_int(v, default=None):
    if v is None:
        return default
    try:
        return int(v, 16) if isinstance(v, str) else int(v)
    except (TypeError, ValueError):
        return default


def _eth_ok_addr(a):
    return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", (a or "").strip()))


def _eth_token_decimals(token):
    """查代币 decimals；查不到就用常见表；再不行返回 None（不猜）。"""
    t = (token or "").lower()
    if t in ETH_TOKEN_DECIMALS:
        return ETH_TOKEN_DECIMALS[t]
    try:
        r = _eth("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"], timeout=8)
        if r and r != "0x":
            return _eth_int(r)
    except Exception:  # noqa: BLE001
        pass
    return None


def _eth_finalized_block():
    b = _eth("eth_getBlockByNumber", ["finalized", False], timeout=10)
    return _eth_int((b or {}).get("number"))


def _eth_decode_erc20_logs(receipt):
    out = []
    for lg in (receipt.get("logs") or []):
        topics = lg.get("topics") or []
        if len(topics) < 3 or (topics[0] or "").lower() != ERC20_TRANSFER:
            continue
        try:
            amount = int(lg.get("data") or "0x0", 16)
        except ValueError:
            amount = None
        token = lg.get("address")
        dec = _eth_token_decimals(token)
        out.append({
            "token": token,
            "from": "0x" + topics[1][-40:],
            "to": "0x" + topics[2][-40:],
            "amount_atomic": amount,
            "decimals": dec,
            "amount_human": round(amount / 10 ** dec, 6) if (amount is not None and dec is not None) else None,
            "amount_note": None if dec is not None else "decimals() not readable for this token - raw units only",
        })
    return out


def t_eth_transaction(txid):
    """以太坊交易核验：成功/回滚、确认数、是否已最终确定，以及解出的 ERC-20 转账事件。"""
    t = (txid or "").strip().lower()
    if not t.startswith("0x"):
        t = "0x" + t
    if not re.fullmatch(r"0x[0-9a-f]{64}", t):
        return {"error": "not an Ethereum transaction hash (expected 0x + 64 hex chars)"}
    tx = _eth("eth_getTransactionByHash", [t])
    rc = _eth("eth_getTransactionReceipt", [t])
    if not tx and not rc:
        return {"found": False, "txid": t,
                "reason": "not present in our node's retained window",
                "retention": {"tx_hash_index_from_block": 26170371,
                              "tx_hash_index_note": "boundary of 2026-10-12; only widens over time",
                              "receipts": "rolling ~44k blocks (~6 days; 1.4-8.3 days as the 50k-block shard rolls)",
                              "block_bodies_from": 15500000},
                "important": ("Our Reth node is pruned, so a miss is not proof the transaction does not exist: "
                              "hash lookups start at block 26,170,371 (2026-10-12) and receipts cover roughly the "
                              "last 6 days. Cross-check an indexer before telling anyone a payment did not arrive.")}
    h = _eth_int((tx or {}).get("blockNumber"))
    out = {"found": True, "txid": t, "block_number": h,
           "from": (tx or {}).get("from"), "to": (tx or {}).get("to"),
           "value_eth": round(_eth_int((tx or {}).get("value"), 0) / ETH_WEI, 12),
           "nonce": _eth_int((tx or {}).get("nonce")),
           "gas_limit": _eth_int((tx or {}).get("gas")),
           "max_fee_per_gas_gwei": round(_eth_int(((tx or {}).get("maxFeePerGas")), 0) / 1e9, 6)
                                     if (tx or {}).get("maxFeePerGas") else None,
           "input_is_contract_call": bool((tx or {}).get("input") and (tx or {}).get("input") != "0x")}
    if rc:
        out["status"] = "success" if _eth_int(rc.get("status")) == 1 else "reverted"
        out["gas_used"] = _eth_int(rc.get("gasUsed"))
        gp = _eth_int(rc.get("effectiveGasPrice"))
        out["effective_gas_price_gwei"] = round(gp / 1e9, 6) if gp else None
        out["fee_eth"] = round(_eth_int(rc.get("gasUsed"), 0) * (gp or 0) / ETH_WEI, 12) if gp else None
        out["erc20_transfers"] = _eth_decode_erc20_logs(rc)
        out["log_count"] = len(rc.get("logs") or [])
        out["contract_created"] = rc.get("contractAddress")
    tip = _eth_int((_eth("eth_getBlockByNumber", ["latest", False]) or {}).get("number"))
    fin = _eth_finalized_block()
    if h:
        out["confirmations"] = (tip - h + 1) if tip else None
        out["finalized"] = bool(fin and h <= fin)
    out["how_to_read"] = ("finalized=true means the block is at or below Ethereum's finalized checkpoint, so "
                          "the transaction cannot be reverted by a reorg. erc20_transfers[] is decoded from "
                          "the receipt logs - for USDT/USDC that is the amount, sender and recipient.")
    out["source"] = "our Reth execution node + finalized checkpoint"
    return out


def t_eth_fee_rates():
    """以太坊费用市场：base fee 走势、优先费分位、以及一笔转账/一次 ERC-20 调用的估算成本。"""
    latest = _eth("eth_getBlockByNumber", ["latest", False]) or {}
    base = _eth_int(latest.get("baseFeePerGas"), 0)
    gas_price = _eth_int(_eth("eth_gasPrice"), 0)
    try:
        prio = _eth_int(_eth("eth_maxPriorityFeePerGas"), 0)
    except Exception:  # noqa: BLE001
        prio = None
    fh = _eth("eth_feeHistory", ["0x5", "latest", [10, 50, 90]]) or {}
    rewards = fh.get("reward") or []
    pcts = {}
    for name, idx in (("p10", 0), ("p50", 1), ("p90", 2)):
        vals = [r[idx] for r in rewards if len(r) > idx]
        if vals:
            pcts[name] = round(sum(_eth_int(v, 0) for v in vals) / len(vals) / 1e9, 6)
    base_series = [round(_eth_int(x, 0) / 1e9, 6) for x in (fh.get("baseFeePerGas") or [])]
    return {
        "base_fee_gwei": round(base / 1e9, 6),
        "next_base_fee_gwei": base_series[-1] if base_series else None,
        "base_fee_last_blocks_gwei": base_series[:-1] or base_series,
        "gas_price_gwei": round(gas_price / 1e9, 6),
        "priority_fee_gwei": round(prio / 1e9, 6) if prio is not None else None,
        "priority_percentiles_gwei": pcts or None,
        "estimated_cost": {
            # 2026-10-19 (ported QA P2-15): eth_maxPriorityFeePerGas returns 0 on an idle Reth
            # node; use feeHistory percentiles so the estimate is not ~22x too low at p90.
            "simple_eth_transfer_21000_gas_eth": round(
                21000 * (base + ((pcts.get("p50") or 0) * 1e9)) / ETH_WEI, 12),
            "erc20_transfer_65000_gas_eth": round(
                65000 * (base + ((pcts.get("p90") or 0) * 1e9)) / ETH_WEI, 12),
            "assumption": ("ETH transfer priced with the p50 tip, ERC-20 with the p90 tip; "
                           "60,000-70,000 gas for an ERC-20 transfer depending on token/storage."),
            "tip_used_gwei": {"simple": pcts.get("p50"), "erc20": pcts.get("p90")},
        },
        "how_to_read": ("Total fee = gas used x (base fee + priority fee). Use priority_percentiles to pick a "
                        "tip: p10 is a slow/cheap inclusion, p90 is a fast one. The base fee is burned, the "
                        "priority fee goes to the validator."),
        "source": "our Reth node (eth_feeHistory + eth_gasPrice + eth_maxPriorityFeePerGas)",
    }


def t_eth_account(address):
    """以太坊账户：ETH 余额、nonce、是否合约账户。"""
    a = (address or "").strip()
    if not _eth_ok_addr(a):
        return {"error": "not an Ethereum address (expected 0x + 40 hex chars)"}
    bal = _eth("eth_getBalance", [a, "latest"])
    nonce = _eth("eth_getTransactionCount", [a, "latest"])
    code = _eth("eth_getCode", [a, "latest"])
    is_contract = bool(code and code != "0x")
    return {
        "address": a,
        "balance_eth": round(_eth_int(bal, 0) / ETH_WEI, 12),
        "nonce": _eth_int(nonce),
        "is_contract": is_contract,
        "code_size_bytes": (len(code) - 2) // 2 if is_contract else 0,
        "note": ("A nonce equal to 0 with an empty code and zero balance means the address has never been "
                 "used on chain; a contract account always has code."),
        "source": "our Reth node (eth_getBalance + eth_getTransactionCount + eth_getCode)",
    }


ERC20_ARITY = {"balanceOf": 1, "allowance": 2, "decimals": 0, "symbol": 0, "name": 0, "totalSupply": 0}
# 内置的 ERC-20 selector（keccak 前 4 字节，硬编码 —— 这样 MCP 侧不需要引入 keccak 依赖）
ERC20_SELECTOR = {
    "balanceOf(address)": "70a08231",
    "allowance(address,address)": "dd62ed3e",
    "decimals()": "313ce567",
    "symbol()": "95d89b41",
    "name()": "06fdde03",
    "totalSupply()": "18160ddd",
}


def _eth_abi_pad_addr(a):
    return a[2:].rjust(64, "0").lower() if _eth_ok_addr(a) else ""


def _eth_abi_decode(hx, kind):
    """以太坊返回值解码。注意地址要还原成 0x 形式，不能复用 TRON 那套 base58 解码。"""
    if not hx or not isinstance(hx, str):
        return None
    try:
        if kind == "address":
            return "0x" + hx[-40:]
        if kind == "bool":
            return bool(int(hx, 16))
        if kind == "string":
            off = int(hx[0:64], 16) * 2
            ln = int(hx[off:off + 64], 16)
            return bytes.fromhex(hx[off + 64: off + 64 + ln * 2]).decode("utf-8", "replace")
        return int(hx, 16)
    except Exception:  # noqa: BLE001
        return None


def t_eth_read_contract(contract, function, argument=None, argument2=None,
                        selector=None, parameter=None, output_type=None):
    """以太坊合约**只读调用**（eth_call）。

    ERC-20 常用函数有语法糖（balanceOf / allowance / decimals / symbol / name / totalSupply）；
    其它函数请显式传 `selector`（4 字节 hex）与 ABI 编码好的 `parameter`——
    MCP 侧刻意不引入 keccak 依赖，所以不会去替你猜 selector。
    """
    c = (contract or "").strip()
    if not _eth_ok_addr(c):
        return {"error": "not an Ethereum address (expected 0x + 40 hex chars)"}
    fn = (function or "").strip()
    if selector:
        # 2026-10-19 (ported QA P1-5): `lstrip("0x")` strips a *character set*, not a prefix:
        # a padded ABI parameter starting with 0x00… lost its padding and the call silently
        # returned a wrong value (USDT balanceOf gave 0 instead of 291368219).
        sel = re.sub(r"^0[xX]", "", str(selector).strip()).lower()
        if not re.fullmatch(r"[0-9a-f]{8}", sel):
            return {"error": "selector must be 4 bytes of hex (8 chars)"}
        sig = fn or "raw"
        param = re.sub(r"^0[xX]", "", str(parameter or "").strip()).lower()
        if not re.fullmatch(r"(?:[0-9a-f]{2})*", param):
            return {"error": "parameter must be an even-length hex string without 0x prefix"}
        kind = output_type or "uint256"
    else:
        base = fn.split("(")[0]
        if base not in ERC20_ARITY:
            return {"error": "unknown function %r - pass an explicit `selector` for non-ERC-20 calls" % fn}
        arity = ERC20_ARITY[base]
        sig = base + "(" + ",".join(["address"] * arity) + ")"
        parts = []
        for arg in (argument, argument2)[:arity]:
            pad = _eth_abi_pad_addr(arg)
            if not pad:
                return {"error": "%s needs %d Ethereum address argument(s)" % (base, arity)}
            parts.append(pad)
        param = "".join(parts)
        kind = output_type or ("string" if base in ("symbol", "name") else "uint256")
        sel = ERC20_SELECTOR.get(sig)
        if not sel:
            return {"error": "no built-in selector for %r" % sig}
    try:
        r = _eth("eth_call", [{"to": c, "data": "0x" + sel + param}, "latest"], timeout=15)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "contract": c, "function": sig, "error": str(exc)[:160]}
    if not r or r == "0x":
        return {"ok": False, "contract": c, "function": sig, "error": "eth_call returned empty",
                "hint": "the function may not exist on this contract, or the call reverted"}
    raw = r[2:]
    val = _eth_abi_decode(raw, kind)
    out = {"ok": True, "contract": c, "function": sig, "selector": sel, "output_type": kind,
           "value": val, "raw": raw,
           "source": "our Reth node (eth_call, read-only, no state change)"}
    if isinstance(val, int) and (sig.startswith("balanceOf") or sig.startswith("totalSupply")
                                 or sig.startswith("allowance")):
        dec = _eth_token_decimals(c)
        out["decimals"] = dec
        if dec is not None:
            out["value_human"] = round(val / 10 ** dec, 6)
    return out


# ============================================================ 2026-10-16 ZEC 交易核验
# Zebra 的 RPC 开了 cookie 认证且 cookie 每次重启都变，所以经 9308 上的只读 helper 取数。
ZEC_HELPER = os.environ.get("RB_ZEC_HELPER", "http://127.0.0.1:8093/rpc")


def _zec(method, params=None, timeout=25):
    d = _http_json(ZEC_HELPER, {"method": method, "params": params or []}, timeout=timeout)
    if not (d or {}).get("ok"):
        raise RuntimeError(str((d or {}).get("error"))[:180])
    return d.get("result")


def t_zec_transaction(txid):
    """Zcash 交易核验：是否上链、第几块、多少确认、透明与屏蔽部分构成。

    诚实边界：**屏蔽（shielded）转账的金额与双方在协议层不可见**，
    只有持有查看密钥的人能解读；本工具如实报告"这笔交易含有多少屏蔽输入/输出"，而不是猜金额。
    """
    t = (txid or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", t):
        return {"error": "not a Zcash transaction id (expected 64 hex chars)"}
    try:
        tx = _zec("getrawtransaction", [t, 1], timeout=30)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "not found" in msg.lower() or "-5" in msg:
            return {"found": False, "txid": t, "reason": "not found on our Zebra node",
                    "important": ("Zebra may not retain every historical transaction. A miss is not proof "
                                  "the transaction does not exist - cross-check an explorer before telling "
                                  "anyone a payment did not arrive.")}
        return {"ok": False, "txid": t, "error": "zec helper unavailable: %s" % msg[:160]}
    if not tx:
        return {"found": False, "txid": t}
    conf = tx.get("confirmations")
    vin = tx.get("vin") or []
    vout = tx.get("vout") or []
    shielded_spends = len(tx.get("vShieldedSpend") or [])
    shielded_outputs = len(tx.get("vShieldedOutput") or [])
    orchard = tx.get("orchard") or {}
    orchard_actions = len(orchard.get("actions") or []) if isinstance(orchard, dict) else 0
    # 2026-10-19 (ported QA P1-4): Ironwood is a first-class shielded pool since NU6; an
    # Ironwood-only transaction used to be reported as "no shielded component".
    ironwood = tx.get("ironwood") or {}
    ironwood_actions = len(ironwood.get("actions") or []) if isinstance(ironwood, dict) else 0
    ironwood_bal_zat = ironwood.get("valueBalanceZat") if isinstance(ironwood, dict) else None
    transparent_out = 0.0
    for o in vout:
        try:
            transparent_out += float(o.get("valueZat") or 0)
        except (TypeError, ValueError):
            pass
    return {
        "found": True, "txid": tx.get("txid") or t,
        "confirmations": conf,
        "block_height": tx.get("height"),
        "block_hash": tx.get("blockhash"),
        "block_time": tx.get("blocktime"),
        "in_active_chain": tx.get("in_active_chain"),
        "size_bytes": tx.get("size"),
        "version": tx.get("version"),
        "expiry_height": tx.get("expiryheight"),
        "transparent": {"inputs": len(vin), "outputs": len(vout),
                        "total_output_zec": round(transparent_out / 1e8, 8)},
        "shielded": {"sapling_spends": shielded_spends, "sapling_outputs": shielded_outputs,
                     "orchard_actions": orchard_actions, "ironwood_actions": ironwood_actions,
                     "ironwood_value_balance_zat": ironwood_bal_zat},
        "has_shielded_component": bool(shielded_spends or shielded_outputs
                                       or orchard_actions or ironwood_actions),
        "not_disclosed_by_protocol": (["shielded amounts", "shielded sender", "shielded recipient"]
                                      if (shielded_spends or shielded_outputs or orchard_actions
                                          or ironwood_actions) else []),
        "how_to_read": ("confirmations counts blocks on top. Zcash reorg risk drops with depth; 100 "
                        "confirmations is the common exchange threshold. If has_shielded_component is true, "
                        "the amounts and parties inside that part are hidden by the protocol - a viewing key "
                        "held by a party to the transaction is the only way to read them."),
        "source": "our Zebra 6.3.0 node via the read-only helper (getrawtransaction verbose)",
    }


# ============================================================ 2026-10-16 节点能力深挖（第二批）
# 原则：**优先加深已有工具，只在出现新动词时才加工具**——tools/list 是真实上下文成本。


def t_btc_get_network_stats():
    """BTC 全网与节点状态：难度、算力、链上吞吐、节点版本与索引状态。"""
    def build():
        ni = btc_rpc("getnetworkinfo") or {}
        ct = btc_rpc("getchaintxstats", []) or {}
        mi = btc_rpc("getmempoolinfo") or {}
        out = {"chain": "BTC",
               "difficulty": btc_rpc("getdifficulty"),
               "network_hashrate_hs": btc_rpc("getnetworkhashps"),
               "chain_tx_count_total": ct.get("txcount"),
               "tx_rate_per_s": round(ct.get("txrate"), 3) if ct.get("txrate") else None,
               "window_blocks": None, "window_tx_count": ct.get("window_tx_count"),
               "protocol_version": ni.get("protocolversion"),
               "client": ni.get("subversion"),
               "connections": ni.get("connections"),
               "relay_fee_btc_kvb": ni.get("relayfee"),
               "mempool_txs": mi.get("size"),
               "uptime_s": btc_rpc("uptime"),
               "source": "our own bitcoind getdifficulty + getnetworkhashps + getchaintxstats + getnetworkinfo"}
        try:
            idx = btc_rpc("getindexinfo") or {}
            out["indexes"] = {k: bool(v.get("synced")) for k, v in idx.items()}
        except Exception:  # noqa: BLE001
            out["indexes"] = None
        return out
    return cached("btc_netstats", 30, build)


def t_eth_get_logs(from_block=None, to_block=None, address=None, topic0=None, limit=50):
    """按区块范围 / 合约地址 / 事件签名拉以太坊日志（eth_getLogs）。默认只扫最近 50 块。"""
    def to_hex(v, latest):
        if v is None:
            return "latest" if latest else None
        if isinstance(v, str) and (v.startswith("0x") or v in ("latest", "earliest", "pending", "safe", "finalized")):
            return v
        return hex(int(v))
    try:
        n = max(1, min(1000, int(limit or 50)))
    except (TypeError, ValueError):
        n = 50
    latest = _eth_int(_eth("eth_getBlockByNumber", ["latest", False]).get("number"))
    fb = to_hex(from_block, False) or hex(max(0, latest - 50))
    tb = to_hex(to_block, True) or "latest"
    try:
        fb_n = _eth_int(fb) if fb.startswith("0x") else latest - 50
        tb_n = _eth_int(tb) if tb.startswith("0x") else latest
        if tb_n - fb_n > 1000:
            return {"error": "block range too wide (max 1000 blocks per call)",
                    "from_block": fb, "to_block": tb}
    except Exception:  # noqa: BLE001
        pass
    f = {"fromBlock": fb, "toBlock": tb}
    if address:
        if not _eth_ok_addr(address):
            return {"error": "not an Ethereum address (expected 0x + 40 hex chars)"}
        f["address"] = address
    if topic0:
        t = topic0 if topic0.startswith("0x") else "0x" + topic0
        f["topics"] = [t]
    retried = False
    try:
        logs = _eth("eth_getLogs", [f], timeout=25) or []
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        # Reth 会在结果数超限时给出建议区间，直接按它重试一次，而不是把错误抛给调用方
        m = re.search(r"range (\d+)-(\d+)", msg)
        if "exceeds max results" in msg and m:
            f = {"fromBlock": hex(int(m.group(1))), "toBlock": hex(int(m.group(2)))}
            if address:
                f["address"] = address
            if topic0:
                f["topics"] = ["0x" + topic0.lstrip("0x")]
            try:
                logs = _eth("eth_getLogs", [f], timeout=25) or []
                retried = True
            except Exception as exc2:  # noqa: BLE001
                return {"error": str(exc2)[:160], "filter": f}
        else:
            return {"error": msg[:160], "filter": f}
    out = []
    for lg in logs[:n]:
        entry = {"block_number": _eth_int(lg.get("blockNumber")), "tx_hash": lg.get("transactionHash"),
                 "address": lg.get("address"), "log_index": _eth_int(lg.get("logIndex")),
                 "topics": lg.get("topics"), "data": lg.get("data")}
        tps = lg.get("topics") or []
        if len(tps) >= 3 and (tps[0] or "").lower() == ERC20_TRANSFER:
            dec = _eth_token_decimals(lg.get("address"))
            amt = None
            try:
                amt = int(lg.get("data") or "0x0", 16)
            except ValueError:
                pass
            entry["decoded"] = {"event": "Transfer", "token": lg.get("address"),
                                "from": "0x" + tps[1][-40:], "to": "0x" + tps[2][-40:],
                                "amount_atomic": amt, "decimals": dec,
                                "amount_human": round(amt / 10 ** dec, 6) if (amt is not None and dec is not None) else None}
        out.append(entry)
    return {"filter": f, "auto_narrowed": retried, "matched": len(logs), "returned": len(out),
            "truncated": len(logs) > len(out), "logs": out,
            "note": "ERC-20 Transfer logs are decoded when they match the Transfer topic.",
            "source": "our Reth node (eth_getLogs)"}


def t_eth_get_block(height=None, block_hash=None):
    """以太坊区块详情：区块号、时间戳、交易数、gas 使用、base fee、出块者。"""
    if block_hash:
        b = _eth("eth_getBlockByHash", [block_hash, False])
        asked = {"block_hash": block_hash}
    else:
        tag = "latest"
        if height is not None:
            tag = height if isinstance(height, str) else hex(int(height))
        b = _eth("eth_getBlockByNumber", [tag, False])
        asked = {"block": tag}
    if not b:
        return {"found": False, **asked,
                "important": "our Reth node keeps block bodies from 15,500,000 (2022-09-14) onward; a miss below "
                              "that is outside our retention window, not proof the block does not exist"}
    ts = _eth_int(b.get("timestamp"))
    return {"found": True, "number": _eth_int(b.get("number")), "hash": b.get("hash"),
            "parent_hash": b.get("parentHash"), "timestamp": ts,
            "age_s": int(time.time() - ts) if ts else None,
            "tx_count": len(b.get("transactions") or []),
            "gas_used": _eth_int(b.get("gasUsed")), "gas_limit": _eth_int(b.get("gasLimit")),
            "gas_used_pct": round(100 * (_eth_int(b.get("gasUsed"), 0) / max(1, _eth_int(b.get("gasLimit"), 1))), 2),
            "base_fee_gwei": round(_eth_int(b.get("baseFeePerGas"), 0) / 1e9, 6) if b.get("baseFeePerGas") else None,
            "miner": b.get("miner"), "size_bytes": _eth_int(b.get("size")),
            "source": "our Reth node (eth_getBlockByNumber / eth_getBlockByHash)"}


def t_xmr_get_output_distribution(from_height=None, to_height=None):
    """Monero 输出分布（ring size / 匿名集规模）。

    ⚠️ 诚实边界：monerod 的输出分布数据滞后链尖的块数**随节点版本变化**（2026-10-19 实测约 102 块，
    历史上约 5,000 块），所以默认不从固定滞后量推算，而是从链尖往回探测到有数据的区间，
    并明确回报真实链尖、查询区间与滞后块数。
    """
    info = cached("xmr_info", 10, lambda: _xmr_rpc("get_info")) or {}
    # 2026-10-19 (ported QA P2-14): get_info.height is chain length (+1); use the real tip.
    tip, _hdr = cached("xmr_tip", 10, _xmr_tip)
    if not tip:
        tip = int(info.get("height") or 0) - 1
    if not tip:
        return {"available": False, "reason": "monerod helper RPC unreachable"}
    tip = int(tip)
    try:
        fr = int(from_height) if from_height is not None else None
        to = int(to_height) if to_height is not None else None
    except (TypeError, ValueError):
        return {"error": "from_height / to_height must be integers"}
    if fr is None or to is None:
        # Probe backwards instead of hard-coding a lag: the daemon keeps a moving window.
        r, fr, to = None, None, None
        lag = 200
        while lag <= 8000:
            _to = tip - lag
            try:
                _r = _xmr_rpc("get_output_distribution",
                              {"amounts": [0], "from_height": _to - 10, "to_height": _to,
                               "cumulative": False, "binary": False}, timeout=40)
            except Exception:  # noqa: BLE001
                _r = None
            dists = (_r or {}).get("distributions") or []
            if dists and (dists[0].get("distribution") or []):
                r, fr, to = _r, _to - 10, _to
                break
            lag *= 2
        if r is None:
            return {"available": False, "tip": tip,
                    "reason": "monerod returned no output distribution in the last 8,000 blocks"}
    else:
        if to < fr:
            return {"error": "to_height must be >= from_height"}
        if to - fr > 1000:
            return {"error": "range too wide (max 1000 blocks per call)"}
        r = _xmr_rpc("get_output_distribution",
                     {"amounts": [0], "from_height": fr, "to_height": to,
                      "cumulative": False, "binary": False}, timeout=40)
    dists = (r or {}).get("distributions") or []
    if not dists:
        return {"available": False, "tip": tip, "requested": {"from": fr, "to": to},
                "reason": ("monerod returned no distribution for this range - the distribution data lags the "
                           "chain tip; try a range further back")}
    d = dists[0]
    dist = d.get("distribution") or []
    return {
        "tip_height": tip,
        "queried_range": {"from": fr, "to": to},
        "data_lags_tip_by_blocks": tip - int(d.get("start_height") or fr),
        "start_height": d.get("start_height"),
        "outputs_before_start": d.get("base"),
        "distribution": dist,
        "largest_bucket_index": (max(range(len(dist)), key=lambda i: dist[i]) if dist else None),
        "how_to_read": ("monerod reports how many spendable outputs fall into each ring-size bucket at that "
                        "height; a larger tail means a bigger anonymity set for the largest ring size. "
                        "Values are raw counts from our own monerod - we do not reinterpret them into a "
                        "'privacy score'."),
        "source": "our monerod get_output_distribution (binary:false) via the read-only helper",
    }


def t_zec_get_treestate(block_hash=None, height=None):
    """Zcash 屏蔽池承诺树状态（Sapling / Orchard / Ironwood 的锚点根）。"""
    try:
        if not block_hash:
            if height is not None:
                block_hash = _zec("getblockhash", [int(height)])
            else:
                block_hash = _zec("getbestblockhash", [])
        ts = _zec("z_gettreestate", [block_hash], timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc)[:160]}
    if not ts:
        return {"found": False, "block_hash": block_hash}
    out = {"block_hash": ts.get("hash") or block_hash, "height": ts.get("height"),
           "time": ts.get("time"), "pools": {}}
    for pool in ("sapling", "orchard", "ironwood"):
        v = ts.get(pool)
        if isinstance(v, dict):
            c = v.get("commitments") or {}
            out["pools"][pool] = {"final_root": c.get("finalRoot"), "final_state": c.get("finalState")}
        elif v is not None:
            out["pools"][pool] = v
    out["how_to_read"] = ("Each root is the commitment-tree anchor for that shielded pool at this block. "
                          "Wallets and provers need an anchor that their node recognises; comparing roots "
                          "across blocks shows how the shielded sets advance.")
    out["source"] = "our Zebra node via the read-only helper (z_gettreestate)"
    return out


def t_zec_get_network_stats():
    """Zcash 全网与节点状态：内存池、节点版本、算力、难度、区块补贴与资助流向。"""
    out = {"chain": "ZEC"}
    for key, method, params in (("mempool", "getmempoolinfo", []),
                                ("network", "getnetworkinfo", []),
                                ("difficulty", "getdifficulty", []),
                                ("network_hashrate_hs", "getnetworkhashps", []),
                                ("blockchain", "getblockchaininfo", []),
                                ("subsidy", "getblocksubsidy", [])):
        try:
            out[key] = _zec(method, params)
        except Exception as exc:  # noqa: BLE001
            out[key] = {"error": str(exc)[:100]}
    bc = out.get("blockchain") if isinstance(out.get("blockchain"), dict) else {}
    if bc:
        out["height"] = bc.get("blocks")
        out["verification_progress"] = bc.get("verificationprogress")
        out["size_on_disk_gb"] = round((bc.get("size_on_disk") or 0) / 1e9, 1) if bc.get("size_on_disk") else None
    sub = out.get("subsidy") if isinstance(out.get("subsidy"), dict) else {}
    if sub:
        out["funding_streams"] = [{"recipient": f.get("recipient"), "value_zec": f.get("value"),
                                   "value_zat": f.get("valueZat"), "address": f.get("address")}
                                  for f in (sub.get("fundingstreams") or [])]
        out["lockbox_streams"] = [{"recipient": f.get("recipient"), "value_zec": f.get("value"),
                                   "value_zat": f.get("valueZat")}
                                  for f in (sub.get("lockboxstreams") or [])]
        # Zebra 的 getblocksubsidy 里区块产出叫 miner；没有 blocksubsidy 这个键
        out["block_reward_zec"] = sub.get("miner")
        out["founders_reward_zec"] = sub.get("founders")
        out["funding_streams_total_zec"] = sub.get("fundingstreamstotal")
    out["source"] = "our Zebra node via the read-only helper (getmempoolinfo / getnetworkinfo / getnetworkhashps / getblocksubsidy)"
    return out


def t_tron_list_proposals(limit=20):
    """TRON 治理提案：编号、状态、参数改动、赞成票与提案人。"""
    try:
        n = max(1, min(100, int(limit or 20)))
    except (TypeError, ValueError):
        n = 20
    d = cached("tron_proposals", 120, lambda: _tron("wallet/listproposals")) or {}
    ps = d.get("proposals") or []
    if not ps:
        return {"available": False, "reason": "proposal list unavailable"}
    by_state = {}
    for p in ps:
        st = p.get("state") or "UNKNOWN"
        by_state[st] = by_state.get(st, 0) + 1
    top = sorted(ps, key=lambda p: -len(p.get("approvals") or []))[:n]
    return {
        "total_proposals": len(ps),
        "by_state": by_state,
        "top_by_approvals": [{
            "proposal_id": p.get("proposal_id"),
            "proposer": _tron_b58(p.get("proposer_address") or ""),
            "approvals": len(p.get("approvals") or []),
            "state": p.get("state"),
            "create_time": p.get("create_time"),
            "expiration_time": p.get("expiration_time"),
            "parameters": ([{"key": x.get("key"), "value": x.get("value")}
                            for x in (p.get("parameters") or [])]
                           if isinstance(p.get("parameters"), list)
                           else [{"key": k, "value": v} for k, v in (p.get("parameters") or {}).items()]),
        } for p in top],
        "how_to_read": ("A proposal that reaches enough approvals changes a chain parameter at the next "
                        "maintenance window (tron_get_chain_economics reports the interval)."),
        "source": "our java-tron wallet/listproposals",
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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Mcp-Session-Id, Accept")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        plan = getattr(self, "_plan_name", None)
        if plan:
            self.send_header("X-MCP-Plan", plan)
            self.send_header("X-RateLimit-Limit", str(RATE_LIMIT_PER_MIN))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _plan(self):
        """2026-10-16：付费只走 x402，API key 档位已取消。

        恒为匿名；付费额度由 x402 信用余额控制（不再是速率档位）。
        仍然设置 `_plan_name` 以便 `_send()` 照常回 `X-MCP-Plan` 头——这是调用方与审计
        判断「这是免费档还是付费档」的唯一外部标识。
        """
        self._plan_name = "anon"
        return self._plan_name

    def _plan_legacy(self):
        key = self.headers.get("X-API-Key")
        if not key and "key=" in (self.path or ""):
            key = self.path.split("key=", 1)[1].split("&")[0]
        if key:
            self._plan_name = verify_key(key)
        else:
            self._plan_name = "anon"
        return self._plan_name

    def _rate_ok(self, limit):
        ip = self._request_ip()          # 2026-10-19: same trusted-IP rule as quota
        now = time.time()
        with _rl_lock:
            win = _rl.setdefault(ip, [])
            win[:] = [t for t in win if now - t < 60]
            if len(win) >= limit:
                return False
            win.append(now)
        return True

    def do_OPTIONS(self):
        # 2026-10-19 (ported QA P2-6): complete CORS preflight headers.
        # _send() already sets Access-Control-Allow-Methods for every response; only the
        # preflight-specific headers are added here (avoids a duplicate header).
        self._send(204, b"", extra={"Access-Control-Max-Age": "86400",
                                    "Allow": "POST, OPTIONS"})

    def do_DELETE(self):
        self._send(405, json.dumps({"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32000, "message": "DELETE not supported"}}),
                   extra={"Allow": "POST, OPTIONS"})

    def do_PUT(self):
        self._send(405, json.dumps({"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32000, "message": "PUT not supported"}}),
                   extra={"Allow": "POST, OPTIONS"})

    def do_HEAD(self):
        # headers only: a HEAD response must not carry a body
        self.send_response(405)
        self.send_header("Allow", "POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _credits(self):
        """GET /credits?payer=<address> — read-only credit balance for one payer.

        Address-keyed and derived from public on-chain payments; there is no
        account and no secret to authenticate with. Rate limited per IP.
        """
        if x402_pay is None or not x402_pay.enabled():
            return self._send(503, json.dumps({
                "error": "x402 is not enabled on this deployment"}), "application/json")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        payer = (q.get("payer") or [""])[0].strip()
        if not payer:
            return self._send(400, json.dumps({
                "error": "payer is required",
                "example": "/credits?payer=0x... (Base) or /credits?payer=<solana address>",
                "note": "use the address that signed the payment; no API key exists",
            }), "application/json")
        if len(payer) > 80 or any(ch.isspace() for ch in payer):
            return self._send(400, json.dumps({"error": "payer must be a single address"}),
                              "application/json")
        if not self._rate_ok(60):
            return self._send(429, json.dumps({"error": "rate limited (60/min per IP)"}),
                              extra={"Retry-After": "60"})
        try:
            out = x402_pay.credit_summary(payer)
        except Exception as exc:  # noqa: BLE001
            return self._send(500, json.dumps({"error": "%s: %s" % (type(exc).__name__, exc)[:200]}),
                              "application/json")
        return self._send(200, json.dumps(out, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        # 2026-10-17 修正：SSE 405 只对 MCP 端点本身生效。
        # 原先不判路径 → Accept 带 text/event-stream 的 GET 打在 /tools、/server.json、
        # /stats、/healthz 上也返回 405（公网 /mcp/tools 因此 405，外部探针 tools_json 连续失败）。
        _bare = (self.path or "/").split("?")[0] in ("", "/", "/mcp", "/mcp/")
        if _bare and "text/event-stream" in (self.headers.get("Accept") or "").lower():
            # 本服务不提供服务端推送流：按 Streamable HTTP 规范回 405，而不是一页 HTML
            # 2026-10-19 (ported QA P1-13): mark the representation as Accept-dependent so caches
            # (CF included) never serve the HTML landing page to an SSE client.
            return self._send(405, json.dumps({"jsonrpc": "2.0", "id": None, "error": {
                "code": -32000, "message": "GET SSE stream not supported; use POST"}}),
                extra={"Allow": "POST, OPTIONS", "Vary": "Accept"})
        if self.path.startswith("/healthz"):
            return self._send(200, json.dumps({"ok": True, "tools": len(TOOLS),
                                              "version": SERVER_INFO["version"]}))
        if self.path.split("?")[0].rstrip("/") in ("/credits", "/mcp/credits"):
            return self._credits()
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
            crows = "".join(
                f'<tr><td>{html.escape(c["client"])}</td><td>{c["calls"]}</td><td>{c["errors"]}</td></tr>'
                for c in d["top_clients"]) or '<tr><td colspan="3" class="muted">No calls yet</td></tr>'
            erows = "".join(
                f'<tr><td>{html.escape(e["reason"])}</td><td>{e["calls"]}</td></tr>'
                for e in d["top_errors"]) or '<tr><td colspan="2" class="muted">No errors</td></tr>'
            krows = "".join(
                f'<tr><td>{html.escape(c["client"])}</td><td>{c["handshakes"]}</td>'
                f'<td>{c["ips"]}</td><td>{html.escape(c["last_seen_utc"])}</td></tr>'
                for c in d["connectors"]) or '<tr><td colspan="4" class="muted">No handshakes yet</td></tr>'
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
<h2>Who is calling</h2>
<table><tr><th>Client (MCP clientInfo, else User-Agent)</th><th>Calls</th><th>Errors</th></tr>{crows}</table>
<h2>Failure reasons</h2>
<table><tr><th>Reason</th><th>Calls</th></tr>{erows}</table>
<h2>Handshakes (clients that connected — a handshake is not a tool call)</h2>
<table><tr><th>Client</th><th>Handshakes</th><th>Distinct IPs</th><th>Last seen (UTC)</th></tr>{krows}</table>
<p class="sub" style="margin-top:18px">JSON: <a href="/mcp/stats?hours={hours}&amp;format=json">/mcp/stats?format=json</a> ·
other windows: <a href="/mcp/stats?hours=1">1h</a> · <a href="/mcp/stats?hours=6">6h</a> · <a href="/mcp/stats?hours=24">24h</a> · <a href="/mcp/stats?hours=168">7d</a></p>
</div></body></html>"""
            return self._send(200, page, "text/html; charset=utf-8")
        if self.path.startswith("/tools"):
            return self._send(200, json.dumps({"tools": tools_list()}, ensure_ascii=False))
        if self.path.startswith("/server.json") or self.path.startswith("/.well-known/mcp"):
            return self._send(200, json.dumps(server_card(), ensure_ascii=False))
        return self._send(200, docs_html(), "text/html; charset=utf-8", {"Vary": "Accept"})

    def _request_ip(self):
        """Trusted client IP. 2026-10-19 (ported QA P1-15): the gateway now sets X-Real-IP
        from the socket peer for direct-origin clients and forwards the CF value for
        tunnel traffic; X-Real-IP therefore wins over client-supplied headers."""
        return (self.headers.get("X-Real-IP")
                or self.headers.get("CF-Connecting-IP")
                or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
                or self.client_address[0])

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_048_576:                       # 2026-10-19 (ported QA P2-5): request body cap
            return self._send(413, json.dumps({"jsonrpc": "2.0", "id": None,
                                               "error": {"code": -32000, "message": "request body too large"}}))
        raw = self.rfile.read(n) if n else b"{}"
        plan = self._plan()
        path = urllib.parse.urlsplit(self.path or "/").path
        if path not in ("", "/", "/mcp", "/mcp/"):
            return self._send(404, json.dumps({"jsonrpc": "2.0", "id": None,
                                               "error": {"code": -32000, "message": "not found"}}))
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype and ctype != "application/json":
            return self._send(415, json.dumps({"jsonrpc": "2.0", "id": None,
                                               "error": {"code": -32000,
                                                         "message": "Content-Type must be application/json"}}))
        _REQ.ip = self._request_ip()
        _REQ.plan = plan
        # 每次请求都有的两个身份线索，落审计用（ip_hash 看不出客户端是什么）
        _REQ.ua = (self.headers.get("User-Agent") or "")[:160]
        _REQ.client = ""
        _REQ.err = ""
        limit = RATE_LIMIT_PER_MIN       # 只有匿名档：付费额度由 x402 信用控制，不再用速率区分
        if not self._rate_ok(limit):
            return self._send(429, json.dumps({"jsonrpc": "2.0", "id": None,
                                               "error": {"code": -32000,
                                                         "message": f"rate limited ({limit}/min per IP); "
                                                                    f"paid access is via x402 and does not raise the per-IP rate limit"}}),
                              # 2026-10-19 (ported QA P2-3): actionable rate-limit metadata.
                              extra={"X-RateLimit-Remaining": "0", "Retry-After": "60"})
        try:
            msg = json.loads(raw.decode() or "{}")
        except Exception:  # noqa: BLE001
            return self._send(400, json.dumps({"jsonrpc": "2.0", "id": None,
                                               "error": {"code": -32700, "message": "parse error"}}))
        batch = isinstance(msg, list)
        msgs = msg if batch else [msg]
        if batch and (not msgs or len(msgs) > 50):
            return self._send(400, json.dumps({"jsonrpc": "2.0", "id": None,
                                               "error": {"code": -32600,
                                                         "message": "Invalid Request: batch must contain 1-50 elements"}}))
        started = time.time()
        responses = []
        for _m in msgs:
            if not isinstance(_m, dict):
                responses.append({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32600,
                                            "message": "Invalid Request: batch element must be an object"}})
                continue
            if _m.get("jsonrpc") not in (None, "2.0"):
                responses.append({"jsonrpc": "2.0", "id": _m.get("id"),
                                  "error": {"code": -32600, "message": "Invalid Request: jsonrpc must be 2.0"}})
                continue
            if not isinstance(_m.get("method"), str):
                responses.append({"jsonrpc": "2.0", "id": _m.get("id"),
                                  "error": {"code": -32600, "message": "Invalid Request: missing method"}})
                continue
            try:
                _r = handle(_m)
            except Exception as _exc:  # noqa: BLE001
                _r = {"jsonrpc": "2.0", "id": _m.get("id"),
                      "error": {"code": -32603, "message": "Internal error: %s" % type(_exc).__name__}}
            if _r is not None:
                responses.append(_r)
        for m, r in zip([x for x in msgs], responses or []):
            if isinstance(m, dict) and m.get("method") == "tools/call":
                name = ((m.get("params") or {}).get("name") or "?")
                ok = not ((r.get("result") or {}).get("isError") is True)
                log_call(name, ok, (time.time() - started) * 1000, self._request_ip(), plan,
                             getattr(_REQ, "ua", ""), getattr(_REQ, "client", ""), getattr(_REQ, "err", ""))
        if not responses:
            return self._send(202, b"")
        payload = responses if batch else responses[0]
        accept = (self.headers.get("Accept") or "").lower()
        body = json.dumps(payload, ensure_ascii=False)
        if "text/event-stream" in accept:
            sse = "event: message\ndata: " + body + "\n\n"
            return self._send(200, sse, "text/event-stream; charset=utf-8", {"Cache-Control": "no-store"})
        return self._send(200, body)


if __name__ == "__main__":
    init_usage()
    init_risk_db()
    init_web_db()
    if x402_pay is not None:
        try:
            x402_pay.init()
            print("[x402] ledger ready; enabled=%s" % x402_pay.enabled(), flush=True)
        except Exception as exc:  # noqa: BLE001
            print("[x402] init failed: %s" % exc, flush=True)
    class _RBHTTPServer(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 256      # 2026-10-19 (ported QA P2-18): was 5 -> SYN-retransmit stalls
        allow_reuse_address = True

    _RBHTTPServer((BIND, PORT), H).serve_forever()
