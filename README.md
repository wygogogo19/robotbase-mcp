# RobotBase MCP Server

**Read-only multi-chain data for AI agents — the first MCP server covering all five classic non-EVM chains: Bitcoin · Monero · Zcash · Dogecoin · Litecoin.**

**免鉴权 · 无追踪 · 只读** ｜ 面向 AI Agent 的五大经典公链数据接口

[![Tools](https://img.shields.io/badge/tools-16-brightgreen)](https://robotbase.cc/mcp/tools)
[![Protocol](https://img.shields.io/badge/MCP-2025--06--18-blue)](https://modelcontextprotocol.io)
[![Transport](https://img.shields.io/badge/transport-streamable--http-orange)](https://robotbase.cc/mcp)
[![Glama](https://glama.ai/mcp/servers/wygogogo19/robotbase-mcp/badges/score.svg)](https://glama.ai/mcp/servers/wygogogo19/robotbase-mcp)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.wygogogo19%2Frobotbase--mcp-blue)](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.wygogogo19/robotbase-mcp)

Hosted endpoint: **`https://robotbase.cc/mcp`** · Landing page: <https://robotbase.cc/mcp> · Server card: <https://robotbase.cc/mcp/server.json> · Live usage audit: <https://robotbase.cc/mcp/stats>

Listed in the **official MCP Registry** as `io.github.wygogogo19/robotbase-mcp` · indexed by **Glama** · published on **Smithery** (`wygogogo/robotbase-mcp`).

---

## Why

Ask any LLM today to check **Bitcoin mempool fees**, a **Monero transaction**, or the **Zcash shielded-pool supply** and it has nowhere to look: the MCP ecosystem is rich for EVM/Solana and nearly empty for the classic non-EVM chains. This server fills that gap with 16 strictly read-only tools backed by full nodes.

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

## Tools (16)

| Tool | What it does | Typical question |
|---|---|---|
| `list_chains` | Supported chains + live status | "Which chains do you support? Are they online?" |
| `chain_status(chain)` | Node height, sync progress, peers, mempool, version | "Is the Monero node synced?" |
| `robotbase_services` | Gateway-wide service health | "Give me an overall health check" |
| `btc_fee_estimates` | Recommended fees for 1/2/3/6/12/24 blocks + mempool min fee | "What fee should I pay right now?" |
| `btc_mempool_summary` | Mempool congestion: pending txs, size, min fee | "Is Bitcoin congested?" |
| `btc_tx_lookup(txid)` | BTC transaction: confirmations, block, I/O summary | "Is this BTC tx confirmed?" |
| `btc_block_summary(height\|blockhash)` | Block summary (defaults to tip) | "How many txs in the latest block?" |
| `btc_address_summary(address)` | Balance, UTXOs, tx count, recent txs (P2PKH/P2SH/bech32/bech32m) | "How much BTC is in this address?" |
| `xmr_node_info` | Monero node status | "Is the XMR node healthy?" |
| `xmr_fee_estimate` | Monero fee per byte + tiers | "What is the XMR fee today?" |
| `xmr_last_block` | Latest Monero block header | "When was the last XMR block?" |
| `xmr_tx_lookup(txid)` | Monero tx: in mempool? block height, confirmations | "Did this XMR swap land on-chain?" |
| `xmr_mempool_stats` | Monero mempool bytes, fees, histogram | "Is the XMR mempool busy?" |
| `zec_chain_info` | Zcash chain info **incl. shielded-pool supply** (transparent/sprout/sapling/orchard/lockbox/ironwood) | "How much ZEC is shielded?" |
| `zec_recent_blocks(n)` | Recent Zcash blocks | "Are ZEC blocks healthy?" |
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
            ├── XMR  : monerod restricted RPC + whitelisted full-RPC helper
            ├── ZEC  : Zebra RPC (valuePools → shielded-pool supply)
            ├── DOGE : Dogecoin Core RPC
            ├── LTC  : Litecoin Core RPC
            └── meta : gateway service aggregation
```

## Privacy & limits

- **No auth** for anonymous use; optional `X-API-Key` header (or `?key=`) raises the limit.
- Anonymous **120 req/min per IP**; with API key **600 req/min per IP** (`X-MCP-Plan` / `X-RateLimit-Limit` response headers).
- Audit stores only a **truncated SHA-256 of the client IP** — never query parameters.
- **Read-only**: no trading, signing, custody, or write methods. BTC transactions are looked up with `txindex`; XMR uses a whitelisted method list.
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
export RB_XMR_RPC=http://127.0.0.1:18089/json_rpc
export RB_XMR_HELPER=http://127.0.0.1:18085/rpc           # whitelisted full RPC
export RB_ZEC_BASE=http://127.0.0.1:8080                  # status API with /api/chaininfo
export RB_DOGE_BASE=http://127.0.0.1:8080
export RB_LTC_BASE=http://127.0.0.1:8080
python3 server.py
```

See [`examples/`](./examples) for client configs and [`examples/env.example`](./examples/env.example) for the full variable list. Endpoints exposed by the server itself: `/` (docs), `/tools` (tool list JSON), `/server.json` (server card), `/stats` (usage audit), `/healthz`.

## License

MIT — see [LICENSE](./LICENSE).

---

<sub>RobotBase · read-only on-chain data infrastructure · <https://robotbase.cc/></sub>
