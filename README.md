# RobotBase MCP Server

**Read-only multi-chain data for AI agents — the first MCP server covering the six classic proof-of-work chains: Bitcoin · Kaspa · Zcash · Ravencoin · Dogecoin · Litecoin.**

**免鉴权 · 无追踪 · 只读** ｜ 面向 AI Agent 的六大经典 PoW 公链数据接口

[![Tools](https://img.shields.io/badge/tools-15-brightgreen)](https://robotbase.cc/mcp/tools)
[![Protocol](https://img.shields.io/badge/MCP-2025--06--18-blue)](https://modelcontextprotocol.io)
[![Transport](https://img.shields.io/badge/transport-streamable--http-orange)](https://robotbase.cc/mcp)
[![Glama](https://glama.ai/mcp/servers/wygogogo19/robotbase-mcp/badges/score.svg)](https://glama.ai/mcp/servers/wygogogo19/robotbase-mcp)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.wygogogo19%2Frobotbase--mcp-blue)](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.wygogogo19/robotbase-mcp)

Hosted endpoint: **`https://robotbase.cc/mcp`** · Landing page: <https://robotbase.cc/mcp> · Server card: <https://robotbase.cc/mcp/server.json> · Live usage audit: <https://robotbase.cc/mcp/stats>

Listed in the **official MCP Registry** as `io.github.wygogogo19/robotbase-mcp` · indexed by **Glama** · published on **Smithery** (`wygogogo/robotbase-mcp`).

---

## Why

Ask any LLM today to check **Bitcoin mempool fees**, whether **our Kaspa solo hashport ever found a real mainnet block**, or the **Zcash shielded-pool supply** and it has nowhere to look: the MCP ecosystem is rich for EVM/Solana and nearly empty for the classic proof-of-work chains. This server fills that gap with 15 strictly read-only tools backed by our own full nodes.

## Pioneer Beta — free API keys

The hosted endpoint is free to use anonymously (120 req/min). For the **first 100 developers** we hand out a dedicated Bearer key that raises the limit to **600 req/min** and unlocks all 15 tools, free during the public beta:

**→ [Request Free Beta Key](https://github.com/wygogogo19/robotbase-mcp/issues/new?title=[Beta+Key+Request]&body=Project+Name:+%0AContact+(GitHub/Telegram/Email):)** — open an issue with your project name and a way to reach you; we reply with an `rb_…` key.

No card, no payment, no custody. The beta is a public stress test — feedback is the currency.

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
       "params":{"name":"btc_fee_estimates","arguments":{}}}'
```

## Tools (15)

| Tool | What it does | Typical question |
|---|---|---|
| `list_chains` | Supported chains + live status | "Which chains do you support? Are they online?" |
| `chain_status(chain)` | Node height, sync progress, peers, mempool, version (`btc\|kas\|zec\|rvn\|doge\|ltc`) | "Is the Kaspa node synced?" |
| `robotbase_services` | Gateway-wide service health | "Give me an overall health check" |
| `btc_fee_estimates` | Recommended fees for 1/2/3/6/12/24 blocks + mempool min fee | "What fee should I pay right now?" |
| `btc_mempool_summary` | Mempool congestion: pending txs, size, min fee | "Is Bitcoin congested?" |
| `btc_tx_lookup(txid)` | BTC transaction: confirmations, block, I/O summary | "Is this BTC tx confirmed?" |
| `btc_block_summary(height\|blockhash)` | Block summary (defaults to tip) | "How many txs in the latest block?" |
| `btc_address_summary(address)` | Balance, UTXOs, tx count, recent txs (P2PKH/P2SH/bech32/bech32m) | "How much BTC is in this address?" |
| `zec_chain_info` | Zcash chain info **incl. shielded-pool supply** (transparent/sprout/sapling/orchard/lockbox/ironwood) | "How much ZEC is shielded?" |
| `zec_recent_blocks(n)` | Recent Zcash blocks | "Are ZEC blocks healthy?" |
| `kas_node_status` | Kaspa node: network height, DAA score, difficulty, network hashrate, DAG tips, block reward, next halving | "How high and how healthy is the Kaspa node?" |
| `kas_pool_status` | Our Kaspa solo hashport: tiers, hashrate, miners, shares, blocks found, last block hash + blue score, stale/invalid | "Did the KAS pool ever find a real mainnet block?" |
| `rvn_node_status` | Ravencoin node: height, sync, network hashrate, difficulty, client | "How far has the Ravencoin node synced?" |
| `rvn_pool_status` | Our Ravencoin solo hashport: state, miners, shares, stratum endpoint, fee, per-miner coinbase | "Is the RVN hashport open and where do I point a GPU rig?" |
| `utxo_chain_status(doge\|ltc)` | Dogecoin / Litecoin node status | "How far has DOGE synced?" |

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
            ├── KAS  : kaspad (Rust) REST + our BlockDAG stratum hashport
            ├── ZEC  : Zebra RPC (valuePools → shielded-pool supply)
            ├── RVN  : Ravencoin Core RPC + our KawPoW solo hashport
            ├── DOGE : Dogecoin Core RPC
            ├── LTC  : Litecoin Core RPC
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
export RB_BTC_RPC_USER=btcrpc
export RB_BTC_RPC_PASS=...
export RB_BTC_DASHBOARD=http://127.0.0.1:8600/api/node   # optional
export RB_ELECTRS_HOST=127.0.0.1  # electrs for btc_address_summary
export RB_ELECTRS_PORT=50001
export RB_ZEC_BASE=http://127.0.0.1:8080                  # status API with /api/chaininfo
export RB_DOGE_BASE=http://127.0.0.1:8080
export RB_LTC_BASE=http://127.0.0.1:8080
export RB_HOME_BASE=http://127.0.0.1:8081                 # gateway: /api/nodes /api/pools /api/node/kas
export RB_USAGE_DB=/opt/mcp/usage.db                      # optional usage audit
export RB_BILLING_DB=/opt/billing/billing.db              # optional: API-key verification
python3 server.py
```

See [`examples/`](./examples) for client configs and [`examples/env.example`](./examples/env.example) for the full variable list. Endpoints exposed by the server itself: `/` (docs), `/tools` (tool list JSON), `/server.json` (server card), `/stats` (usage audit), `/healthz`.

## License

MIT — see [LICENSE](./LICENSE).

---

<sub>RobotBase · read-only on-chain data infrastructure · <https://robotbase.cc/></sub>
