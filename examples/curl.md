# Calling RobotBase MCP with curl

Endpoint: `https://robotbase.cc/mcp` (Streamable HTTP, JSON-RPC 2.0)

## 1. Handshake

```bash
curl -s https://robotbase.cc/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-06-18","capabilities":{},
                 "clientInfo":{"name":"curl","version":"1"}}}'
```

## 2. List tools

```bash
curl -s https://robotbase.cc/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

## 3. Call a tool

```bash
# BTC fees right now
curl -s https://robotbase.cc/mcp -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call",
       "params":{"name":"btc_get_fee_rates","arguments":{}}}'

# Zcash shielded-pool supply
curl -s https://robotbase.cc/mcp -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call",
       "params":{"name":"zec_get_pool_status","arguments":{}}}'

# Monero node state, read straight off our own monerod
curl -s https://robotbase.cc/mcp -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":5,"method":"tools/call",
       "params":{"name":"xmr_get_node_status","arguments":{}}}'
```

## 4. Web tools (search + fetch)

`web_search` ranks the live web (Serper primary; Tavily / Brave / cn.bing RSS fallbacks) and
`web_fetch` returns the readable text of one public URL. They have their own free allowance —
20 searches and 50 fetches per day per IP, in separate buckets from the risk tools — and then cost
1 x402 credit per call. Results may be cached for up to 10 minutes.

```bash
# search: title / url / snippet / published, plus the provider that answered
curl -s https://robotbase.cc/mcp -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":6,"method":"tools/call",
       "params":{"name":"web_search",
                 "arguments":{"query":"bitcoin fee market","count":3}}}'

# fetch one public page as clean markdown (robots.txt-respecting, SSRF-guarded)
curl -s https://robotbase.cc/mcp -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":7,"method":"tools/call",
       "params":{"name":"web_fetch",
                 "arguments":{"url":"https://docs.python.org/3/library/json.html",
                              "max_chars":8000}}}'
```

The response body is capped at 12,000 characters; raise `max_chars` (up to 20,000) if you need
more of the page. `source` is `direct` when this node fetched the page itself and `provider` when
a blocked page was fetched through the provider fallback.

## 5. Metered risk tools and x402

There is **no API key**: `X-API-Key` is not part of this service and sending it changes nothing.
The two risk tools have a free allowance of 5 calls/day per IP; past that the call returns
`Payment Required` with machine-readable terms in `structuredContent.accepts[]` (USDC on Base or
Solana, 10 calls / 0.10 USDC or 100 calls / 1.00 USDC; Base and Solana are a payment rail only —
no Base or Solana chain data is served):

```bash
curl -s https://robotbase.cc/mcp -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":6,"method":"tools/call",
       "params":{"name":"tron_check_address_risk","arguments":{"address":"T..."}}}'
```

Settle one pack, then re-send **the same payment payload** on later calls — it is idempotent per
payment nonce, so it is charged once and each call spends one credit:

```bash
curl -s https://robotbase.cc/mcp -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":7,"method":"tools/call",
       "params":{"name":"tron_check_address_risk","arguments":{"address":"T..."},
                 "_meta":{"x402/payment":{ ... settled payment payload ... }}}}'
```

Response headers `X-MCP-Plan` (`anon`) and `X-RateLimit-Limit` show the effective quota — 120 req/min
per IP, the only tier there is.

## 6. Usage audit

```bash
curl -s 'https://robotbase.cc/mcp/stats?hours=24'          # JSON
open 'https://robotbase.cc/mcp/stats?hours=24'             # HTML dashboard
```
