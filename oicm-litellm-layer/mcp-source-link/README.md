# mcp-source-link

The MCP source broker's Go side. One binary, two modes: `worker` runs as a sidecar beside the Python broker and owns two listeners, `connector` runs on a source host and dials out. What the running system does, the port map, and the transport tradeoffs live in `../docs/mcp/source-broker/running-system/README.md`; this file covers only how to build, test, and change the module

## Toolchain

Go 1.27.4, pinned in `go.mod` and in `.go-version`. Additive change policy and the other repo-wide rules are in the root `AGENTS.md`

The repo Makefile expects the Go binary and module cache this module has always used. `make -C oicm-litellm-layer mcp-sidecar-build` drives them through `GO_BIN` (`/tmp/sa-build/bin/go` by default) and `GO_ENV` (which also pins `GOPATH`, `GOMODCACHE`, and `CGO_ENABLED=0`). Override on a machine with a system toolchain:

```
make -C oicm-litellm-layer mcp-sidecar-check GO_BIN=$(command -v go)
```

## Build

```
make -C oicm-litellm-layer mcp-sidecar-build
```

That compiles a `CGO_ENABLED=0` static binary, asserts it is statically linked, then bakes it into a `FROM scratch` image. The static check is not cosmetic: the runtime image has no dynamic loader, so a cgo-linked binary fails at exec

## Test

```
cd oicm-litellm-layer/mcp-source-link && go test ./...
cd oicm-litellm-layer/mcp-source-link && CGO_ENABLED=1 go test -race ./...
```

The build stays `CGO_ENABLED=0`; only test invocations switch to 1, because the race detector requires cgo

`internal/harness` is test support only and is not imported by the binary. It stands up a fixture broker, a reverse-h2 server, and a tunnel server so behavior tests can drive real transports without a cluster

## Lint

```
make -C oicm-litellm-layer mcp-sidecar-check
```

Runs gofmt, `go vet`, `staticcheck`, regenerates `dispatchpb` and fails on drift, then runs the race tests. `staticcheck` is not part of the Go distribution, so it gets its own `STATICCHECK` variable (`/tmp/rd-gopath/bin/staticcheck` by default):

```
make -C oicm-litellm-layer mcp-sidecar-check STATICCHECK=$(command -v staticcheck)
```

`make check` runs this target via `scripts/pre_commit_lint.sh` whenever a file in this module is in scope, so a green local `make check` means this gate passed too

## Regenerating the wire contract

`dispatchpb/dispatch.proto` is the contract between sidecar and connector, and the generated stubs are committed. After any edit to the proto, regenerate and commit the result:

```
cd oicm-litellm-layer/mcp-source-link && go generate ./dispatchpb
```

The two plugins are pinned in `go.mod`'s `tool` block, so `go install` without a version in this directory installs exactly the versions the committed stubs were built with:

```
cd oicm-litellm-layer/mcp-source-link && go install google.golang.org/protobuf/cmd/protoc-gen-go google.golang.org/grpc/cmd/protoc-gen-go-grpc
```

`protoc` itself is a system binary, not a Go module. libprotoc 36.2 produced the committed stubs; `test-mcp-source-link.yml` pins that version and its checksum. The `go:generate` line runs from the module root on purpose, so the generated `source:` path stays `dispatchpb/dispatch.proto`; generating from inside `dispatchpb/` rewrites it to `dispatch.proto` and every regen then shows a spurious diff

## Layout

One package per behavior, under `internal/`. `main.go` is wiring only and is the sole file at the module root, because Go requires `package main` there for the binary

| Package | Owns |
|---|---|
| `internal/wire` | shared envelope types and the size and deadline limits both sides enforce |
| `internal/config` | flags and environment to a validated config, plus TLS |
| `internal/control` | the broker control-plane client: report, refresh, peers, revoke |
| `internal/pool` | member eligibility, selection, forwarding, and session registry |
| `internal/tunnel` | the `jhump/grpctunnel` transport, admit, probe, and stream auth |
| `internal/reverseh2` | the reverse-h2 transport: sessions, handlers, and the h2c server |
| `internal/connector` | connector-side dial, serve, and dispatch loops for both transports |
| `internal/harness` | test fixtures only, never linked into the binary |
