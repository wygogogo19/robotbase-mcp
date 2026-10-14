# RobotBase MCP Server

**The semantic risk & verification layer for Web3 agents — one read-only MCP endpoint.**

**免鉴权 · 无追踪 · 只读** ｜ 自建裸金属全节点，对 Agent 暴露统一的「验证 + 风控」语义入口

Live today: **Bitcoin · Ethereum · Monero · Zcash**. Provisioning: **Solana · Base · Tron**.

[![Tools](https://img.shields.io/badge/tools-13-brightgreen)](https://robotbase.cc/mcp/tools)
[![Protocol](https://img.shields.io/badge/MCP-2025--06--18-blue)](https://modelcontextprotocol.io)
[![Transport](https://img.shields.io/badge/transport-streamable--http-orange)](https://robotbase.cc/mcp)
[![Glama](https://glama.ai/mcp/servers/wygogogo19/robotbase-mcp/badges/score.svg)](https://glama.ai/mcp/servers/wygogogo19/robotbase-mcp)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.wygogogo19%2Frobotbase--mcp-blue)](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.wygogogo19/robotbase-mcp)

Hosted endpoint: **`https://robotbase.cc/mcp`** · Landing page: <https://robotbase.cc/mcp> · Server card: <https://robotbase.cc/mcp/server.json> · Live usage audit: <https://robotbase.cc/mcp/stats>

Listed in the **official MCP Registry** as `io.github.wygogogo19/robotbase-mcp` · indexed by **Glama** · published on **Smithery** (`wygogogo/robotbase-mcp`).

---

## Why

An autonomous agent handling crypto has exactly two blocking questions: **did this payment actually settle**, and **what will the next one cost**. Ask an LLM those today and it has nowhere reliable to look — the MCP ecosystem is rich in wrappers around hosted indexers and thin on self-run infrastructure.

This server answers both with **13 strictly read-only tools** backed by our own full nodes and local indexes: `btc_get_transaction` for the settlement check, `btc_get_fee_rates` and `btc_get_mempool_state` for the price of the next one, `zec_get_pool_status` for Zcash shielded-pool supply, and node telemetry for the rest of the roster.

## Pricing

The hosted endpoint is **free during launch**: anonymous access, 120 req/min per IP, no key, no signup, no card.

When billing opens it will be a **prepaid API key topped up in USDT**, metered per call — the only model every mainstream MCP client can actually use today. Machine-to-machine X402 settlement stays a lab track until mainstream clients can answer a `402 Payment Required` challenge.

## Connect (3 lines)

```json
{
  "mcpServers": {
    "robotbase": { "url": "https://robotbase.cc/mcp" }
  }
}
```

Works with Claude Desktop, Cursor, VS Code (remote MCP), and any MCP client supporting Streamable HTTP (`2025-06-18`). No API key needed.

```bash
curl -s https://robotbase.cc/mcp -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"btc_get_fee_rates","arguments":{}}}'
```

## Tools (13)

| Tool | What it does | Typical question |
|---|---|---|
| `list_chains` | Every chain on the roster with its state (`live\|provisioning\|planned`) and availability | "Which chains do you support? Are they online?" |
| `chain_status(chain)` | Node height, sync progress, peers, mempool, version (`btc\|eth\|xmr\|zec\|base\|sol\|tron`) | "Is the Monero node synced?" |
| `robotbase_capabilities` | What each node can and cannot answer, incl. pruning/retention limits | "Can you still read that far back?" |
| `robotbase_services` | Gateway-wide service health | "Give me an overall health check" |
| `btc_get_fee_rates` | Fees for 1/2/3/6/12/24-block targets + mempool floor, in sat/vB | "What fee should I pay right now?" |
| `btc_get_mempool_state` | Mempool depth, capacity load, min fee, total fees + busy/normal verdict | "Is Bitcoin congested right now?" |
| `btc_get_transaction(txid)` | BTC transaction: confirmations, block, I/O summary | "Did this payment actually settle?" |
| `btc_get_block(height\|blockhash)` | Block summary (defaults to tip) | "How many txs in the latest block?" |
| `btc_get_address(address)` | Balance, UTXOs, tx count, recent txs (P2PKH/P2SH/bech32/bech32m) | "How much BTC is in this address?" |
| `zec_get_pool_status` | Zcash node state **plus all six value pools** (transparent/sprout/sapling/orchard/ironwood/lockbox) with 1h/24h deltas | "How much ZEC is shielded, and is it growing?" |
| `zec_get_recent_blocks(n)` | Recent Zcash blocks | "Are ZEC blocks healthy?" |
| `xmr_get_node_status` | Monero node: height vs target, sync flag, difficulty, txpool, database size, monerod version, peers | "Is the Monero node synced?" |
| `broadcast_raw_transaction` | Relay an already-signed BTC transaction through our own node (testmempoolaccept first; operator-gated) | "Broadcast this signed tx for me" |

Boundary worth stating plainly: RobotBase does **not** index Monero transactions, wallets or ring signatures, and does **not** label or risk-score Bitcoin addresses. Where a node cannot answer, the tool says so instead of guessing.

## Beyond a generic reader

Most MCP servers wrap a third-party API. These come from infrastructure we operate, which is why they are hard to find elsewhere:

1. **Settlement verification** — `btc_get_transaction` / `btc_get_block` / `btc_get_address` answer the first question an agent has once a payment is claimed, from our own `txindex` node.
2. **Fee & mempool oracles** — `btc_get_fee_rates` + `btc_get_mempool_state` answer the second: what the next transaction will cost, and whether now is a good moment.
3. **Local indexes** — `zec_get_pool_status` reads our own Zebra node and value-pool index (all six pools with deltas) rather than a public explorer.
4. **Optional relay** — `broadcast_raw_transaction` lets an agent sign locally and use us purely as a high-availability broadcast path; it is disabled unless the operator sets `RB_ENABLE_BROADCAST=1`.

## Architecture

```
   AI Agent (Claude / Cursor / VS Code / custom)
            │  HTTPS · MCP Streamable HTTP (2025-06-18)
            ▼
   https://robotbase.cc/mcp          ← hosted, no auth required
            │
            ▼
   MCP server (dependency-free Python, this repo)
            ├── BTC  : Bitcoin Core RPC + electrs (address/tx index)
            ├── ETH  : Reth (execution) + Lighthouse (consensus)
            ├── XMR  : monerod JSON-RPC (node telemetry only)
            ├── ZEC  : Zebra RPC (valuePools → shielded-pool supply)
            ├── SOL / BASE / TRON : provisioning — light API aggregation refined in our middle layer
            └── meta : gateway service aggregation
```

## Privacy & limits

- **No auth** for anonymous use; optional `X-API-Key` header (or `?key=`) raises the limit.
- Anonymous **120 req/min per IP**; with API key **600 req/min per IP** (`X-MCP-Plan` / `X-RateLimit-Limit` response headers).
- Audit stores only a **truncated SHA-256 of the client IP** — never query parameters.
- **Read-only**: no trading, signing, custody, or write methods. BTC transactions are looked up with `txindex`.
- Responses are trimmed to ~3.8 KB per call to protect LLM context.

## Self-hosting (this repo)

`server.py` is a dependency-free reference implementation that talks to **your own** nodes. Configure everything with environment variables:

```bash
export RB_BIND=0.0.0.0            # bind address (default 127.0.0.1)
export RB_PORT=8090               # listen port
export RB_BTC_RPC_URL=http://127.0.0.1:8332
export RB_BTC_RPC_USER=rpcuser
export RB_BTC_RPC_PASS=...
export RB_BTC_DASHBOARD=http://127.0.0.1:8600/api/node   # optional
export RB_ELECTRS_HOST=127.0.0.1  # electrs for btc_get_address
export RB_ELECTRS_PORT=50001
export RB_ZEC_BASE=http://127.0.0.1:8080                  # status API with /api/chaininfo
export RB_ETH_EXEC_RPC=http://127.0.0.1:8545              # Reth execution RPC (chain_status: eth)
export RB_ETH_CONS_RPC=http://127.0.0.1:5052              # Lighthouse beacon API (chain_status: eth)
export RB_XMR_HELPER=http://127.0.0.1:18085/rpc           # monerod JSON-RPC (xmr_get_node_status)
export RB_HOME_BASE=http://127.0.0.1:8081                 # gateway: /api/nodes /api/pools /api/services
export RB_USAGE_DB=./usage.db                      # optional usage audit
export RB_BILLING_DB=./billing.db              # optional: API-key verification
python3 server.py
```

See [`examples/`](./examples) for client configs and [`examples/env.example`](./examples/env.example) for the full variable list. Endpoints exposed by the server itself: `/` (docs), `/tools` (tool list JSON), `/server.json` (server card), `/stats` (usage audit), `/healthz`.

## License

MIT — see [LICENSE](./LICENSE).

---

<sub>RobotBase · verification & risk intelligence for Web3 agents · <https://robotbase.cc/></sub>
