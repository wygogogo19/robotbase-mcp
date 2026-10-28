# RobotBase MCP Server

**One MCP endpoint for agents: search the web, read any page, verify on-chain payments, and pay per call — no API keys.**

**免鉴权 · 无追踪 · 只读** ｜ 自建裸金属全节点，对 Agent 暴露统一的「验证 + 风控」语义入口

Hosted surface: **three MCP editions**, all including the full **17-tool web toolbox**:
**Web3** (`https://web3.robotbase.cc/mcp`, **65 tools** — BTC/ETH/XMR/ZEC/TRON verification & risk, prediction markets, crypto trends),
**Dev** (`https://dev.robotbase.cc/mcp`, **18 tools** — GitHub/HF/npm/PyPI/HN trends + web), and
**Content** (`https://content.robotbase.cc/mcp`, **35 tools** — social hot lists, attention trends, 16 CyberLibrary book tools + web).
`https://robotbase.cc/mcp` is the Web3 compatibility alias. This repository is the dependency-free reference implementation: **45 tools** (41 free verification + 2 risk + 2 web).

| Edition | Endpoint | Tools | Audience |
|---|---|---:|---|
| **Web3** | `https://web3.robotbase.cc/mcp` | 65 | chain verification, risk, prediction markets, crypto trends |
| **Dev** | `https://dev.robotbase.cc/mcp` | 18 | GitHub/HF/npm/PyPI/HN trends + web docs tooling |
| **Content** | `https://content.robotbase.cc/mcp` | 35 | social hot lists, attention trends, books + web |

> `https://robotbase.cc/mcp` is a compatibility alias of Web3 for existing clients — it is not a separate MCP server and should not be listed as a separate connector.

All four include the **17-tool web toolbox**. Free web allowance is **100 calls/day/IP per edition**; the x402 credit balance is shared.


- **Install guide for agents:** [`llms-install.md`](./llms-install.md) · **Privacy:** <https://robotbase.cc/privacy> · **Security:** [`SECURITY.md`](./SECURITY.md)

> **Base and Solana data tools were retired on 2026-10-18.** The five-chain surface is Bitcoin, Ethereum, Monero, Zcash and Tron. x402 payments still settle in USDC on Base or Solana; that is a payment rail, not a data service.

[![Hosted tools](https://img.shields.io/badge/Web3%20tools-65-brightgreen)](https://web3.robotbase.cc/mcp/tools) [![Reference tools](https://img.shields.io/badge/reference%20tools-45-blue)](https://github.com/wygogogo19/robotbase-mcp)
[![Protocol](https://img.shields.io/badge/MCP-2025--06--18-blue)](https://modelcontextprotocol.io)
[![Transport](https://img.shields.io/badge/transport-streamable--http-orange)](https://robotbase.cc/mcp)
[![Glama](https://glama.ai/mcp/servers/wygogogo19/robotbase-mcp/badges/score.svg)](https://glama.ai/mcp/servers/wygogogo19/robotbase-mcp)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.wygogogo19%2Frobotbase--mcp-blue)](https://registry.modelcontextprotocol.io/v0/servers/io.github.wygogogo19%2Frobotbase-mcp/versions)

Hosted endpoint: **`https://robotbase.cc/mcp`** · Landing page: <https://robotbase.cc/mcp> · Server card: <https://robotbase.cc/mcp/server.json> · Live usage audit: <https://robotbase.cc/mcp/stats>

Listed in the **official MCP Registry** as `io.github.wygogogo19/robotbase-mcp` · indexed by **Glama** · published on **Smithery** (`wygogogo/robotbase-mcp`).

---

## Why

An autonomous agent handling crypto has exactly two blocking questions: **did this payment actually settle**, and **what will the next one cost**. Ask an LLM those today and it has nowhere reliable to look — the MCP ecosystem is rich in wrappers around hosted indexers and thin on self-run infrastructure.

The Web3 edition answers with **65 strictly read-only tools** (44 free verification tools + 2 metered risk tools + 2 market/trend tools + 17 web tools: 14 metered plus 3 free helpers — `web_crawl_job`, `web_monitor_list` and `web_monitor_delete`); this reference implementation ships **45** of them (41 free + 2 risk + 2 web). Everything is backed by our own full nodes, local indexes and a metered web layer: `btc_get_transaction` for the settlement check, `btc_get_fee_rates` and `btc_get_mempool_state` for the price of the next one, `zec_get_pool_status` for Zcash shielded-pool supply, a full Monero surface (`xmr_get_fee_rates`, `xmr_get_transaction`, `xmr_verify_payment`, …) read straight off our own monerod, an Ethereum surface (`eth_get_transaction`, `eth_get_fee_rates`, `eth_read_contract`) on our own Reth node, and a Tron surface (`tron_read_contract`, `tron_get_transaction`) on our own java-tron node.

## Pricing

| Tier | What it covers | Limit |
|---|---|---|
| **Verification (41 tools)** | Fee, mempool, tx/block/address verification, node and shielded-pool telemetry, optional raw-tx relay | **Free forever**, no key, no signup |
| **Risk intelligence (2 tools)** | `tron_check_address_risk`, `tron_verify_usdt_transfer` | **5 calls/day per IP** free, resets 00:00 Asia/Shanghai |
| **Hosted web tools (17 tools)** | `web_search`, `web_fetch`, `web_crawl`, `web_map`, `web_extract`, `web_act`, `web_screenshot`, `web_feed`, `web_metadata`, `web_fetch_batch`, `web_diff`, `web_monitor_*` and helpers — one shared web bucket | **100 calls/day per IP, per edition**, resets 00:00 Asia/Shanghai |
| **x402 credit packs** | Past the free allowance, each metered call costs **$0.01** (1 credit: risk 1, `web_search` 1, `web_fetch` 1) | **10 calls / 0.10 USDC** or **100 calls / 1.00 USDC** in USDC on Base or Solana, paid from the caller's own wallet — payment rail only, no Base/Solana chain data |

**Base and Solana are a payment rail only.** x402 settles in USDC on Base or Solana, but RobotBase does **not** serve Base or Solana chain data: those eight data tools were retired on 2026-10-18, and the roster is Bitcoin, Ethereum, Monero, Zcash and Tron.

Every metered answer carries its own `_metering` block (allowance, reset time, calls left), and a call that fails local validation — a malformed address or txid — does **not** consume the allowance. Past the free allowance the tool returns `Payment Required` and names the top-up page instead of a partial result.

**There is no API key, no account and no subscription — from 2026-10-16 the paid tier is x402 machine payment only.** When the free allowance runs out the tool answers `402 Payment Required` with the full machine-readable terms in `structuredContent.accepts[]`. An agent settles one credit pack (EIP-3009 on Base, a signed transfer on Solana), then re-sends **that same payment payload** in `params._meta["x402/payment"]` on every later call: settlement is idempotent per payment nonce, so the payload is never charged twice — each call simply spends one credit until the pack is empty. Buy another pack to continue.

Replay works the same on both networks. For Solana the payload carries only the signed transaction, so the server recovers the payer from the settled-payment ledger by the transaction hash — it never re-simulates a transaction that already landed.

## The x402 payment layer (open source)

[`x402_pay.py`](./x402_pay.py) is the complete paid-tier implementation and it ships in this repo. It is **standard library only** — it talks HTTP to your facilitator and keeps a sqlite ledger. It never touches a private key and never submits a transaction.

What it does:

| Piece | Behaviour |
|---|---|
| 402 challenge | `structuredContent.accepts[]` lists every network/asset/pack combination, with `extra.feePayer` for Solana and the EIP-712 domain for EIP-3009 |
| Settlement | `POST /verify` then `POST /settle` on your facilitator, once per payment nonce |
| Credit | a settled pack credits `calls` against the payer address; the pack is spent one call at a time |
| Replay | the client re-sends the **same** payload; the ledger short-circuits it, so nothing is simulated or settled twice (EVM and Solana alike) |
| Retry safety | the same payer + tool + arguments within 5 minutes returns the cached answer with `charged_this_call=false` |
| Balance | `GET /credits?payer=<address>` returns credits left, calls charged, last payment and the live pack list |
| Exhaustion | the tool answers `PaymentRequired` / `insufficient_credit` instead of a partial result |
| Failure safety | a tool that fails local validation returns `_no_charge`, so a bad argument never burns a credit |
| Concurrency | `spend()` is a single atomic `UPDATE ... WHERE balance>0`; the balance cannot go negative |
| Reconciliation | if a settlement's response is lost, EIP-3009 `authorizationState` is checked on-chain before crediting again |

Enable it entirely through environment variables (see [`examples/x402.env.example`](./examples/x402.env.example)):

```bash
export RB_X402_ENABLED=1
export RB_X402_FACILITATOR_URL=https://facilitator.example
export RB_X402_OPTIONS='[{"network":"eip155:8453","asset":"0x…","pay_to":"0x…","decimals":6, …}]'
export RB_X402_PACKS='[{"amount_atomic":"100000","calls":10,"label":"10 calls"}]'
export RB_X402_LEDGER_DB=./x402_credit.db
python3 server.py
```

With those unset, the server runs the free tier only. This repo ships the **45 chain/web tools** as a reference implementation — the only differences from the hosted endpoint are where the per-chain backends point (your `RB_*` variables instead of ours) and the hosted-only CyberLibrary bridge (16 book tools) and fifteen additional web tools (screenshot, map, batch, feed, metadata, crawl, extract, act, extract_schema, diff, crawl_async, crawl_job, monitor_set, monitor_list, monitor_delete), which are not included here.

Check a payer's balance without spending a call:

```
GET https://robotbase.cc/mcp/credits?payer=<the address that paid>
→ { "found": true, "credits_remaining": 19, "calls_charged": 1,
    "last_payment": { … }, "packs": [ … ] }
```

EVM addresses are case-insensitive; there is no authentication because the balance is derived from public on-chain payments.

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

## Tools (45 — reference implementation)

> Public order is **web-first, then high-frequency verification** (the order returned by `tools/list` and `server.json`); the hosted endpoint puts its 16 CyberLibrary tools last.

> The hosted endpoint `https://robotbase.cc/mcp` has **79 tools**: these 45 plus 3 free XMR wallet/merchant tools, the 16 **CyberLibrary** book tools (`cyberlibrary_catalog_index`, `cyberlibrary_language_shard`, `cyberlibrary_book_meta`, `cyberlibrary_get_toc`, `cyberlibrary_get_chapter`, `cyberlibrary_get_markdown`, `cyberlibrary_cite`, `cyberlibrary_search_books`, `cyberlibrary_fulltext_search`, `cyberlibrary_service_info`, `cyberlibrary_get_chunks`, `cyberlibrary_verify_quote`, `cyberlibrary_search_passages`, `cyberlibrary_rag_context`, `cyberlibrary_semantic_search`, `cyberlibrary_search`) twelve additional web tools (`web_screenshot`, `web_map`, `web_fetch_batch`, `web_feed`, `web_metadata`, `web_crawl`, `web_extract`, `web_act`, `web_extract_schema`, `web_diff`, `web_crawl_async`, `web_crawl_job`) and 3 scheduled-monitor tools (`web_monitor_set`, `web_monitor_list`, `web_monitor_delete`).

### Web tools (high-frequency)

The hosted endpoint exposes **seventeen** web tools: `web_search`, `web_fetch`, `web_screenshot`,
`web_map`, `web_fetch_batch`, `web_feed`, `web_metadata`, `web_crawl`, `web_extract`, `web_act`,
`web_extract_schema`, `web_diff`, `web_crawl_async`, `web_crawl_job`, `web_monitor_set`,
`web_monitor_list` and `web_monitor_delete`. This reference implementation ships the first two; the
other fifteen are hosted-only. They are listed first in the public `tools/list` order.

Two general-purpose web tools sit next to the on-chain surface. They are the entry point an agent
uses before it verifies: search the live web, read the one page that looks authoritative, then
check the claim on chain. No API key is needed to call them.

| Tool | What it does | Typical question |
|---|---|---|
| `web_search(query, count?, freshness?, site?, lang?)` | Ranked web results with title, URL, snippet and published date. **Serper is the primary provider**, with Tavily, Brave and a zero-key `cn.bing` RSS feed as fallbacks; the answer carries a `provider` field. Results are deduplicated (one domain cannot take more than ~40% of a result set) and cached for up to 10 minutes | "What is the BTC fee market saying today?" "Recent Zcash governance proposals?" |
| `web_fetch(url, max_chars?, format?)` | Readable text/markdown of **one** public URL: `robots.txt` is checked first and `scripts`/`nav`/`header`/`footer` are stripped by a standard-library extractor. Direct fetch is tried first; pages blocked from this node are fetched through the provider fallback and marked `source=provider`. Response capped at 12,000 characters per call | "Read this article and summarise it" "Turn this URL into clean markdown" |

**Shared free allowance:** the two web tools share **100 calls/day per IP**, in a bucket separate from the 5/day risk allowance — searching can never consume the risk tools' free calls. A cached search still counts as one call against the free allowance; a paid
call answered from cache does not spend a credit. Past the allowance each call is 1 x402 credit
($0.01), charged through the same USDC credit balance as the risk tools.

**Boundaries:** `web_fetch` reads one public URL, never a batch or a whole site; it does not log
in, break paywalls, follow `noindex` tricks or bypass `robots.txt`. All fetching is SSRF-guarded:
only `http`/`https`, only ports 80/443, private / loopback / link-local / reserved / CGNAT
addresses are rejected, every redirect hop is re-validated, the resolved IP is pinned to the
connection, and the response body is capped at 2 MB. Audit stores no query string and no URL —
only the tool name, latency, result and a truncated IP hash.

**Provider configuration (optional).** The reference implementation is standard library only.
Set `RB_SERPER_KEY` for the primary search provider, and optionally `RB_TAVILY_KEY` /
`RB_BRAVE_KEY` for the fallback chain (`cn.bing` RSS needs no key). Set
`RB_TAVILY_DAILY_CREDITS` (default 15) to cap daily Tavily credits, and `RB_SERPER_DAILY_CALLS`
(default 200) to cap Serper calls. With no provider key at all, `web_search` degrades to the
zero-key `cn.bing` RSS feed and `web_fetch` still works for directly reachable pages.

### Chain verification tools

| Tool | What it does | Typical question |
|---|---|---|
| `list_chains` | Every chain on the roster with its state (`live\|provisioning\|planned`) and availability | "Which chains do you support? Are they online?" |
| `chain_status(chain)` | Node height, sync progress, peers, mempool, version (`btc\|eth\|xmr\|zec\|tron`) | "Is the Monero node synced?" |
| `robotbase_capabilities` | What each node can and cannot answer, incl. pruning/retention limits | "Can you still read that far back?" |
| `robotbase_services` | Gateway-wide service health | "Give me an overall health check" |
| `btc_get_fee_rates` | Fees for 1/2/3/6/12/24-block targets + mempool floor, in sat/vB | "What fee should I pay right now?" |
| `btc_get_mempool_state` | Mempool depth, capacity load, min fee, total fees + busy/normal verdict | "Is Bitcoin congested right now?" |
| `btc_get_transaction(txid)` | BTC transaction: confirmations, block, I/O summary | "Did this payment actually settle?" |
| `btc_get_block(height\|blockhash)` | Block summary (defaults to tip) | "How many txs in the latest block?" |
| `btc_get_network_stats` | Difficulty, hashrate, total txs and current tx/s, client, peers, relay fee, uptime, synced indexes | "How much hashrate secures Bitcoin?" |
| `btc_get_address(address)` | Balance, UTXOs, tx count, recent txs (P2PKH/P2SH/bech32/bech32m) | "How much BTC is in this address?" |
| `broadcast_raw_transaction` | Relay an already-signed BTC transaction through our own node (testmempoolaccept first; operator-gated) | "Broadcast this signed tx for me" |
| `eth_get_transaction(txid)` | ETH transaction: status, gas, fee, confirmations, finalized, and **ERC-20 transfers decoded from the receipt logs** with the token's own decimals resolved | "Did this ETH or USDT/USDC payment settle?" |
| `eth_get_fee_rates` | Base fee (now and next), priority-fee percentiles, and cost estimates for a plain and an ERC-20 transfer | "What will my Ethereum transaction cost?" |
| `eth_get_account(address)` | ETH balance, nonce, and whether the address is a contract | "Is this an EOA or a contract?" |
| `eth_get_block(height?\|block_hash?)` | Block by number, tag (latest/finalized/safe) or hash: txs, gas used vs limit, base fee, builder | "How full is Ethereum right now?" |
| `eth_get_logs(from?, to?, address?, topic0?)` | Event logs over a range with ERC-20 Transfers decoded; auto-narrows when a query exceeds the node's result cap | "Who moved this token in the last N blocks?" |
| `eth_read_contract(contract, function, argument?)` | Read-only `eth_call` on our own Reth node: `balanceOf`, `allowance`, `decimals`, `symbol`, `name`, `totalSupply`, or any raw selector | "What is this address's USDT balance?" |
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
| `xmr_get_node_status` | Monero node: true last-block height, sync flag, difficulty, txpool, database size, monerod version, peers | "Is the Monero node synced?" |
| `xmr_get_fee_rates` | Fee estimate per kB across ascending priority tiers, read from our own node | "What fee will my XMR transaction need?" |
| `xmr_get_mempool_state` | Pending tx count, total bytes and fees, age of the oldest pending tx, double-spend/failing counts | "Is Monero congested right now?" |
| `xmr_get_block(height\|block_hash)` | One block header: time, tx count, reward, difficulty, weight, protocol version | "What is in block 3,784,931?" |
| `xmr_get_recent_blocks(n)` | Recent blocks plus the observed block interval vs the 120s target | "Are XMR blocks healthy?" |
| `xmr_get_output_distribution(from?, to?)` | Spendable outputs per ring-size bucket — Monero's anonymity-set side. Raw counts, never a "privacy score". Data lags the tip by ~5,000 blocks and says so | "How large is the spendable output set?" |
| `xmr_get_transaction(txid)` | Is this txid mined, which block, confirmations, size, outputs, ring size. Amount/sender/recipient are reported as **not disclosed by the protocol** — Monero hides them by design and no third party can recover them | "Did my XMR transfer get mined?" |
| `xmr_get_network_stats` | Network hashrate, difficulty, emission and fees over the last 720 blocks, protocol version, next hard fork | "How much hashrate secures Monero?" |
| `xmr_verify_payment(txid, address, tx_key\|signature)` | **Cryptographic Monero payment verification**: give it a txid plus the transaction key (`get_tx_key`) or a tx proof (`get_tx_proof`) from your own wallet and it verifies the transaction really pays that address, for how much, with how many confirmations. Runs on RobotBase's own monero-wallet-rpc instance (separate service, separate credentials, empty wallet — no funds, no receiving keys) | "Did this XMR payment actually arrive?" |
| `zec_get_pool_status` | Zcash node state **plus all six value pools** (transparent/sprout/sapling/orchard/ironwood/lockbox) with 1h/24h deltas | "How much ZEC is shielded, and is it growing?" |
| `zec_get_recent_blocks(n)` | Recent Zcash blocks | "Are ZEC blocks healthy?" |
| `zec_get_network_stats` | Mempool, node version, difficulty, hashrate, chain progress, block reward, funding streams (ZCG) and lockbox | "How much hashrate secures Zcash, and where does the subsidy go?" |
| `zec_get_treestate(block_hash?\|height?)` | Sapling / Orchard / Ironwood shielded commitment-tree anchor roots at a block | "How is the shielded set advancing?" |
| `zec_get_transaction(txid)` | ZEC transaction: confirmations, block, active-chain, and how much is transparent vs shielded. Shielded amounts/parties are reported as **not disclosed by the protocol** | "Did this ZEC payment land?" |

### Risk intelligence (metered)

Two tools answer what a free block explorer cannot. They are aggregation over public datasets refined in our own middle layer; the TRON settlement check reads our own node inside its retention window and falls back to TronGrid for older transactions.

| Tool | What it does | Typical question |
|---|---|---|
| `tron_check_address_risk(address)` | TRC-20 counterparty screening: judicial / black-market / mixer / phishing labels, account activation and age, 0-100 safety score, plus an explicit `not_covered` list | "Is it safe to accept USDT from this address?" |
| `tron_verify_usdt_transfer(txid, expected_to?, expected_amount_usdt?)` | USDT settlement check: finalized on the solidity chain, confirmations, amount, sender, recipient, matched against what you expect. Reads **our own Tron node** inside its ~6 day window (sub-second, no third party) and falls back to TronGrid for older transactions — `source` says which answered, and a local miss is never reported as "not found" | "Did this USDT payment land, irreversibly, for the right amount?" |

**Base and Solana are a payment rail, not a data service.** x402 settles in USDC on Base or Solana; the Base/Solana on-chain data tools were retired on 2026-10-18 and no external RPC is queried for chain data.

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
            ├── TRON : java-tron lite full node (tip state + a ~6 day window)
            ├── meta : gateway service aggregation
            └── web  : Serper / Tavily / Brave / cn.bing RSS search (optional keys),
                       direct fetch + standard-library extraction, Tavily /extract fallback
```

## Privacy & limits

- **No auth at all** — robotbase.cc issues no API key, and sending `X-API-Key` changes nothing.
- **120 req/min per IP**, one tier (`X-MCP-Plan` / `X-RateLimit-Limit` response headers). Paid access is x402 credit and does not raise the per-IP rate limit.
- Audit stores only a **truncated SHA-256 of the client IP** — never query parameters.
- **Read-only**: no trading, signing, custody, or write methods. BTC transactions are looked up with `txindex`.
- Responses are trimmed to ~3.8 KB per call to protect LLM context; `web_fetch` is the
  exception (12,000 characters) because its whole point is to return a page's text.
- **Web tools** have an extra 20 req/min/IP limit on top of the 120/min/IP overall limit, and
  their free allowance (100 calls/day/IP shared by the 14 metered web tools) is tracked separately.

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
export RB_BILLING_DB=./billing.db                         # deprecated: API-key verification (no keys are issued since 2026-10-16)
export RB_RISK_DB=./risk_quota.db                         # optional: daily risk-tool allowance bookkeeping
export RB_WEB_CACHE_DB=./web_cache.db                     # optional: web result cache + web quota/spend tables
# web tools: no key is required. Optional provider keys buy quality/reliability:
export RB_SERPER_KEY=...                                  # primary search provider (best results)
export RB_TAVILY_KEY=...                                  # fallback search + fetch-extract fallback
export RB_BRAVE_KEY=...                                   # optional extra search fallback
export RB_SERPER_DAILY_CALLS=200                          # optional: daily Serper call cap
export RB_TAVILY_DAILY_CREDITS=15                         # optional: daily Tavily credit cap
export RB_WEB_FREE=100                                    # optional: per-IP free web allowance (shared by web tools)
export RB_WEB_GLOBAL=2000                                 # optional: site-wide free web cap
export RB_WEB_DAILY_BUDGET_USD=5                          # optional: daily upstream spend breaker
export RB_WEB_TRUNCATE=12000 RB_WEB_CACHE_MAX_MB=100      # optional: web_fetch cap / cache cap
export RB_WEB_UA='RobotBaseMCP/0.25.2 (+https://robotbase.cc/mcp)'  # optional: crawler User-Agent
# optional paid tier — see examples/x402.env.example for the full list
export RB_X402_ENABLED=1
export RB_X402_FACILITATOR_URL=https://facilitator.example
export RB_X402_OPTIONS='[ … ]'
export RB_X402_PACKS='[ … ]'
export RB_X402_LEDGER_DB=./x402_credit.db
python3 server.py
```

See [`examples/`](./examples) for client configs, [`examples/env.example`](./examples/env.example) for the node variables and [`examples/x402.env.example`](./examples/x402.env.example) for the payment variables. Endpoints exposed by the server itself: `/` (docs), `/tools` (tool list JSON), `/server.json` (server card), `/stats` (usage audit), `/healthz`.

## Reference implementation history

- **2026-10-20 — v0.27.0**: the 10 hosted web tools now share **100 calls/day per IP** (previously `web_search` 20 + other 9 shared 50); reference implementation's 2 web tools use the same shared-100 policy. Paid points, risk/library 5/day, and all other terms unchanged.
- **2026-10-23 — v0.32.0**: split the hosted surface into **three MCP editions**, all including the 17 web tools: **Web3** 65 (ZEC kept; prediction/crypto trends), **Dev** 18 (dev_trending + web), **Content** 35 (CyberLibrary + social/attention + web). Official Registry now has separate entries `robotbase-mcp` (Web3 alias), `robotbase-dev` and `robotbase-content`. Per-edition free web bucket: 100/day/IP each; shared x402 credit balance.
- **2026-10-23 — v0.31.1**: `web_monitor_list` gains a single-monitor mode — pass `monitor_id` to get that monitor plus `last_event.diff` (the most recent detected change, up to 4000 chars), so agents without a webhook can read what changed; `include_diff=false` omits it. Validated end-to-end against the hosted endpoint with a real public HTTPS webhook receiver (delivery `status=200`) and an isolated instance (`RB_MONITOR_MIN_INTERVAL_S=5`). Tool count unchanged: 79 hosted / 45 reference.
- **2026-10-23 — v0.31.0**: hosted surface reaches **79 tools** (44 free verification + 2 risk + 16 CyberLibrary + **17 web**: 14 metered + 3 free helpers — `web_crawl_job`, `web_monitor_list`, `web_monitor_delete`), adding `web_monitor_set`, `web_monitor_list` and `web_monitor_delete` for scheduled page-change monitoring with optional HMAC-SHA256 webhooks; reference implementation remains 45 tools. Tool count 79, metered 32. Registry/Glama/Smithery metadata synced to the hosted surface.
- **2026-10-23 — v0.30.0**: hosted surface reaches **76 tools** (44 free verification + 2 risk + 16 CyberLibrary + **14 web**), adding `web_extract_schema`, `web_diff`, `web_crawl_async` and `web_crawl_job`; reference implementation remains 45 tools. Registry/Glama/Smithery metadata synced to the hosted surface.
- **2026-10-20 — v0.26.0**: public tool order is now web-first / high-frequency-first (`tools/list`, `server.json`, README table); hosted endpoint stays at 72 tools (44 free + 2 risk + 16 CyberLibrary + 10 web), reference implementation remains 45 tools.
- **2026-10-19 — v0.25.2**: hosted surface reaches 72 tools (10 web tools incl. `web_crawl`/`web_extract`/`web_act` and OCR; self-hosted SearXNG primary for `web_search`; 16 CyberLibrary tools). This release also ports the defect fixes that shipped in the hosted service (v0.22.0/v0.22.1) into this 45-tool reference implementation. Highlights: JSON-safe truncation (responses
  over the transport cap stay **valid JSON** and carry `_truncated` / `_original_chars` metadata
  instead of being sliced mid-object); bech32m/BIP350 witness-v1+ and base58check address
  validation; Zcash **Ironwood** shielded-pool accounting; `eth_read_contract` raw selector and
  ABI-parameter prefix handling (no more silent `lstrip("0x")` corruption); TRON contract-revert
  detection, `has_code` via `getcontract`, USDT verdict finality split, confirmation off-by-one,
  risk-address base58 checksum + 41-hex validation and unactivated-account coverage notes;
  consistent `isError` for every tool; strict batch/HTTP handling (batch element validation,
  405 for DELETE/PUT/HEAD, 413 body cap, 415 Content-Type, 404 path, complete CORS preflight,
  trusted client IP via `X-Real-IP`, `Retry-After` on 429); tool `title` + annotations
  (`readOnlyHint` etc.) and `additionalProperties` normalization in `tools/list`; `web_fetch`
  `max_chars` cap/budget and HTTP 4xx passthrough instead of a silent provider fallback; dynamic
  XMR output-distribution lag with the real chain tip; Monero proof-check error semantics; and
  fee estimates priced from `eth_feeHistory` percentiles (p50 tip for a transfer, p90 for an
  ERC-20 transfer) instead of a zero `eth_maxPriorityFeePerGas`. Version fields in `server.json`,
  `mcp-registry.json` and `smithery.yaml` follow; `glama.json` carries no version field.

## License

MIT — see [LICENSE](./LICENSE).

---

<sub>RobotBase · verification & risk intelligence for Web3 agents · <https://robotbase.cc/></sub>
