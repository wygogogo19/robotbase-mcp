// Minimal x402 v2 payer for RobotBase MCP (Base mainnet USDC).
// Usage:
//   npm install viem
//   PRIVATE_KEY=0x... node examples/x402-pay.mjs
// Never commit or paste the private key anywhere.

import { randomBytes } from "node:crypto";
import { createWalletClient, http } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { base } from "viem/chains";

const MCP = "https://robotbase.cc/mcp";
const KEY = process.env.PRIVATE_KEY;
if (!KEY) throw new Error("set PRIVATE_KEY");
const PK = KEY.startsWith("0x") ? KEY : "0x" + KEY;
const account = privateKeyToAccount(PK);
const wallet = createWalletClient({ account, chain: base, transport: http() });

const toolCall = (payment) => ({
  jsonrpc: "2.0",
  id: 1,
  method: "tools/call",
  params: {
    name: "evm_rpc",
    arguments: { chain: "base", method: "eth_blockNumber", params: [] },
    ...(payment ? { _meta: { "x402/payment": payment } } : {}),
  },
});

async function post(body) {
  const res = await fetch(MCP, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return await res.json();
}

// 1) Force the 10/day free allowance to run out and read accepts[].
let challenge;
for (let i = 0; i < 12; i++) {
  const data = await post(toolCall(null));
  const sc = data?.result?.structuredContent;
  if (sc?.accepts?.length) { challenge = sc; break; }
}
if (!challenge) throw new Error("did not receive x402 accepts[]");

// 2) Pick Base USDC 0.10 (10 credits).
const req = challenge.accepts.find(
  (a) => a.network === "eip155:8453" && String(a.amount) === "100000"
);
if (!req) throw new Error("Base 0.10 USDC option not found");

const now = Math.floor(Date.now() / 1000);
const validAfter = now - 60;
const validBefore = now + 600;
const nonce = "0x" + randomBytes(32).toString("hex");

// EIP-3009 TransferWithAuthorization (USDC on Base).
const domain = {
  name: req.extra?.name || "USD Coin",
  version: req.extra?.version || "2",
  chainId: 8453,
  verifyingContract: req.asset,
};
const types = {
  TransferWithAuthorization: [
    { name: "from", type: "address" },
    { name: "to", type: "address" },
    { name: "value", type: "uint256" },
    { name: "validAfter", type: "uint256" },
    { name: "validBefore", type: "uint256" },
    { name: "nonce", type: "bytes32" },
  ],
};
const message = {
  from: account.address,
  to: req.payTo,
  value: BigInt(req.amount),
  validAfter: BigInt(validAfter),
  validBefore: BigInt(validBefore),
  nonce,
};
const signature = await wallet.signTypedData({
  account,
  domain,
  types,
  primaryType: "TransferWithAuthorization",
  message,
});

// 3) Send the x402 payload in params._meta.
const payment = {
  x402Version: 2,
  scheme: "exact",
  network: req.network,
  accepted: req,
  payload: {
    signature,
    authorization: {
      from: account.address,
      to: req.payTo,
      value: String(req.amount),
      validAfter: String(validAfter),
      validBefore: String(validBefore),
      nonce,
    },
  },
};
const result = await post(toolCall(payment));
console.log(JSON.stringify(result, null, 2));
