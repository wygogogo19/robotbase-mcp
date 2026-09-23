# RobotBase MCP Server

**Read-only multi-chain data for AI agents — the first MCP server covering the six classic proof-of-work chains: Bitcoin · Kaspa · Zcash · Ravencoin · Dogecoin · Litecoin.**

**免鉴权 · 无追踪 · 只读** ｜ 面向 AI Agent 的六大经典 PoW 公链数据接口

[![Tools](https://img.shields.io/badge/tools-25-brightgreen)](https://robotbase.cc/mcp/tools)
[![Protocol](https://img.shields.io/badge/MCP-2025--06--18-blue)](https://modelcontextprotocol.io)
[![Transport](https://img.shields.io/badge/transport-streamable--http-orange)](https://robotbase.cc/mcp)
[![Glama](https://glama.ai/mcp/servers/wygogogo19/robotbase-mcp/badges/score.svg)](https://glama.ai/mcp/servers/wygogogo19/robotbase-mcp)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.wygogogo19%2Frobotbase--mcp-blue)](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.wygogogo19/robotbase-mcp)

Hosted endpoint: **`https://robotbase.cc/mcp`** · Landing page: <https://robotbase.cc/mcp> · Server card: <https://robotbase.cc/mcp/server.json> · Live usage audit: <https://robotbase.cc/mcp/stats>

Listed in the **official MCP Registry** as `io.github.wygogogo19/robotbase-mcp` · indexed by **Glama** · published on **Smithery** (`wygogogo/robotbase-mcp`).

---

## Why

Ask any LLM today to check **Bitcoin mempool fees**, whether **our Kaspa solo hashport ever found a real mainnet block**, or the **Zcash shielded-pool supply** and it has nowhere to look: the MCP ecosystem is rich for EVM/Solana and nearly empty for the classic proof-of-work chains. This server fills that gap with 25 strictly read-only tools backed by our own full nodes, our own pool engines and local indexes.

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

## Tools (25)

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
| `pow_halving_oracle` | Halving countdown for every chain we run: next height, blocks remaining, ETA, reward before/after | "When is the next Kaspa reduction?" |
| `pow_network_mining_intel` | Network hashrate + difficulty per chain, with the method stated (node-reported vs difficulty-derived) | "How much hashpower secures Litecoin right now?" |
| `get_recommended_fee_rate` | Fee tiers (fast / medium / slow) + mempool minimum; BTC from our own estimator, ZEC returns the ZIP-317 conventional fee | "What fee gets my BTC confirmed next block?" |
| `mempool_congestion_status` | BTC mempool size, bytes, capacity load, min fee, total fees + a busy/normal verdict; tx counts for LTC/DOGE | "Is now a good time to settle on-chain?" |
| `zec_shielded_pools_metrics` | All six Zcash value pools with share of supply and 1h/24h deltas | "How much ZEC sits in Orchard vs Ironwood?" |
| `kas_pool_attribution_intel` | Chain-wide Kaspa pool attribution computed from our own block index: blocks and share per pool over 1-720h | "Who is winning Kaspa blocks today?" |
| `zec_block_attribution_intel` | Coinbase attribution over the last N Zcash blocks from our own node scan: shielded-pool share of block rewards, coinbase outputs split into consensus funding streams (lockbox) vs real miner payout addresses, and the pool tags miners printed into their own coinbase text (plus our `/RobotBase/` tag) | "Who is mining Zcash right now, and how much of the reward is shielded?" |
| `rvn_asset_lookup` | Ravencoin native asset registry from our own ravend: name, amount, units, reissuable flag, IPFS flag | "Does this Ravencoin asset exist, and can it still be reissued?" |
| `robotbase_pool_worker_query` | Look up one miner on our hashports by wallet or worker: hashrate, shares, stale/invalid, difficulty | "How is my rig doing on robotbase?" |
| `broadcast_raw_transaction` | Relay an already-signed transaction through our own node (testmempoolaccept first; operator-gated) | "Broadcast this signed tx for me" |
| `utxo_chain_status(doge\|ltc)` | Dogecoin / Litecoin node status | "How far has DOGE synced?" |

## Beyond a generic reader

Most MCP servers wrap a third-party API. These four capabilities come from infrastructure we operate, which is why they are hard to find elsewhere:

1. **Mining intelligence** — `pow_halving_oracle`, `pow_network_mining_intel` and the two pool tools read our own nodes and pool engines, not a public API.
2. **Fee & mempool oracles** — `mempool_congestion_status` + `get_recommended_fee_rate` answer the first question any autonomous agent has before it sends a transaction.
3. **Local indexes & native registries** — `zec_shielded_pools_metrics` (six value pools with deltas), `zec_block_attribution_intel` (shielded-coinbase share + `/RobotBase/`-tagged blocks from our own node scan), `kas_pool_attribution_intel` (hundreds of thousands of Kaspa blocks attributed to the pool that found them) and `rvn_asset_lookup` (Ravencoin's native asset registry straight from our own ravend).
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
export RB_RVN_ASSET_BASE=http://127.0.0.1:18081           # raven asset registry (rvn_asset_lookup)
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
