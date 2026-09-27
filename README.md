# RobotBase MCP Server

**Read-only multi-chain data for AI agents across four production chains: Bitcoin · Ethereum · Monero · Zcash.**

**免鉴权 · 无追踪 · 只读** ｜ 面向 AI Agent 的六大经典 PoW 公链数据接口

[![Tools](https://img.shields.io/badge/tools-21-brightgreen)](https://robotbase.cc/mcp/tools)
[![Protocol](https://img.shields.io/badge/MCP-2025--06--18-blue)](https://modelcontextprotocol.io)
[![Transport](https://img.shields.io/badge/transport-streamable--http-orange)](https://robotbase.cc/mcp)
[![Glama](https://glama.ai/mcp/servers/wygogogo19/robotbase-mcp/badges/score.svg)](https://glama.ai/mcp/servers/wygogogo19/robotbase-mcp)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.wygogogo19%2Frobotbase--mcp-blue)](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.wygogogo19/robotbase-mcp)

Hosted endpoint: **`https://robotbase.cc/mcp`** · Landing page: <https://robotbase.cc/mcp> · Server card: <https://robotbase.cc/mcp/server.json> · Live usage audit: <https://robotbase.cc/mcp/stats>

Listed in the **official MCP Registry** as `io.github.wygogogo19/robotbase-mcp` · indexed by **Glama** · published on **Smithery** (`wygogogo/robotbase-mcp`).

---

## Why

Ask any LLM today to check **Bitcoin mempool fees**, **whether our Ethereum node is in sync**, **how much hashrate is on our Monero p2pool sidechains**, or the **Zcash shielded-pool supply** and it has nowhere to look: the MCP ecosystem is rich for hosted-indexer SaaS and thin on self-run nodes. This server fills that gap with 21 strictly read-only tools backed by our own full nodes, our own pool engines and local indexes.

## Pioneer Beta — free API keys

The hosted endpoint is free to use anonymously (120 req/min). For the **first 100 developers** we hand out a dedicated Bearer key that raises the limit to **600 req/min** and unlocks all 21 tools, free during the public beta:

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

## Tools (21)

| Tool | What it does | Typical question |
|---|---|---|
| `list_chains` | Supported chains + live status | "Which chains do you support? Are they online?" |
| `chain_status(chain)` | Node height, sync progress, peers, mempool, version (`btc\|eth\|xmr\|zec`) | "Is the Monero node synced?" |
| `robotbase_services` | Gateway-wide service health | "Give me an overall health check" |
| `btc_fee_estimates` | Recommended fees for 1/2/3/6/12/24 blocks + mempool min fee | "What fee should I pay right now?" |
| `btc_mempool_summary` | Mempool congestion: pending txs, size, min fee | "Is Bitcoin congested?" |
| `btc_tx_lookup(txid)` | BTC transaction: confirmations, block, I/O summary | "Is this BTC tx confirmed?" |
| `btc_block_summary(height\|blockhash)` | Block summary (defaults to tip) | "How many txs in the latest block?" |
| `btc_address_summary(address)` | Balance, UTXOs, tx count, recent txs (P2PKH/P2SH/bech32/bech32m) | "How much BTC is in this address?" |
| `zec_chain_info` | Zcash chain info **incl. shielded-pool supply** (transparent/sprout/sapling/orchard/lockbox/ironwood) | "How much ZEC is shielded?" |
| `zec_recent_blocks(n)` | Recent Zcash blocks | "Are ZEC blocks healthy?" |
| `eth_node_status` | Ethereum node: execution (Reth) + consensus (Lighthouse) sync state, height/head slot, peers, and an explicit provisioning status while the node is being built | "Is our Ethereum node synced yet?" |
| `xmr_node_status` | Monero node: height vs target, sync flag, difficulty, txpool, database size, monerod version, peers | "Is the Monero node synced?" |
| `xmr_pool_status` | Our Monero p2pool mini + nano sidechains: pool hashrate, miners, sidechain height/difficulty, blocks found, last block age, our workers, fee, non-custodial flag — plus Monero network difficulty/hashrate/reward | "How much hashrate is on the Monero mini sidechain?" |
| `pow_halving_oracle` | Halving countdown for the PoW chains we run (BTC / ZEC): next height, blocks remaining, ETA, reward before/after | "When is the next Zcash halving?" |
| `pow_network_mining_intel` | Network hashrate + difficulty for BTC / XMR / ZEC (method stated per chain) plus a proof-of-stake note for ETH | "How much hashpower secures Zcash right now?" |
| `get_recommended_fee_rate` | Fee tiers (fast / medium / slow) + mempool minimum; BTC from our own estimator, ZEC returns the ZIP-317 conventional fee | "What fee gets my BTC confirmed next block?" |
| `mempool_congestion_status` | BTC mempool size, bytes, capacity load, min fee, total fees + a busy/normal verdict | "Is now a good time to settle on-chain?" |
| `zec_shielded_pools_metrics` | All six Zcash value pools with share of supply and 1h/24h deltas | "How much ZEC sits in Orchard vs Ironwood?" |
| `zec_block_attribution_intel` | Coinbase attribution over the last N Zcash blocks from our own node scan: shielded-pool share of block rewards, coinbase outputs split into consensus funding streams (lockbox) vs real miner payout addresses, and the pool tags miners printed into their own coinbase text (plus our `/RobotBase/` tag) | "Who is mining Zcash right now, and how much of the reward is shielded?" |
| `robotbase_pool_worker_query` | Look up one miner on our hashports by wallet or worker: hashrate, shares, stale/invalid, difficulty | "How is my rig doing on robotbase?" |
| `broadcast_raw_transaction` | Relay an already-signed transaction through our own node (testmempoolaccept first; operator-gated) | "Broadcast this signed tx for me" |

## Beyond a generic reader

Most MCP servers wrap a third-party API. These four capabilities come from infrastructure we operate, which is why they are hard to find elsewhere:

1. **Mining intelligence** — `pow_halving_oracle`, `pow_network_mining_intel` and the two pool tools read our own nodes and pool engines, not a public API.
2. **Fee & mempool oracles** — `mempool_congestion_status` + `get_recommended_fee_rate` answer the first question any autonomous agent has before it sends a transaction.
3. **Local indexes** — `zec_shielded_pools_metrics` (six value pools with deltas) and `zec_block_attribution_intel` (shielded-coinbase share + `/RobotBase/`-tagged blocks from our own node scan), plus `xmr_pool_status` reading our own p2pool sidechains rather than a public explorer.
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
            ├── XMR  : monerod JSON-RPC + p2pool (mini / nano sidechains)
            ├── ZEC  : Zebra RPC (valuePools → shielded-pool supply)
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
export RB_ETH_EXEC_RPC=http://127.0.0.1:8545              # Reth execution RPC (eth_node_status)
export RB_ETH_CONS_RPC=http://127.0.0.1:5052              # Lighthouse beacon API (eth_node_status)
export RB_XMR_HELPER=http://127.0.0.1:18085/rpc           # monerod JSON-RPC (xmr_node_status)
export RB_XMR_P2POOL_API=http://127.0.0.1:9327/pool.json   # p2pool pool.json (xmr_pool_status)
export RB_HOME_BASE=http://127.0.0.1:8081                 # gateway: /api/nodes /api/pools /api/services
export RB_USAGE_DB=/opt/mcp/usage.db                      # optional usage audit
export RB_BILLING_DB=/opt/billing/billing.db              # optional: API-key verification
python3 server.py
```

See [`examples/`](./examples) for client configs and [`examples/env.example`](./examples/env.example) for the full variable list. Endpoints exposed by the server itself: `/` (docs), `/tools` (tool list JSON), `/server.json` (server card), `/stats` (usage audit), `/healthz`.

## License

MIT — see [LICENSE](./LICENSE).

---

<sub>RobotBase · read-only on-chain data infrastructure · <https://robotbase.cc/></sub>
