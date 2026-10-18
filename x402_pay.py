"""x402 payment layer for an MCP server - standard library only.

Design
------
1. **No new dependencies.** Two jobs: talk HTTP to an x402 facilitator, and keep a
   local sqlite ledger. It runs on a stock python3 and never imports an x402 SDK.
2. **Credit packs, not pay-per-call.** A settlement has gas cost; settling every
   $0.01 call loses money. One settlement buys N calls, amortising the gas.
3. **No API keys.** Credit is held against the paying address. The client keeps
   re-sending the same settled payment payload; each call spends one credit.
   Settlement is idempotent per payment nonce, so the payload is charged once.
4. **Spec-shaped PaymentRequired.** `structuredContent` and `content[0].text`
   carry the same object; `content[1]` is a human-readable explanation.

This module never touches a private key and never submits a transaction. All
on-chain settlement is delegated to the facilitator.

Configuration is entirely environment variables - see `examples/x402.env.example`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Any

_cache: dict[str, Any] = {"cfg": None, "ts": 0.0}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_json(name: str, default: Any) -> Any:
    raw = _env(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return default


def _truthy(name: str) -> bool:
    return _env(name).lower() in ("1", "true", "yes", "on")


def cfg() -> dict[str, Any]:
    """Build the config from environment variables (10 s cache).

    Required when enabled:
      RB_X402_FACILITATOR_URL   e.g. https://facilitator.example
      RB_X402_OPTIONS           JSON array, one object per accepted network/asset
      RB_X402_PACKS             JSON array of {"amount_atomic","calls","label"?}

    Optional:
      RB_X402_ENABLED=1         default: enabled iff facilitator_url is set
      RB_X402_FACILITATOR_TOKEN sent as x-internal-token (if your facilitator wants one)
      RB_X402_NETWORK           default network for a payload without `accepted.network`
      RB_X402_LEDGER_DB         default ./x402_credit.db
      RB_X402_TOPUP_URL         shown in the human-readable 402 text
      RB_X402_TIMEOUT           facilitator timeout seconds (60)
      RB_X402_MAX_TIMEOUT       paymentRequirements.maxTimeoutSeconds (300)
      RB_X402_EVM_RPC           JSON-RPC used for EIP-3009 reconciliation
      RB_X402_CLAIM=1           enable the cross-service "one payment once" claim
      RB_X402_SERVICE_NAME      name reported to the claim endpoint
    """
    now = time.time()
    if _cache["cfg"] is None or now - _cache["ts"] > 10:
        options = _env_json("RB_X402_OPTIONS", [])
        packs = _env_json("RB_X402_PACKS", [])
        facilitator = _env("RB_X402_FACILITATOR_URL")
        default_net = _env("RB_X402_NETWORK") or (options[0].get("network") if options else "")
        _cache["cfg"] = {
            "enabled": _truthy("RB_X402_ENABLED") or bool(facilitator and options),
            "facilitator_url": facilitator,
            "facilitator_token": _env("RB_X402_FACILITATOR_TOKEN"),
            "facilitator_timeout": int(_env("RB_X402_TIMEOUT", "60") or 60),
            "network": default_net,
            "payment_options": options,
            "packs": packs,
            "ledger_db": _env("RB_X402_LEDGER_DB", "x402_credit.db") or "x402_credit.db",
            "topup_url": _env("RB_X402_TOPUP_URL"),
            "max_timeout_seconds": int(_env("RB_X402_MAX_TIMEOUT", "300") or 300),
            "evm_rpc": _env("RB_X402_EVM_RPC", "https://mainnet.base.org") or "https://mainnet.base.org",
            "claim": _truthy("RB_X402_CLAIM"),
            "service_name": _env("RB_X402_SERVICE_NAME", "mcp") or "mcp",
        }
        _cache["ts"] = now
    return _cache["cfg"]


def enabled() -> bool:
    c = cfg()
    return (bool(c.get("enabled")) and bool(c.get("facilitator_url"))
            and bool(c.get("payment_options")) and bool(c.get("packs")))


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(cfg()["ledger_db"], timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init() -> None:
    """建表。幂等。"""
    con = _db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS x402_payments(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts INTEGER NOT NULL,
      network TEXT NOT NULL,
      payer TEXT NOT NULL,
      pay_to TEXT NOT NULL,
      amount TEXT NOT NULL,
      nonce TEXT NOT NULL,
      tx_hash TEXT,
      ok INTEGER NOT NULL,
      reason TEXT,
      tool TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_x402_nonce ON x402_payments(network, nonce);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_x402_tx ON x402_payments(tx_hash) WHERE tx_hash IS NOT NULL AND tx_hash<>'';

    CREATE TABLE IF NOT EXISTS x402_credits(
      payer TEXT PRIMARY KEY,
      network TEXT NOT NULL,
      balance INTEGER NOT NULL DEFAULT 0,
      updated_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS x402_credit_events(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts INTEGER NOT NULL,
      payer TEXT NOT NULL,
      delta INTEGER NOT NULL,
      reason TEXT NOT NULL,
      tool TEXT
    );

    """)
    con.commit()
    con.close()


# ---------------------------------------------------------------- 付款要求
def packs() -> list[dict[str, Any]]:
    """可选的价格档位。规范允许 accepts[] 给多个选项，由客户端挑一个。"""
    c = cfg()
    if c.get("packs"):
        return c["packs"]
    # 兼容旧的单档配置
    return [{"amount_atomic": str(c.get("pack_price_atomic", "1000000")),
             "calls": int(c.get("pack_calls", 100)), "label": "default"}]


def options() -> list[dict[str, Any]]:
    """可选的支持网络/资产（2026-10-16 起支持 Base + Solana 双网络）。

    新配置用 `payment_options` 数组；旧配置（单网络、顶层 network/asset/pay_to）自动兼容。
    """
    c = cfg()
    if c.get("payment_options"):
        return c["payment_options"]
    if not c.get("network") or not c.get("asset") or not c.get("pay_to"):
        return []
    return [{
        "network": c["network"],
        "asset": c["asset"],
        "asset_name": c.get("asset_name", "USD Coin"),
        "asset_version": c.get("asset_version", "2"),
        "pay_to": c["pay_to"],
        "decimals": c.get("decimals", 6),
        "symbol": c.get("asset_symbol", "USDC"),
    }]


def option_for(network: str) -> dict[str, Any] | None:
    return next((o for o in options() if o.get("network") == network), None)


def requirement(amount_atomic: str, option: dict[str, Any] | None = None) -> dict[str, Any]:
    """组装一条 paymentRequirements。EVM 走 EIP-712 域，Solana 走 feePayer。"""
    c = cfg()
    o = option or options()[0]
    net = o["network"]
    req: dict[str, Any] = {
        "scheme": "exact",
        "network": net,
        "amount": str(amount_atomic),
        "asset": o["asset"],
        "payTo": o["pay_to"],
        "maxTimeoutSeconds": int(c.get("max_timeout_seconds", 300)),
    }
    if str(net).startswith("solana:"):
        # SVM：客户端把交易里留好 fee payer 槽位，由 facilitator 签名并付费
        extra: dict[str, Any] = {"feePayer": o["fee_payer"]}
        if o.get("memo"):
            extra["memo"] = o["memo"]
    else:
        # EIP-712 域必须带上：缺了 facilitator 会直接判 missing_eip712_domain
        extra = {"name": o.get("asset_name", "USD Coin"), "version": str(o.get("asset_version", "2"))}
    req["extra"] = extra
    return req


def all_requirements() -> list[dict[str, Any]]:
    return [requirement(p["amount_atomic"], o) for o in options() for p in packs()]


def calls_for_amount(amount: Any) -> int:
    """客户实际选了哪一档 → 该给多少次调用。不匹配一律 0（由 facilitator 的白名单兜底）。"""
    try:
        a = int(str(amount))
    except (TypeError, ValueError):
        return 0
    for p in packs():
        if int(str(p["amount_atomic"])) == a:
            return int(p["calls"])
    return 0


def payment_required(tool: str, reason: str) -> dict[str, Any]:
    """按 MCP 传输规范构造 PaymentRequired（v2）。

    `reason` 既可能是纯文本（旧路径），也可能是 server.py 传来的 JSON 信封
    `{"reason": 错误码, "detail": 详细原因, "network": 网络}` —— 后者让付款方
    既能用代码分支处理，也能看懂"为什么被拒"。
    """
    c = cfg()
    first_opt = (options() or [{}])[0]
    dec = 10 ** int(first_opt.get("decimals", c.get("decimals", 6)))
    opts = packs()
    code, detail, net_hint = "", "", ""
    if isinstance(reason, str) and reason.strip().startswith("{"):
        try:
            env = json.loads(reason)
            code = str(env.get("reason") or "")
            detail = str(env.get("detail") or "")
            net_hint = str(env.get("network") or "")
        except Exception:  # noqa: BLE001
            pass
    if detail:
        detail = _clean_detail(detail)
    if code:
        if code in ("settlement_pending", "settle_unreachable", "verify_unreachable"):
            # 结算可能已经上链：此时让客户重新签名 = 重复扣款风险。只让他用同一载荷重试。
            howto = ("do NOT sign a new authorization - retry this same call with the SAME payment "
                     "payload in a minute; it is checked on-chain and credited once if it settled.")
        elif net_hint.startswith("eip155:"):
            howto = "sign a fresh EIP-3009 authorization and retry."
        else:
            howto = "sign a fresh transfer transaction and retry."
        err_text = ("x402 payment rejected: %s%s. Re-read accepts[] from structuredContent, %s"
                    % (code, (": " + detail) if detail else "", howto))
    else:
        err_text = reason or "Payment required to access this resource"
    obj = {
        "x402Version": 2,
        "error": err_text,
        "resource": {
            "url": "mcp://tool/%s" % tool,
            "description": "Metered MCP tool: %s" % tool,
            "mimeType": "application/json",
        },
        "accepts": all_requirements(),
    }
    # 机器可读：错误码 + 详细原因（付款方不必再解析自然语言）
    if code:
        obj["reason"] = code
    if detail:
        obj["detail"] = detail[:300]
    pretty = "; ".join("%s %s = %d calls" % (p["amount_atomic"] and
                                             float(p["amount_atomic"]) / dec,
                                             c.get("asset_symbol", "USDC"), p["calls"]) for p in opts)
    nets = ", ".join("%s → %s" % (o["network"], o["pay_to"]) for o in options())
    human = (
        "%s\n\n"
        "Machine payment (x402): buy a credit pack — %s — on any of the accepted networks "
        "(%s), then retry this same call with the payload in "
        'params._meta["x402/payment"]. Full requirements are in structuredContent.accepts[]. '
        "There is no API key - the settled payload keeps unlocking calls until the pack is spent."
        % (obj["error"], pretty, nets)
    )
    topup = c.get("topup_url")
    if topup:
        human += " Pricing: %s" % topup
    return {"isError": True, "structuredContent": obj,
            "content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)},
                        {"type": "text", "text": human}]}


def _clean_detail(s: str) -> str:
    """把底层报错整理成付款方读得懂的一句话。

    EVM 的 invalidMessage 常形如 `('execution reverted: ERC20: ...', '0x08c379a0…')`，
    其中一长串十六进制是 revert data，对付款方没有信息量，去掉。
    """
    s = " ".join(str(s).split())
    s = re.sub(r",?\s*'?0x[0-9a-fA-F]{40,}'?", "", s)      # 去掉 revert data 块
    s = re.sub(r"0x[0-9a-fA-F]{40,}", "<hex>", s)
    s = s.strip().strip("'\"")
    while s[:1] in ("(", "["):               # 去掉元组/列表外壳
        s = s[1:].lstrip()
    while s and s[-1] in "')]., ":           # 去掉右括号/引号/逗号残留
        s = s[:-1].rstrip()
    return s.strip("'\"").strip()[:200]


# ---------------------------------------------------------------- facilitator 调用
def _fac(path: str, body: dict[str, Any]) -> dict[str, Any]:
    c = cfg()
    req = urllib.request.Request(
        c["facilitator_url"].rstrip("/") + path,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json",
                 "x-internal-token": c.get("facilitator_token", "")},
        method="POST")
    with urllib.request.urlopen(req, timeout=int(c.get("facilitator_timeout", 60))) as r:
        return json.loads(r.read() or b"{}")


def verify(payload: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    return _fac("/verify", {"x402Version": 2, "paymentPayload": payload, "paymentRequirements": req})


def settle(payload: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    return _fac("/settle", {"x402Version": 2, "paymentPayload": payload, "paymentRequirements": req})


# ---------------------------------------------------------------- 跨服务入账唯一性
# GLOBAL-CLAIM-2026-10-18：如果多个 MCP 服务共用同一组收款地址，它们各自记一本账，
# 同一笔链上付款就会在两边分别入账 = 一次付款换两份信用（我们实测发生过）。
# 用一个结算网关上的 (network, tx_hash) 全局登记处可以避免；单服务部署无需开启。
def _global_claim(network: str, tx: str, payer: str = "") -> dict[str, Any]:
    """向结算网关登记 (network, tx_hash)。

    返回 {"ok": True}                    本服务持有，可以入账
         {"ok": False, "owner": "<service>"} 已被别家入账，禁止重复发额度
         {"ok": None, "reason": "..."}    登记处不可达 / 无 tx：调用方按 fail-open 处理
    """
    if not tx:
        return {"ok": None, "reason": "no_tx"}
    c = cfg()
    service = c.get("service_name") or "mcp"
    req = urllib.request.Request(
        c["facilitator_url"].rstrip("/") + "/claim",
        data=json.dumps({"network": network, "tx": tx,
                         "service": service, "payer": payer}).encode(),
        headers={"content-type": "application/json",
                 "x-internal-token": c.get("facilitator_token", "")},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            d = json.loads(exc.read() or b"{}")
        except Exception:  # noqa: BLE001
            return {"ok": None, "reason": "claim_http_%s" % exc.code}
    except Exception as exc:  # noqa: BLE001
        return {"ok": None, "reason": "claim_unreachable: %s" % str(exc)[:100]}
    if d.get("ok"):
        return {"ok": True}
    return {"ok": False, "owner": d.get("owner") or "unknown"}


def _claim_gate(network: str, tx: str, payer: str) -> tuple[dict[str, Any] | None, str]:
    """入账前的唯一性闸门。返回 (阻断结果 or None, 入账 reason 后缀)。"""
    if not cfg().get("claim"):
        # 单服务部署不需要跨服务去重；开启 RB_X402_CLAIM=1 才会调用 /claim。
        return (None, "")
    r = _global_claim(network, tx, payer)
    if r.get("ok") is False:
        owner = r.get("owner") or "another service"
        return ({"ok": False, "reason": "already_credited_elsewhere", "network": network,
                 "payer": payer, "owner": owner, "tx": tx,
                 "detail": ("this on-chain payment was already credited by %s. One payment buys "
                            "one credit pack; spend it through %s, or contact support."
                            % (owner, owner))}, "")
    return (None, "" if r.get("ok") else ":claim_unverified")


# ---------------------------------------------------------------- 账本
def _payer_of(payload: dict[str, Any]) -> str:
    """EVM：授权里的 from。Solana：载荷只有交易，payer 由 facilitator verify 回传（见 try_pay）。"""
    try:
        return str(payload["payload"]["authorization"]["from"])
    except Exception:
        return ""


def _nonce_of(payload: dict[str, Any]) -> str:
    """EVM：EIP-3009 nonce。Solana：交易字节的 sha256（同一笔重复提交必得同一值，天然幂等键）。"""
    try:
        return str(payload["payload"]["authorization"]["nonce"])
    except Exception:
        pass
    tx = (payload.get("payload") or {}).get("transaction")
    if isinstance(tx, str) and tx:
        return hashlib.sha256(tx.encode()).hexdigest()
    return ""


def _seen(network: str, nonce: str) -> bool:
    if not nonce:
        return False
    con = _db()
    try:
        return con.execute("SELECT 1 FROM x402_payments WHERE network=? AND nonce=? AND ok=1",
                           (network, nonce)).fetchone() is not None
    finally:
        con.close()


def _record(network: str, payer: str, pay_to: str, amount: str, nonce: str,
            tx: str, ok: bool, reason: str, tool: str) -> bool:
    """写账本（upsert）。返回 True 表示这一次把该 nonce 从「未入账」变成了「已入账」。

    2026-10-17 修复：旧版 INSERT OR IGNORE 让第一条失败记录永久占位，同一笔付款
    之后确认成功也写不进去。现在失败记录可被后续成功覆盖；已成功的记录不会被改动。
    """
    con = _db()
    try:
        before = con.execute("SELECT ok FROM x402_payments WHERE network=? AND nonce=?",
                             (network, nonce)).fetchone()
        if before is not None and int(before["ok"]) == 1:
            return False
        con.execute(
            "INSERT INTO x402_payments(ts,network,payer,pay_to,amount,nonce,tx_hash,ok,reason,tool)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(network, nonce) DO UPDATE SET ok=excluded.ok, reason=excluded.reason,"
            " tx_hash=COALESCE(excluded.tx_hash, x402_payments.tx_hash), ts=excluded.ts"
            " WHERE x402_payments.ok=0",
            (int(time.time()), network, payer, pay_to, amount, nonce, tx or None, 1 if ok else 0, reason, tool))
        con.commit()
        return bool(ok)
    finally:
        con.close()


def _pending_row(network: str, nonce: str):
    """该 nonce 是否有一条曾经通过 facilitator verify、但未入账的记录。"""
    con = _db()
    try:
        return con.execute("SELECT * FROM x402_payments WHERE network=? AND nonce=? AND ok=0",
                           (network, nonce)).fetchone()
    finally:
        con.close()


def _settled_row(network: str, nonce: str) -> dict[str, Any] | None:
    """该 (network, nonce) 是否已经成功入账过；返回 dict（连接关掉后仍可用）。"""
    con = _db()
    try:
        row = con.execute(
            "SELECT payer, tx_hash, amount FROM x402_payments WHERE network=? AND nonce=? AND ok=1",
            (network, nonce)).fetchone()
        return dict(row) if row is not None else None
    finally:
        con.close()


def onchain_authorization_used(network: str, asset: str, payer: str, nonce: str) -> bool | None:
    """EIP-3009 authorizationState(authorizer, nonce)：True=已在链上使用，False=未使用，None=查不到。

    只读 eth_call，走公共 RPC（默认 mainnet.base.org），不依赖 facilitator。
    """
    if not str(network).startswith("eip155:"):
        return None
    try:
        a = payer.lower().replace("0x", "").rjust(64, "0")
        n = nonce.lower().replace("0x", "").rjust(64, "0")
        if len(a) != 64 or len(n) != 64:
            return None
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": asset, "data": "0xe94a0102" + a + n}, "latest"]}
        req = urllib.request.Request(cfg()["evm_rpc"], data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json",
                                              "user-agent": "mcp-x402/1"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read() or b"{}").get("result")
        if not isinstance(res, str) or not res.startswith("0x"):
            return None
        return int(res, 16) != 0
    except Exception:  # noqa: BLE001
        return None


def _auth_matches(payload: dict[str, Any], req: dict[str, Any]) -> bool:
    """载荷里的授权确实是付给我们、金额正确（链上兜底入账前的必要条件）。"""
    try:
        au = payload["payload"]["authorization"]
        return (str(au["to"]).lower() == str(req["payTo"]).lower()
                and int(str(au["value"])) == int(str(req["amount"])))
    except Exception:  # noqa: BLE001
        return False


def _onchain_settled(net: str, opt: dict[str, Any], payer: str, nonce: str, tries: int = 1) -> bool:
    for i in range(max(1, tries)):
        st = onchain_authorization_used(net, opt.get("asset", ""), payer, nonce)
        if st:
            return True
        if i + 1 < tries:
            time.sleep(3)
    return False


def _credit(payer: str, network: str, delta: int, reason: str, tool: str) -> int:
    con = _db()
    try:
        con.execute("INSERT INTO x402_credits(payer,network,balance,updated_at) VALUES(?,?,?,?)"
                    " ON CONFLICT(payer) DO UPDATE SET balance=balance+excluded.balance, updated_at=excluded.updated_at",
                    (payer, network, delta, int(time.time())))
        con.execute("INSERT INTO x402_credit_events(ts,payer,delta,reason,tool) VALUES(?,?,?,?,?)",
                    (int(time.time()), payer, delta, reason, tool))
        con.commit()
        return con.execute("SELECT balance FROM x402_credits WHERE payer=?", (payer,)).fetchone()["balance"]
    finally:
        con.close()


def balance(payer: str) -> int:
    con = _db()
    try:
        row = con.execute("SELECT balance FROM x402_credits WHERE payer=?", (payer,)).fetchone()
        return int(row["balance"]) if row else 0
    finally:
        con.close()


def spend(payer: str, tool: str) -> int:
    """原子扣 1 次信用，返回剩余；余额不足返回 -1。

    2026-10-17：旧写法是「先 SELECT 余额、再 UPDATE」，两个并发请求可以同时看到余额 1
    并各自扣一次，把余额扣成 -1（等于一次付款服务两次）。现在把判断和扣减合并成一条
    带 `WHERE balance>0` 的 UPDATE，由 sqlite 的行锁保证只有一个请求能扣到。
    """
    con = _db()
    try:
        cur = con.execute(
            "UPDATE x402_credits SET balance=balance-1, updated_at=? WHERE payer=? AND balance>0",
            (int(time.time()), payer))
        if cur.rowcount == 0:
            con.rollback()
            return -1
        con.execute("INSERT INTO x402_credit_events(ts,payer,delta,reason,tool) VALUES(?,?,?,?,?)",
                    (int(time.time()), payer, -1, "call", tool))
        con.commit()
        row = con.execute("SELECT balance FROM x402_credits WHERE payer=?", (payer,)).fetchone()
        return int(row["balance"]) if row else -1
    finally:
        con.close()


# ---------------------------------------------------------------- 主流程
def try_pay(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    """客户带了付款来：verify → settle → 记信用（**不签发 key**）。

    已结算过的载荷（EVM 与 SVM 都一样）在这里短路：返回 replayed=True，由调用方扣 1 次信用。
    返回 {"ok": True, "payer":..., "tx":..., "balance":...} 或
         {"ok": False, "reason":...}
    注意：**只做付款与入账，不执行工具**——工具由调用方在拿到 ok 之后执行，
    这样上游失败时不会白扣客户的钱。
    """
    c = cfg()
    nonce = _nonce_of(payload)
    if not nonce:
        return {"ok": False, "reason": "invalid_payload", "network": "",
                "detail": "付款载荷里既没有授权 nonce，也没有可识别的交易"}
    # 客户选了哪一个网络 + 哪一档？前者决定用哪份 requirements，后者决定入账多少次调用。
    accepted = payload.get("accepted") or {}
    amount = accepted.get("amount")
    net = accepted.get("network") or c["network"]
    opt = option_for(net)
    if opt is None:
        offered = ", ".join(o["network"] for o in options())
        return {"ok": False, "reason": "invalid_network", "network": net,
                "detail": "%s 不在本服务支持的网络里（可选：%s）" % (net, offered)}
    calls = calls_for_amount(amount)
    if calls <= 0:
        amounts = ", ".join(str(p["amount_atomic"]) for p in packs())
        return {"ok": False, "reason": "invalid_payment_requirements", "network": net,
                "detail": "付款金额 %r 不在价目表里（可选：%s）" % (amount, amounts)}
    req = requirement(str(amount), opt)
    payer = _payer_of(payload)          # EVM 直接可得；SVM 的载荷里只有交易字节，payer 为空
    # 2026-10-17 修复（真实 Solana 付款复现）：旧逻辑要求 payer 非空才走幂等分支，而 SVM 载荷
    # 天然拿不到 payer，于是重放时跳过幂等、把这笔**已经上链**的交易又送去 facilitator 模拟，
    # 得到 "already processed"，客户明明有额度却用不了。现在先按 (network, nonce) 查账本，
    # 把当初入账的 payer 取回来：只要这笔结算过，就只扣信用，永远不再模拟。
    settled = _settled_row(net, nonce)
    if settled is not None:
        payer = payer or str(settled.get("payer") or "")
        if payer:
            return {"ok": True, "payer": payer, "tx": settled.get("tx_hash") or "",
                    "balance": balance(payer), "replayed": True, "network": net}

    # 2026-10-17：之前判过「待结算/失败」的同一笔授权再次到达 → 先上链查。
    # 已被使用 = 钱已到账，直接入账发额度（不再要求客户重签，杜绝重复扣款）。
    if payer and _auth_matches(payload, req):
        prev = _pending_row(net, nonce)
        if prev is not None and _onchain_settled(net, opt, payer, nonce):
            return _credit_onchain(net, payer, req, nonce, calls, tool)

    try:
        v = verify(payload, req)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "verify_unreachable", "network": net,
                "detail": "结算校验服务暂时不可达：%s" % str(exc)[:160]}
    if not v.get("isValid"):
        return {"ok": False, "reason": v.get("invalidReason") or "invalid_payment",
                "network": net, "detail": str(v.get("invalidMessage") or "")[:300]}
    payer = payer or str(v.get("payer") or "")
    if not payer:
        return {"ok": False, "reason": "invalid_payload", "network": net,
                "detail": "校验通过但无法从载荷中确定付款方地址"}
    if _seen(net, nonce):
        # 校验已过但已入过账（例如同一笔交易被重放）→ 直接返回既有结果
        con = _db()
        try:
            row = con.execute("SELECT tx_hash FROM x402_payments WHERE network=? AND nonce=? AND ok=1",
                              (net, nonce)).fetchone()
        finally:
            con.close()
        return {"ok": True, "payer": payer, "tx": row["tx_hash"] if row else "",
                "balance": balance(payer), "replayed": True, "network": net}

    try:
        s = settle(payload, req)
    except Exception as exc:  # noqa: BLE001
        _record(net, payer, req["payTo"], req["amount"], nonce, "", False,
                "settle_unreachable", tool)
        if _auth_matches(payload, req) and _onchain_settled(net, opt, payer, nonce, tries=3):
            return _credit_onchain(net, payer, req, nonce, calls, tool)
        return {"ok": False, "reason": "settle_unreachable", "network": net, "payer": payer,
                "detail": "上链结算服务暂时不可达：%s" % str(exc)[:160]}
    if not s.get("success"):
        _record(net, payer, req["payTo"], req["amount"], nonce, s.get("transaction") or "", False,
                s.get("errorReason") or "settle_failed", tool)
        # facilitator 提交成功但回读确认失败（如 RPC 403）时，链上可能已经扣款：上链兜底
        if _auth_matches(payload, req) and _onchain_settled(net, opt, payer, nonce, tries=3):
            return _credit_onchain(net, payer, req, nonce, calls, tool, s.get("transaction") or "")
        return {"ok": False, "reason": s.get("errorReason") or "settle_failed", "network": net,
                "payer": payer, "detail": str(s.get("errorMessage") or "")[:300]}

    tx = s.get("transaction", "")
    block, claim_suffix = _claim_gate(net, tx, payer)   # GLOBAL-CLAIM-2026-10-18
    if block is not None:
        _record(net, payer, req["payTo"], req["amount"], nonce, tx, False,
                "claimed_by:%s" % (block.get("owner") or ""), tool)
        return block
    if _record(net, payer, req["payTo"], req["amount"], nonce, tx, True, "", tool):
        bal = _credit(payer, net, calls, "x402_settle" + claim_suffix, tool)
    else:
        bal = balance(payer)
    # 2026-10-17：不再签发 API key（与 llms.txt「没有 API key」一致）
    return {"ok": True, "payer": payer, "tx": tx, "network": net,
            "balance": bal, "pack_calls": calls, "amount": req["amount"]}


def _credit_onchain(net: str, payer: str, req: dict[str, Any], nonce: str, calls: int,
                    tool: str, tx: str = "") -> dict[str, Any]:
    """链上确认授权已被使用：把账本改成已入账并发放额度（同一 nonce 只发一次）。"""
    block, claim_suffix = _claim_gate(net, tx, payer)   # GLOBAL-CLAIM-2026-10-18
    if block is not None:
        _record(net, payer, req["payTo"], req["amount"], nonce, tx, False,
                "claimed_by:%s" % (block.get("owner") or ""), tool)
        return block
    if _record(net, payer, req["payTo"], req["amount"], nonce, tx, True, "onchain_authorization_used", tool):
        bal = _credit(payer, net, calls, "x402_onchain_reconcile" + claim_suffix, tool)
    else:
        bal = balance(payer)
    if not tx:   # 2026-10-17b：回执 transaction 用账本里已存的 tx_hash
        con = _db()
        try:
            r = con.execute("SELECT tx_hash FROM x402_payments WHERE network=? AND nonce=? AND ok=1",
                            (net, nonce)).fetchone()
            tx = (r["tx_hash"] if r else "") or ""
        finally:
            con.close()
    return {"ok": True, "payer": payer, "tx": tx, "network": net, "balance": bal,
            "pack_calls": calls, "amount": req["amount"], "reconciled_onchain": True}
