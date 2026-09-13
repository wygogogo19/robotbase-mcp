# Reference container for self-hosting the RobotBase MCP Server against your own nodes.
# The hosted service at https://robotbase.cc/mcp needs no container.
FROM python:3.12-slim

WORKDIR /app
COPY server.py ./

ENV RB_BIND=0.0.0.0 \
    RB_PORT=8090 \
    PYTHONUNBUFFERED=1

EXPOSE 8090

# Tool endpoints are configured with RB_* environment variables (see examples/env.example).
CMD ["python3", "server.py"]
