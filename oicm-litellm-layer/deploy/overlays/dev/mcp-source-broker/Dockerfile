ARG LITELLM_SRC_BASE=registry.adeoaiengine.ecouncil.ae/openinnovationai/platform/mlops/mlops-serving/litellm-src:jya0-v1.108.5
FROM ${LITELLM_SRC_BASE}

ARG CHISEL_SHA256=f3f180f1d93aa72cce4e6386f98cc06569a0146fbd65eb4423cf83e6434bcfe6
COPY chisel.gz /tmp/chisel.gz
RUN echo "${CHISEL_SHA256}  /tmp/chisel.gz" | sha256sum -c - && gzip -dc /tmp/chisel.gz > /usr/local/bin/chisel && chmod 500 /usr/local/bin/chisel && rm -f /tmp/chisel.gz
COPY relay_entrypoint.sh /usr/local/bin/mcp-relay-entrypoint
RUN chmod 500 /usr/local/bin/mcp-relay-entrypoint
ENV LITELLM_LOCAL_MODEL_COST_MAP=true
ENTRYPOINT ["/app/.venv/bin/python","-m","litellm.proxy.mcp_source_broker.main"]
