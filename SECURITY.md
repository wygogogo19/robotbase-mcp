# Security Policy

RobotBase runs a public, read-only MCP endpoint at **https://robotbase.cc/mcp**. This file
covers the hosted service and the reference implementation in this repository.

## Supported versions

| Surface | Supported |
|---|---|
| Hosted endpoint `https://robotbase.cc/mcp` | current production version |
| This repository (`main`) | current release tag |

## Reporting a vulnerability

- Email **ops@robotbase.cc** with a description, reproduction steps and impact.
- Or use GitHub's private vulnerability reporting (**Security → Advisories → Report a vulnerability**).
- Please **do not** open a public issue for a security report.

We aim to acknowledge reports within **3 business days** and to agree a disclosure timeline
with the reporter. Please give us a reasonable window to fix the issue before publishing.

## In scope

- Authentication/authorization bypass, tool-call validation bypass
- SSRF or access to internal/private networks through `web_fetch` / `web_*` tools
- Exposure of secrets, provider keys, internal addresses or other users' data
- x402 payment/credit integrity: replay, double-spend, quota bypass, forged settlement
- Rate-limit bypass, resource exhaustion, injection into tool responses
- Supply-chain issues in this repository (dependencies, CI workflows, release artifacts)

## Out of scope

- Third-party providers (Cloudflare, search providers, blockchain networks) and their outages
- Blockchain consensus, node peering or chain-level attacks
- Social engineering, physical access, spam/abuse reports without a security impact
- Denial-of-service testing, brute force, or automated scanning that degrades the service

## Testing rules

- The service is read-only; it never holds funds or signs transactions.
- Do not attempt to access other users' payment data, bypass quotas at scale, or run DoS tests.
- Use your own test addresses/URLs; do not publish proof-of-concept payloads that could harm
  third parties.
- Keep requests within published rate limits; stop when asked.

## Security posture

- Read-only MCP surface; no custody, no API keys, no user accounts.
- `web_fetch` enforces an SSRF guard (scheme/port/DNS/redirect checks) and respects `robots.txt`.
- Audit data stores only a truncated `sha256` of the client IP and never stores tool arguments.
- Secret material is kept server-side (mode 600) and is never committed to this repository.
