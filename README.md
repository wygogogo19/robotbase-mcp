# RobotBase MCP Server

> **Status 2026-11-26 — v0.35.1.** Hosted Web3 serves **71 read-only tools across 10 live chains**.
> `server_editions.py` is the full hosted multi-edition implementation.
> `server.py` remains the dependency-free reference subset for local self-hosting.

**One MCP endpoint for agents: search the web, read public pages, verify on-chain truth, and pay per call.**

## Endpoints

| Edition | Endpoint | Tools | Audience |
|---|---|---:|---|
| **Web3** | `https://web3.robotbase.cc/mcp` | **71** | chain verification, risk, prediction markets, crypto trends |
| **Dev** | `https://dev.robotbase.cc/mcp` | **18** | GitHub / HF / npm / PyPI / HN trends + web |
| **Content** | `https://content.robotbase.cc/mcp` | **35** | social hot lists, attention trends, CyberLibrary + web |
| Compatibility alias | `https://robotbase.cc/mcp` | **71** | alias of the Web3 edition |

All editions include the same **17-tool web toolbox**.

## Live chains

| Chain | Data path |
|---|---|
| BTC / LTC / XMR / ZEC | self-hosted full nodes |
| ETH / TRON / Base / Solana / BSC / Polygon | managed external RPC |

No raw RPC endpoint is published. Every read path is the MCP surface.

## Web3 tool surface (71)

- **17 web tools** — search, fetch, screenshot, map, batch, feed, metadata, crawl, extract, act, schema extraction, diff, async crawl and scheduled page monitors
- **4 status/overview tools** — `list_chains`, `chain_status`, `robotbase_services`, `robotbase_capabilities`
- **BTC / LTC / XMR / ZEC / ETH / TRON** — chain verification, block/transaction/account reads, fee and mempool state, shielded pools, TRC-20 decoding, risk and settlement checks
- **Base / Polygon / BSC** — `evm_rpc` for allowlisted read-only EVM JSON-RPC
- **Solana** — `solana_rpc` for allowlisted read-only Solana JSON-RPC
- **2 market tools** — `prediction_market_odds`, `crypto_trending`

Example calls:

```bash
curl -s https://robotbase.cc/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"chain_status","arguments":{"chain":"base"}}}'

curl -s https://robotbase.cc/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"evm_rpc","arguments":{"chain":"base","method":"eth_blockNumber","params":[]}}}'
```

## Pricing

- **10 free calls/day/IP**, shared across all tools and editions
- Past the free pool:
  - x402 USDC on Base or Solana
  - or an optional XMR prepaid key
- x402 requires no account; the credits dashboard supports Google/GitHub login
- Read-only by construction: no signing, no trading, no custody, no raw RPC

## Full hosted implementation

`server_editions.py` contains the full multi-edition server:

```bash
RB_EDITION=web3 RB_PORT=8090 python3 server_editions.py
```

It includes all 17 web tools, CyberLibrary, chain tools, TRON tools, generic EVM/Solana RPC tools, the shared free-call gate, x402 and the optional XMR prepaid key path.

`server.py` is the smaller dependency-free reference implementation for BTC/XMR/ZEC. It is intentionally a subset and is kept for local experimentation.

## Docker

```bash
docker build -t robotbase-mcp .
docker run --rm -p 8090:8090 -e RB_EDITION=web3 robotbase-mcp
```

## Configuration

All backends and secrets are environment-driven. No credentials are committed.

Key variables include:

```text
RB_EDITION=web3            # web3 | dev | content
RB_PORT=8090
RB_BIND=127.0.0.1

# self-hosted chain endpoints
RB_BTC_RPC_URL, RB_BTC_RPC_USER, RB_BTC_RPC_PASS
RB_ELECTRS_HOST, RB_ELECTRS_PORT
RB_LTC_RPC_URL
XMR_HELPER
ZEC_HELPER, RB_ZEC_BASE
TRON_NODE, TRON_SOLIDITY
RB_TRON_HTTP_BASE

# external RPC endpoints (managed)
BASE_RPC, POLYGON_RPC, BSC_RPC, SOLANA_RPC
EXTERNAL_RPC_KEY2

# web layer
RB_SEARXNG_URL, RB_SERPER_KEY, RB_TAVILY_KEY
```

See `examples/env.example` for a local reference setup.

## MCP Registry

- `io.github.wygogogo19/robotbase-mcp` — Web3 alias
- `io.github.wygogogo19/robotbase-web3` — Web3
- `io.github.wygogogo19/robotbase-dev` — Dev
- `io.github.wygogogo19/robotbase-content` — Content

## License

MIT
