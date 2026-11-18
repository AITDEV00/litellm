package connector

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"log"
	"math/rand/v2"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"sync/atomic"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/connectivity"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/jhump/grpctunnel"
	"github.com/jhump/grpctunnel/tunnelpb"

	"oicm-litellm-layer/mcp-source-link/dispatchpb"
	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/tunnel"
	"oicm-litellm-layer/mcp-source-link/internal/wire"
)

// dialTimeout bounds how long NewClient may take to reach Ready before the
// caller reports a dial failure and the reconnect loop backs off.
const dialTimeout = 15 * time.Second

// Capabilities is the capability set this connector build implements. The
// broker gates each pushed RPC on this list.
var Capabilities = []string{"execute", "probe"}

// DialTarget converts the public HTTPS gateway URL into the host:port form
// gRPC requires, applying 443 when the URL omits a port. Passing the raw URL
// to grpc.Dial leaves the scheme in the address, which the dialer cannot
// parse and every attempt dies on the dial timeout.
func DialTarget(gateway string) (string, error) {
	parsed, err := url.Parse(gateway)
	if err != nil {
		return "", err
	}
	if parsed.Hostname() == "" {
		return "", errors.New("gateway URL has no host")
	}
	port := parsed.Port()
	if port == "" {
		port = "443"
	}
	return net.JoinHostPort(parsed.Hostname(), port), nil
}

func logConnectorError(kind string, count *atomic.Uint64, err error) {
	n := count.Add(1)
	if n <= 3 || n%1000 == 0 {
		log.Printf("%s count=%d: %v", kind, n, err)
	}
}

// Server implements DispatchService on the connector: it answers the
// broker's pushed RPCs by round-tripping the connector's origin.
type Server struct {
	dispatchpb.UnimplementedDispatchServiceServer
	origin          http.RoundTripper
	authority       string
	roundTripErrors atomic.Uint64
	readErrors      atomic.Uint64
}

// NewServer builds a dispatch server over the given origin transport.
//
// authority is the Host header the origin should receive. It is the target's own
// host (an IP literal, or a Kubernetes service DNS name), so an origin behind a
// service router sees the name it is deployed under rather than an unrelated
// placeholder.
func NewServer(origin http.RoundTripper, authority string) *Server {
	return &Server{origin: origin, authority: authority}
}

// Admit answers the capability handshake.
func (d *Server) Admit(_ context.Context, _ *dispatchpb.AdmitRequest) (*dispatchpb.AdmitResponse, error) {
	return &dispatchpb.AdmitResponse{Capabilities: Capabilities}, nil
}

// Probe answers a catalog probe by fetching the origin's OpenAPI document.
//
// The nonce is echoed to the origin for an origin that still signs, but a missing
// signature is no longer an error: an unsigned catalog is the norm for a local
// source. The broker records the catalog digest as the fingerprint either way.
func (d *Server) Probe(ctx context.Context, req *dispatchpb.ProbeRequest) (*dispatchpb.ProbeResponse, error) {
	target := url.URL{Scheme: "http", Host: d.authority, Path: "/openapi.json"}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodGet, target.String(), nil)
	if err != nil {
		return nil, err
	}
	httpReq.Header.Set("X-Source-Nonce", req.Nonce)
	resp, err := d.origin.RoundTrip(httpReq)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("origin catalog probe failed: %d", resp.StatusCode)
	}
	contents, err := io.ReadAll(io.LimitReader(resp.Body, tunnel.MaxCatalog+1))
	if err != nil {
		return nil, err
	}
	if len(contents) > tunnel.MaxCatalog {
		return nil, errors.New("origin catalog exceeds the dispatch body limit")
	}
	// Absent signature is allowed; a present one is carried through for the
	// broker's optional check.
	signature, _ := base64.StdEncoding.DecodeString(resp.Header.Get("X-Source-Signature"))
	return &dispatchpb.ProbeResponse{CatalogJson: contents, ProofSignature: signature}, nil
}

// Execute runs one dispatched request against the origin.
func (d *Server) Execute(ctx context.Context, req *dispatchpb.DispatchRequest) (*dispatchpb.DispatchResponse, error) {
	if len(req.Body) > wire.MaxOriginBody {
		return nil, status.Error(codes.ResourceExhausted, "request body exceeds the dispatch limit")
	}
	target := url.URL{Scheme: "http", Host: d.authority, Path: req.Path, RawQuery: req.Query}
	httpReq, err := http.NewRequestWithContext(ctx, req.Method, target.String(), bytes.NewReader(req.Body))
	if err != nil {
		return nil, err
	}
	for _, header := range req.Headers {
		httpReq.Header.Add(header.Name, header.Value)
	}
	resp, err := d.origin.RoundTrip(httpReq)
	if err != nil {
		logConnectorError("connector origin execute failure", &d.roundTripErrors, err)
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, wire.MaxOriginBody+1))
	if err != nil {
		logConnectorError("connector origin response read failure", &d.readErrors, err)
		return nil, err
	}
	if len(body) > wire.MaxOriginBody {
		return nil, status.Error(codes.ResourceExhausted, "origin response exceeds the dispatch limit")
	}
	headers := make([]*dispatchpb.Header, 0, len(resp.Header))
	for name, values := range resp.Header {
		for _, value := range values {
			headers = append(headers, &dispatchpb.Header{Name: name, Value: value})
		}
	}
	return &dispatchpb.DispatchResponse{Status: int32(resp.StatusCode), Headers: headers, Body: body}, nil
}

// RunTunnel is the tunnel transport's reconnect loop: dial the broker with
// TLS, attach identity metadata, and serve the reverse tunnel.
func RunTunnel(
	ctx context.Context,
	cfg config.ConnectorConfig,
	gateway string,
	tlsConfig *tls.Config,
	origin http.RoundTripper,
	authority string,
) error {
	dialTarget, err := DialTarget(gateway)
	if err != nil {
		return err
	}
	creds := credentials.NewTLS(tlsConfig)
	server := NewServer(origin, authority)
	delay := 300 * time.Millisecond
	for ctx.Err() == nil {
		started := time.Now()
		err := tunnelServe(ctx, cfg, dialTarget, creds, server)
		if ctx.Err() != nil {
			return nil
		}
		if time.Since(started) > 30*time.Second {
			delay = 300 * time.Millisecond
		}
		log.Printf("tunnel connector %s disconnected: %v", cfg.WorkerID, err)
		timer := time.NewTimer(delay + time.Duration(rand.Int64N(int64(delay/2))))
		select {
		case <-ctx.Done():
			timer.Stop()
		case <-timer.C:
		}
		delay = min(15*time.Second, delay*2)
	}
	return nil
}

// tunnelServe opens one reverse tunnel and serves pushed RPCs on it until it
// drops. Identity metadata rides the tunnel-opening stream and is validated
// by the worker before the tunnel establishes.
func tunnelServe(ctx context.Context, cfg config.ConnectorConfig, target string, creds credentials.TransportCredentials, server *Server) error {
	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(creds))
	if err != nil {
		return err
	}
	defer conn.Close()
	// NewClient does not dial, so an unreachable endpoint would otherwise fail
	// only when the tunnel stream is first used, or stall until the parent
	// context ends on a black-holed address. Wait for Ready under a deadline so
	// a bad endpoint reports a dial-shaped error the retry loop can log.
	if err := waitReady(ctx, conn, target); err != nil {
		return err
	}
	// NewClient is lazy, so the connection is only proven once the tunnel
	// stream is established. A broken endpoint therefore surfaces here as a
	// stream error rather than a dial error, which the caller's retry loop
	// handles identically.
	conn.Connect()
	tunnelServer := grpctunnel.NewReverseTunnelServer(tunnelpb.NewTunnelServiceClient(conn))
	dispatchpb.RegisterDispatchServiceServer(tunnelServer, server)
	sessionCtx := metadata.AppendToOutgoingContext(ctx,
		"x-source-member", cfg.WorkerID,
		"x-source-generation", strconv.FormatUint(cfg.Generation, 10),
		"authorization", cfg.Credential,
	)
	started, err := tunnelServer.Serve(sessionCtx)
	log.Printf("tunnel session finished worker=%s started=%v error=%v", cfg.WorkerID, started, err)
	if !started && err != nil {
		return err
	}
	return err
}

// waitReady blocks until the client connection reports Ready, the deadline
// passes, or the connection enters a terminal state. NewClient is lazy, so
// this is what turns an unreachable endpoint into a prompt error rather than a
// stall on first use.
func waitReady(ctx context.Context, conn *grpc.ClientConn, target string) error {
	dialCtx, cancel := context.WithTimeout(ctx, dialTimeout)
	defer cancel()
	conn.Connect()
	for {
		state := conn.GetState()
		switch state {
		case connectivity.Ready:
			return nil
		case connectivity.TransientFailure, connectivity.Shutdown:
			return fmt.Errorf("dial %s: connection not ready", target)
		}
		if !conn.WaitForStateChange(dialCtx, state) {
			return fmt.Errorf("dial %s: %w", target, dialCtx.Err())
		}
	}
}
