# The MCP source broker control app: the exact gateway image plus a broker
# entrypoint. The base is NOT a moving tag - pass the running gateway digest
# (the make target resolves it) or LITELLM_SRC_BASE explicitly, so the broker
# always carries the gateway code it runs beside.
ARG LITELLM_SRC_BASE
FROM ${LITELLM_SRC_BASE}
ENV LITELLM_LOCAL_MODEL_COST_MAP=true
ENTRYPOINT ["/app/.venv/bin/python", "-m", "litellm.proxy.mcp_source_broker.main"]
