# The reverse-h2 sidecar for the MCP source broker. The binary must be built
# outside this Dockerfile (CGO_ENABLED=0, static) because no Go toolchain image
# is pullable from the internal registry; `make mcp-sidecar-build` runs the
# build and drops the binary at ./source-link right before this file runs.
# FROM scratch keeps the surface minimal: one static ELF, no shell, no libc.
FROM scratch
COPY source-link /source-link
ENTRYPOINT ["/source-link"]
