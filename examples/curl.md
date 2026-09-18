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
       "params":{"name":"btc_fee_estimates","arguments":{}}}'

# Zcash shielded-pool supply
curl -s https://robotbase.cc/mcp -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call",
       "params":{"name":"zec_chain_info","arguments":{}}}'

# Kaspa: did our solo hashport find a real mainnet block?
curl -s https://robotbase.cc/mcp -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":5,"method":"tools/call",
       "params":{"name":"kas_pool_status","arguments":{}}}'

# Ravencoin: is the node synced and where is the GPU hashport?
curl -s https://robotbase.cc/mcp -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":6,"method":"tools/call",
       "params":{"name":"rvn_pool_status","arguments":{}}}'
```

## 4. Higher rate limit (optional)

```bash
curl -s https://robotbase.cc/mcp \
  -H 'content-type: application/json' \
  -H 'X-API-Key: rb_your_key_here' \
  -d '{"jsonrpc":"2.0","id":6,"method":"tools/list"}'
```

Response headers `X-MCP-Plan` (`anon` / `key`) and `X-RateLimit-Limit` show the effective quota.

## 5. Usage audit

```bash
curl -s 'https://robotbase.cc/mcp/stats?hours=24'          # JSON
open 'https://robotbase.cc/mcp/stats?hours=24'             # HTML dashboard
```
