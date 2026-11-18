package reverseh2

import (
	"net/http"
	"time"

	"oicm-litellm-layer/mcp-source-link/internal/pool"
	"oicm-litellm-layer/mcp-source-link/internal/wire"
)

// Plane serves the long-poll routes on the reverse listener.
type Plane struct {
	pool *pool.Runtime
}

// New builds the plane over the shared session registry.
func New(p *pool.Runtime) *Plane { return &Plane{pool: p} }

// Routes registers the four connector-facing handlers.
func (p *Plane) Routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /mcp-source-link-reverse", p.RegisterHandler)
	mux.HandleFunc("POST /mcp-source-link-hold", p.HoldHandler)
	mux.HandleFunc("POST /mcp-source-link-dispatch", p.DispatchHandler)
	mux.HandleFunc("POST /mcp-source-link-respond", p.RespondHandler)
	return mux
}

// HTTP2Config sizes the reverse listener for many concurrent streams.
func HTTP2Config() *http.HTTP2Config {
	return &http.HTTP2Config{
		MaxConcurrentStreams:          wire.StreamLimit,
		MaxReceiveBufferPerStream:     wire.StreamWindow,
		MaxReceiveBufferPerConnection: wire.ConnectionBuffer,
		SendPingTimeout:               wire.SendPingAfter * time.Second,
		PingTimeout:                   wire.PingTimeout * time.Second,
	}
}

// Server builds the reverse listener with h2c-only protocol support.
func Server() *http.Server {
	return &http.Server{
		ReadHeaderTimeout: 5 * time.Second,
		MaxHeaderBytes:    32 << 10,
		HTTP2:             HTTP2Config(),
		Protocols:         H2CProtocols(),
	}
}

// H2CProtocols serves only unencrypted HTTP/2 on connector-facing listeners.
func H2CProtocols() *http.Protocols {
	protocols := new(http.Protocols)
	protocols.SetUnencryptedHTTP2(true)
	protocols.SetHTTP1(false)
	return protocols
}

// DataProtocols keeps the shared private dispatch listener backward-compatible
// with the Python control app's HTTP/1.1 revocation and readiness calls, while
// letting an HTTP/2 client multiplex without per-request TCP ports.
func DataProtocols() *http.Protocols {
	protocols := new(http.Protocols)
	protocols.SetUnencryptedHTTP2(true)
	protocols.SetHTTP1(true)
	return protocols
}
