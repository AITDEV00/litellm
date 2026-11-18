// Package listen owns the protocol and sizing choices for the sidecar's two
// listeners. It exists so the worker and the test fixture agree on h2c versus
// HTTP/1.1 negotiation without either depending on a data-plane package.
package listen

import (
	"net/http"
	"time"

	"oicm-litellm-layer/mcp-source-link/internal/wire"
)

// HTTP2Config sizes a listener for many concurrent streams.
func HTTP2Config() *http.HTTP2Config {
	return &http.HTTP2Config{
		MaxConcurrentStreams:          wire.StreamLimit,
		MaxReceiveBufferPerStream:     wire.StreamWindow,
		MaxReceiveBufferPerConnection: wire.ConnectionBuffer,
		SendPingTimeout:               wire.SendPingAfter * time.Second,
		PingTimeout:                   wire.PingTimeout * time.Second,
	}
}

// Server builds the connector-facing listener with h2c-only protocol support.
// The tunnel plane is gRPC over cleartext HTTP/2 and serves nothing on
// HTTP/1.1, so accepting it would only produce confusing 4xx responses.
func Server() *http.Server {
	return &http.Server{
		ReadHeaderTimeout: 5 * time.Second,
		MaxHeaderBytes:    32 << 10,
		HTTP2:             HTTP2Config(),
		Protocols:         H2C(),
	}
}

// H2C serves only unencrypted HTTP/2 on connector-facing listeners.
func H2C() *http.Protocols {
	protocols := new(http.Protocols)
	protocols.SetUnencryptedHTTP2(true)
	protocols.SetHTTP1(false)
	return protocols
}

// Data keeps the shared private dispatch listener backward-compatible with the
// Python control app's HTTP/1.1 revocation and readiness calls, while letting
// an HTTP/2 client multiplex without per-request TCP ports.
func Data() *http.Protocols {
	protocols := new(http.Protocols)
	protocols.SetUnencryptedHTTP2(true)
	protocols.SetHTTP1(true)
	return protocols
}
