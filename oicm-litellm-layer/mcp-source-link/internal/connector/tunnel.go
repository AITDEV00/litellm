package connector

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math/rand/v2"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
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
var Capabilities = []string{"execute", "execute_stream", "probe"}

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
	kind            string
	path            string
	roundTripErrors atomic.Uint64
	readErrors      atomic.Uint64
}

// NewServer builds a dispatch server over the given origin transport.
//
// authority is the Host header the origin should receive. It is the target's own
// host:port (an IP literal, or a Kubernetes service DNS name), so an origin behind a
// service router sees the name it is deployed under rather than an unrelated
// placeholder, and an MCP origin that validates its Host header for DNS-rebinding
// protection sees the port its allowlist expects. kind and path identify what the
// origin serves: an OpenAPI source exposes its spec at /openapi.json, while an MCP
// source serves JSON-RPC at its configured path.
func NewServer(origin http.RoundTripper, authority, kind, path string) *Server {
	return &Server{origin: origin, authority: authority, kind: kind, path: path}
}

// Admit answers the capability handshake.
func (d *Server) Admit(_ context.Context, _ *dispatchpb.AdmitRequest) (*dispatchpb.AdmitResponse, error) {
	return &dispatchpb.AdmitResponse{Capabilities: Capabilities}, nil
}

// Probe answers a catalog probe. For an OpenAPI source it fetches the origin's
// /openapi.json and returns the document for the broker to fingerprint. For an
// MCP source there is no OpenAPI document: the probe reaches the MCP endpoint to
// confirm the origin is up, and returns an empty catalog so the broker records
// health without a fingerprint.
//
// The nonce is echoed to the origin for an origin that still signs, but a missing
// signature is no longer an error: an unsigned catalog is the norm for a local
// source. The broker records the catalog digest as the fingerprint either way.
func (d *Server) Probe(ctx context.Context, req *dispatchpb.ProbeRequest) (*dispatchpb.ProbeResponse, error) {
	if d.kind == "mcp" {
		return d.mcpProbe(ctx)
	}
	target := url.URL{Scheme: "http", Host: d.authority, Path: d.probePath()}
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
	signature, _ := base64.StdEncoding.DecodeString(resp.Header.Get("X-Source-Signature"))
	return &dispatchpb.ProbeResponse{CatalogJson: contents, ProofSignature: signature}, nil
}

// probePath is the origin path the probe dials for this source's kind.
func (d *Server) probePath() string {
	if d.kind == "mcp" {
		if d.path != "" {
			return d.path
		}
		return "/mcp"
	}
	return "/openapi.json"
}

// mcpProbe performs a bounded MCP `initialize` handshake and accepts only a
// valid JSON-RPC result as proof the origin speaks MCP.
//
// The previous probe treated any HTTP status below 500 as reachable, so a 401,
// 403, a redirect, or an unrelated service answering HTML with 200 all counted
// as a healthy MCP backend, even though none of them can serve a tool call. A
// real initialize is the smallest request that proves the endpoint is the MCP
// server it claims to be and that it authorizes this connector.
//
// The response may be a single JSON body or a text/event-stream whose message
// event carries the JSON-RPC reply; both are the Streamable HTTP transport's
// documented shapes. A 3xx is refused rather than followed: d.origin is a bare
// RoundTripper, so following a redirect would dial a host the operator never
// approved, exactly the private-to-public hop the target policy forbids.
func (d *Server) mcpProbe(ctx context.Context) (*dispatchpb.ProbeResponse, error) {
	request := []byte(
		`{"jsonrpc":"2.0","id":"source-link-probe","method":"initialize",` +
			`"params":{"protocolVersion":"2025-06-18","capabilities":{},` +
			`"clientInfo":{"name":"source-link-probe","version":"1"}}}`,
	)
	target := url.URL{Scheme: "http", Host: d.authority, Path: d.probePath()}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, target.String(), bytes.NewReader(request))
	if err != nil {
		return nil, err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "application/json, text/event-stream")
	resp, err := d.origin.RoundTrip(httpReq)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= http.StatusMultipleChoices && resp.StatusCode < http.StatusBadRequest {
		return nil, fmt.Errorf("origin mcp probe refused a redirect: %d", resp.StatusCode)
	}
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		return nil, fmt.Errorf("origin mcp initialize failed: %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, tunnel.MaxCatalog+1))
	if err != nil {
		return nil, err
	}
	if len(body) > tunnel.MaxCatalog {
		return nil, errors.New("origin mcp initialize response exceeds the probe limit")
	}
	if !mcpInitializeSucceeded(resp.Header.Get("Content-Type"), body) {
		return nil, errors.New("origin mcp probe did not receive a JSON-RPC initialize result")
	}
	return &dispatchpb.ProbeResponse{}, nil
}

// mcpInitializeSucceeded reports whether the response body carries a JSON-RPC
// initialize result. It accepts a plain JSON body or an SSE body whose data
// lines form the JSON-RPC reply, and rejects HTML or any body without a result.
func mcpInitializeSucceeded(contentType string, body []byte) bool {
	payload := body
	if strings.HasPrefix(strings.ToLower(strings.TrimSpace(contentType)), "text/event-stream") {
		payload = sseData(body)
	}
	var reply struct {
		Result json.RawMessage `json:"result"`
		Error  json.RawMessage `json:"error"`
	}
	if err := json.Unmarshal(payload, &reply); err != nil {
		return false
	}
	return len(reply.Result) > 0 && len(reply.Error) == 0
}

// sseData concatenates the data lines of a server-sent-events body into the
// JSON payload MCP wraps its JSON-RPC message in.
func sseData(body []byte) []byte {
	var payload []byte
	for _, line := range strings.Split(string(body), "\n") {
		if strings.HasPrefix(line, "data:") {
			payload = append(payload, strings.TrimPrefix(line, "data:")...)
		}
	}
	return payload
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

// ExecuteStream runs one dispatched request against the origin and relays the
// origin response body to the caller as it arrives, instead of reading it whole
// first. The first message carries status and headers; each later message
// carries one body chunk. A streaming MCP client therefore sees bytes as the
// origin produces them, so an SSE or streamable-http response is not held until
// the origin closes.
func (d *Server) ExecuteStream(req *dispatchpb.DispatchRequest, stream dispatchpb.DispatchService_ExecuteStreamServer) error {
	if len(req.Body) > wire.MaxOriginBody {
		return status.Error(codes.ResourceExhausted, "request body exceeds the dispatch limit")
	}
	target := url.URL{Scheme: "http", Host: d.authority, Path: req.Path, RawQuery: req.Query}
	httpReq, err := http.NewRequestWithContext(stream.Context(), req.Method, target.String(), bytes.NewReader(req.Body))
	if err != nil {
		return err
	}
	for _, header := range req.Headers {
		httpReq.Header.Add(header.Name, header.Value)
	}
	resp, err := d.origin.RoundTrip(httpReq)
	if err != nil {
		logConnectorError("connector origin execute failure", &d.roundTripErrors, err)
		return err
	}
	defer resp.Body.Close()
	headers := make([]*dispatchpb.Header, 0, len(resp.Header))
	for name, values := range resp.Header {
		for _, value := range values {
			headers = append(headers, &dispatchpb.Header{Name: name, Value: value})
		}
	}
	if err := stream.Send(&dispatchpb.DispatchResponseChunk{Status: int32(resp.StatusCode), Headers: headers}); err != nil {
		return err
	}
	buffer := make([]byte, wire.StreamChunk)
	for {
		n, readErr := resp.Body.Read(buffer)
		if n > 0 {
			chunk := make([]byte, n)
			copy(chunk, buffer[:n])
			if err := stream.Send(&dispatchpb.DispatchResponseChunk{Body: chunk}); err != nil {
				return err
			}
		}
		if readErr == io.EOF {
			return nil
		}
		if readErr != nil {
			logConnectorError("connector origin response read failure", &d.readErrors, readErr)
			return readErr
		}
	}
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
	server := NewServer(origin, authority, cfg.Kind, cfg.Path)
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
