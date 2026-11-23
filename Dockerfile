# Full hosted multi-edition MCP server (Web3 72 tools / Dev 18 / Content 35).
# All chain and provider endpoints are environment-driven; no secrets are baked in.
FROM python:3.12-slim

WORKDIR /app
COPY server_editions.py server.py x402_pay.py ./

ENV RB_BIND=0.0.0.0 \
    RB_EDITION=web3 \
    RB_PORT=8090 \
    PYTHONUNBUFFERED=1

EXPOSE 8090

CMD ["python3", "server_editions.py"]
