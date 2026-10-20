# Installing RobotBase MCP (for AI agents / Cline / Codex / Claude Code / Cursor)

RobotBase is one remote MCP endpoint: live web search + fetch, on-chain verification and
risk on five self-hosted chains, and 80k+ public-domain books. No API key, no account, no
local process or container is required for the hosted endpoint.

## Endpoint

- URL: `https://robotbase.cc/mcp`
- Transport: Streamable HTTP (MCP protocol `2025-06-18`)
- Auth: none for the free tier; optional x402 payment only after free allowances are used
- Docs: <https://robotbase.cc/mcp/docs> · Tools JSON: <https://robotbase.cc/mcp/tools>
- Server card: <https://robotbase.cc/mcp/server.json> · llms.txt: <https://robotbase.cc/llms.txt>

## Install (one of these)

**Cline / VS Code**
1. Open Cline → MCP Servers → Remote (or "Add remote server").
2. Paste `https://robotbase.cc/mcp` and save/approve. No config, no keys.
3. Ask: "Search the latest Bitcoin fee market news, then fetch the top result and summarize it."

**Claude Code**
```bash
claude mcp add --transport http robotbase https://robotbase.cc/mcp
```

**Cursor / Claude Desktop / Codex / other MCP clients**
```json
{
  "mcpServers": {
    "robotbase": { "type": "http", "url": "https://robotbase.cc/mcp" }
  }
}
```

## What you get

- 10 web tools: `web_search`, `web_fetch`, `web_screenshot`, `web_map`, `web_fetch_batch`,
  `web_feed`, `web_metadata`, `web_crawl`, `web_extract`, `web_act`.
- 44 free verification tools across Bitcoin, Ethereum, Monero, Zcash and Tron
  (`btc_get_fee_rates`, `btc_get_transaction`, `eth_read_contract`, `xmr_verify_payment`,
  `zec_get_pool_status`, `tron_verify_usdt_transfer`, ...).
- 2 TRON risk tools + 16 CyberLibrary book tools (catalog, TOC, chapter, Markdown, citations,
  title search, full-text search, RAG context).

## Free tier and limits (per IP, per day)

| Area | Free allowance | After |
|---|---|---|
| Verification tools | free forever, no key | — |
| Web search | 20 calls/day | 1 credit per call (x402) |
| Fetch family | 50 calls/day | 1 credit per call (x402) |
| Risk + CyberLibrary | 5 calls/day (shared) | 1–5 credits (risk 1, retrieval 1–2, full-text 5) |

Credits are prepaid USDC packs, paid by the caller's own wallet over x402 (USDC on Base or
Solana), **not** an API key or subscription. Base and Solana are payment rails only; no
Base/Solana chain data is served. Free tier needs no wallet.

## Test prompts

- "What changed in the Bitcoin fee market today? Search, read the best result, then check BTC fee rates."
- "Read this URL and give me a clean Markdown summary."
- "Did this USDT transfer land, and is the sender risky?"
- "Find every mention of 'ledger' in Moby Dick, with citations."
- "Verify this Monero payment using its tx key."

## Troubleshooting

- **402 Payment Required** — free allowance for that tool bucket is used. Use another bucket,
  wait for the daily reset (00:00 Asia/Shanghai), or pay a credit pack; the 402 body contains
  the machine-readable `accepts[]` terms.
- **Timeout / blocked site** — `web_fetch` tries direct fetch first, then a provider fallback;
  some sites are unreachable from our egress. The tool returns a readable error rather than
  empty content.
- **Tool not found** — refresh the client's tool list; the hosted surface currently has 72 tools.

## Self-hosting (optional)

The public repository is a dependency-free reference implementation:
<https://github.com/wygogogo19/robotbase-mcp>. The hosted endpoint additionally bundles
CyberLibrary and extra web tools. See the repo README for `RB_*` environment variables.

## Safety

Read-only; no custody; no signing; no API keys are issued. Do not use it to attempt login or
paywall bypass; `web_fetch` respects `robots.txt`. Report issues to `ops@robotbase.cc`.
