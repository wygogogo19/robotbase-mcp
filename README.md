# RobotBase MCP Server

**The semantic risk & verification layer for Web3 agents — one read-only MCP endpoint.**

**免鉴权 · 无追踪 · 只读** ｜ 自建裸金属全节点，对 Agent 暴露统一的「验证 + 风控」语义入口

Live today: **Bitcoin · Ethereum · Monero · Zcash**. Provisioning: **Solana · Base · Tron**.

[![Tools](https://img.shields.io/badge/tools-45-brightgreen)](https://robotbase.cc/mcp/tools)
[![Protocol](https://img.shields.io/badge/MCP-2025--06--18-blue)](https://modelcontextprotocol.io)
[![Transport](https://img.shields.io/badge/transport-streamable--http-orange)](https://robotbase.cc/mcp)
[![Glama](https://glama.ai/mcp/servers/wygogogo19/robotbase-mcp/badges/score.svg)](https://glama.ai/mcp/servers/wygogogo19/robotbase-mcp)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.wygogogo19%2Frobotbase--mcp-blue)](https://registry.modelcontextprotocol.io/v0/servers/io.github.wygogogo19%2Frobotbase-mcp/versions)

Hosted endpoint: **`https://robotbase.cc/mcp`** · Landing page: <https://robotbase.cc/mcp> · Server card: <https://robotbase.cc/mcp/server.json> · Live usage audit: <https://robotbase.cc/mcp/stats>

Listed in the **official MCP Registry** as `io.github.wygogogo19/robotbase-mcp` · indexed by **Glama** · published on **Smithery** (`wygogogo/robotbase-mcp`).

---

## Why

An autonomous agent handling crypto has exactly two blocking questions: **did this payment actually settle**, and **what will the next one cost**. Ask an LLM those today and it has nowhere reliable to look — the MCP ecosystem is rich in wrappers around hosted indexers and thin on self-run infrastructure.

This server answers both with **45 strictly read-only tools** (41 free verification tools + 4 metered risk tools) backed by our own full nodes and local indexes: `btc_get_transaction` for the settlement check, `btc_get_fee_rates` and `btc_get_mempool_state` for the price of the next one, `zec_get_pool_status` for Zcash shielded-pool supply, a full Monero surface (`xmr_get_fee_rates`, `xmr_get_transaction`, `xmr_verify_payment`, …) read straight off our own monerod, an Ethereum surface (`eth_get_transaction`, `eth_get_fee_rates`, `eth_read_contract`) on our own Reth node, and a Tron surface (`tron_read_contract`, `tron_get_transaction`) on our own java-tron node.

## Pricing

| Tier | What it covers | Limit |
|---|---|---|
| **Verification (13 tools)** | Fee, mempool, tx/block/address verification, node and shielded-pool telemetry, optional raw-tx relay | **Free forever**, no key, no signup |
| **Risk intelligence (4 tools)** | `tron_check_address_risk`, `tron_verify_usdt_transfer`, `sol_check_token_safety`, `base_check_contract_risk` | **5 calls/day per IP** free, resets 00:00 Asia/Shanghai |
| **Prepaid API key** | Lifts the risk-tool cap and raises the rate limit | USDT (TRC-20) or BTC on-chain at <https://robotbase.cc/billing/pricing> — the key is issued **automatically** the moment the payment confirms, and usage is readable at `/billing/v1/usage?key=…` |

Every metered answer carries its own `_metering` block (allowance, reset time, calls left), and a call that fails local validation — a malformed address or txid — does **not** consume the allowance. Past the free allowance the tool returns `Payment Required` and names the top-up page instead of a partial result.

The API key is the primary billing model because it is the only one every mainstream MCP client can actually use today. Machine-to-machine X402 settlement stays a lab track until mainstream clients can answer a `402 Payment Required` challenge.

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

## Tools (45)

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
| `btc_get_network_stats` | Difficulty, hashrate, total txs and current tx/s, client, peers, relay fee, uptime, synced indexes | "How much hashrate secures Bitcoin?" |
| `btc_get_address(address)` | Balance, UTXOs, tx count, recent txs (P2PKH/P2SH/bech32/bech32m) | "How much BTC is in this address?" |
| `zec_get_pool_status` | Zcash node state **plus all six value pools** (transparent/sprout/sapling/orchard/ironwood/lockbox) with 1h/24h deltas | "How much ZEC is shielded, and is it growing?" |
| `zec_get_recent_blocks(n)` | Recent Zcash blocks | "Are ZEC blocks healthy?" |
| `zec_get_network_stats` | Mempool, node version, difficulty, hashrate, chain progress, block reward, funding streams (ZCG) and lockbox | "How much hashrate secures Zcash, and where does the subsidy go?" |
| `zec_get_treestate(block_hash?\|height?)` | Sapling / Orchard / Ironwood shielded commitment-tree anchor roots at a block | "How is the shielded set advancing?" |
| `zec_get_transaction(txid)` | ZEC transaction: confirmations, block, active-chain, and how much is transparent vs shielded. Shielded amounts/parties are reported as **not disclosed by the protocol** | "Did this ZEC payment land?" |
| `eth_get_transaction(txid)` | ETH transaction: status, gas, fee, confirmations, finalized, and **ERC-20 transfers decoded from the receipt logs** with the token's own decimals resolved | "Did this ETH or USDT/USDC payment settle?" |
| `eth_get_fee_rates` | Base fee (now and next), priority-fee percentiles, and cost estimates for a plain and an ERC-20 transfer | "What will my Ethereum transaction cost?" |
| `eth_get_account(address)` | ETH balance, nonce, and whether the address is a contract | "Is this an EOA or a contract?" |
| `eth_get_block(height?\|block_hash?)` | Block by number, tag (latest/finalized/safe) or hash: txs, gas used vs limit, base fee, builder | "How full is Ethereum right now?" |
| `eth_get_logs(from?, to?, address?, topic0?)` | Event logs over a range with ERC-20 Transfers decoded; auto-narrows when a query exceeds the node's result cap | "Who moved this token in the last N blocks?" |
| `eth_read_contract(contract, function, argument?)` | Read-only `eth_call` on our own Reth node: `balanceOf`, `allowance`, `decimals`, `symbol`, `name`, `totalSupply`, or any raw selector | "What is this address's USDT balance?" |
| `xmr_get_node_status` | Monero node: true last-block height, sync flag, difficulty, txpool, database size, monerod version, peers | "Is the Monero node synced?" |
| `xmr_get_fee_rates` | Fee estimate per kB across ascending priority tiers, read from our own node | "What fee will my XMR transaction need?" |
| `xmr_get_mempool_state` | Pending tx count, total bytes and fees, age of the oldest pending tx, double-spend/failing counts | "Is Monero congested right now?" |
| `xmr_get_block(height\|block_hash)` | One block header: time, tx count, reward, difficulty, weight, protocol version | "What is in block 3,784,931?" |
| `xmr_get_recent_blocks(n)` | Recent blocks plus the observed block interval vs the 120s target | "Are XMR blocks healthy?" |
| `xmr_get_output_distribution(from?, to?)` | Spendable outputs per ring-size bucket — Monero's anonymity-set side. Raw counts, never a "privacy score". Data lags the tip by ~5,000 blocks and says so | "How large is the spendable output set?" |
| `xmr_get_transaction(txid)` | Is this txid mined, which block, confirmations, size, outputs, ring size. Amount/sender/recipient are reported as **not disclosed by the protocol** — Monero hides them by design and no third party can recover them | "Did my XMR transfer get mined?" |
| `xmr_get_network_stats` | Network hashrate, difficulty, emission and fees over the last 720 blocks, protocol version, next hard fork | "How much hashrate secures Monero?" |
| `xmr_verify_payment(txid, address, tx_key\|signature)` | **Cryptographic Monero payment verification**: give it a txid plus the transaction key (`get_tx_key`) or a tx proof (`get_tx_proof`) from your own wallet and it verifies the transaction really pays that address, for how much, with how many confirmations. Runs on RobotBase's own monero-wallet-rpc instance (separate service, separate credentials, empty wallet — no funds, no receiving keys) | "Did this XMR payment actually arrive?" |
| `broadcast_raw_transaction` | Relay an already-signed BTC transaction through our own node (testmempoolaccept first; operator-gated) | "Broadcast this signed tx for me" |
| `tron_get_node_status` | Tron node: tip height, finalized (solidity) height and lag, peers, client version, txs in the tip block | "Is the Tron node synced?" |
| `tron_get_current_block` | The block being produced: number, id, timestamp, tx count by contract type, producing witness | "How busy is Tron right now?" |
| `tron_get_block(height\|block_id)` | One block: producer, timestamp, tx count by type, tx ids | "What is in block 87,147,698?" |
| `tron_list_proposals(limit?)` | Governance proposals: id, proposer, state, approvals and the parameters they would change | "Is a chain-parameter change pending?" |
| `tron_get_transaction(txid)` | One transaction: success/reverted, energy used, confirmations, finalized-on-solidity, and **TRC-20 transfers decoded from the receipt logs** (token, from, to, amount) | "Did this USDT payment land, and for how much?" |
| `tron_get_account(address)` | TRX balance, activation state, energy and bandwidth, witness flag, account age | "How much TRX is in this address?" |
| `tron_get_contract(address)` | Is the ABI published, name, creator, function list, code hash, TRC-20 detection | "Is this contract verified?" |
| `tron_read_contract(contract, function, argument)` | **Read-only constant call run by our own node** — `balanceOf`, `allowance`, `decimals`, `symbol`, `name`, `totalSupply`, or any raw selector. No key, no state change | "What is this address's USDT balance?" |
| `tron_get_chain_economics` | Energy price (sun/unit), transaction fee, account creation fee, witness pay per block, energy/bandwidth limits, next maintenance | "What will a TRC-20 transfer cost in TRX?" |
| `tron_list_witnesses(limit)` | Super representatives: blocks produced, missed, votes, websites | "Who produces Tron blocks?" |

### Risk intelligence (metered)

Four tools answer what a free block explorer cannot. They are aggregation over public datasets refined in our own middle layer — deliberately **no** heavy local node for Solana, Tron or Base.

| Tool | What it does | Typical question |
|---|---|---|
| `tron_check_address_risk(address)` | TRC-20 counterparty screening: judicial / black-market / mixer / phishing labels, account activation and age, 0-100 safety score, plus an explicit `not_covered` list | "Is it safe to accept USDT from this address?" |
| `tron_verify_usdt_transfer(txid, expected_to?, expected_amount_usdt?)` | USDT settlement check: finalized on the solidity chain, confirmations, amount, sender, recipient, matched against what you expect. Reads **our own Tron node** inside its ~6 day window (sub-second, no third party) and falls back to TronGrid for older transactions — `source` says which answered, and a local miss is never reported as "not found" | "Did this USDT payment land, irreversibly, for the right amount?" |
| `sol_check_token_safety(mint)` | Solana rug screen: mint authority, freeze authority, liquidity-lock percentage, top-10 concentration, transfer fee / hook | "Is this token a rug before my agent buys it?" |
| `base_check_contract_risk(contract)` | Base (EVM) screen: honeypot / cannot-sell, buy & sell tax, verified source, upgradeable proxy, hidden owner, balance-rewrite, pausable transfers, mintable supply, selfdestruct, blacklist, LP lock, top-10 concentration | "Can this Base token be sold, and what does the exit cost?" |

Each one returns `risk_level`, a 0-100 `safety_score`, the verbatim upstream flags it was derived from, a `not_covered` list and a disclaimer — it relays public labels and **forms no legal opinion**. Issuer-backed tokens (regulated stablecoins) are read as issuer control rather than as a rug signal, and that decision is stated in the response.

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
export RB_BTC_DASHBOARD=http://127.0.0.1:8600            # optional: /api/node used by chain_status(btc)
export RB_ELECTRS_HOST=127.0.0.1  # electrs for btc_get_address
export RB_ELECTRS_PORT=50001
export RB_ZEC_BASE=http://127.0.0.1:8080                  # status API with /api/chaininfo
export RB_XMR_HELPER=http://127.0.0.1:18085/rpc           # monerod JSON-RPC (xmr_get_node_status)
export RB_HOME_BASE=http://127.0.0.1:8081                 # gateway aggregation: /api/nodes (eth + xmr telemetry) /api/services
export RB_USAGE_DB=./usage.db                             # optional usage audit
export RB_BILLING_DB=./billing.db                         # optional: API-key verification
export RB_RISK_DB=./risk_quota.db                         # optional: daily risk-tool allowance bookkeeping
python3 server.py
```

See [`examples/`](./examples) for client configs and [`examples/env.example`](./examples/env.example) for the full variable list. Endpoints exposed by the server itself: `/` (docs), `/tools` (tool list JSON), `/server.json` (server card), `/stats` (usage audit), `/healthz`.

## License

MIT — see [LICENSE](./LICENSE).

---

<sub>RobotBase · verification & risk intelligence for Web3 agents · <https://robotbase.cc/></sub>
