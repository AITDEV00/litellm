package main

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
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/jhump/grpctunnel"
	"github.com/jhump/grpctunnel/tunnelpb"

	"oicm-litellm-layer/mcp-source-link/dispatchpb"
)

// grpcDialTarget converts the public HTTPS gateway URL into the host:port form
// gRPC requires, applying 443 when the URL omits a port. Passing the raw URL to
// grpc.Dial leaves the scheme in the address, which the dialer cannot parse and
// every attempt dies on the dial timeout.
func grpcDialTarget(gateway string) (string, error) {
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

// connectorCapabilities is the capability set this connector build
// implements. The broker gates each pushed RPC on this list.
var connectorCapabilities = []string{"execute", "probe"}

// dispatchServer implements DispatchService on the connector: it answers
// the broker's pushed RPCs by round-tripping the connector's loopback
// origin.
type dispatchServer struct {
	dispatchpb.UnimplementedDispatchServiceServer
	origin *http.Transport
}

func (d *dispatchServer) Admit(_ context.Context, _ *dispatchpb.AdmitRequest) (*dispatchpb.AdmitResponse, error) {
	return &dispatchpb.AdmitResponse{Capabilities: connectorCapabilities}, nil
}

func (d *dispatchServer) Probe(ctx context.Context, req *dispatchpb.ProbeRequest) (*dispatchpb.ProbeResponse, error) {
	target := url.URL{Scheme: "http", Host: "target.source.invalid", Path: "/openapi.json"}
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
	catalog, err := io.ReadAll(io.LimitReader(resp.Body, tunnelMaxCatalog+1))
	if err != nil {
		return nil, err
	}
	if len(catalog) > tunnelMaxCatalog {
		return nil, errors.New("origin catalog exceeds the dispatch body limit")
	}
	signature, err := base64.StdEncoding.DecodeString(resp.Header.Get("X-Source-Signature"))
	if err != nil {
		return nil, errors.New("origin returned no proof signature")
	}
	return &dispatchpb.ProbeResponse{CatalogJson: catalog, ProofSignature: signature}, nil
}

func (d *dispatchServer) Execute(ctx context.Context, req *dispatchpb.DispatchRequest) (*dispatchpb.DispatchResponse, error) {
	if len(req.Body) > reverseMaxOriginBody {
		return nil, status.Error(codes.ResourceExhausted, "request body exceeds the dispatch limit")
	}
	target := url.URL{Scheme: "http", Host: "target.source.invalid", Path: req.Path, RawQuery: req.Query}
	httpReq, err := http.NewRequestWithContext(ctx, req.Method, target.String(), bytes.NewReader(req.Body))
	if err != nil {
		return nil, err
	}
	for _, header := range req.Headers {
		httpReq.Header.Add(header.Name, header.Value)
	}
	resp, err := d.origin.RoundTrip(httpReq)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, reverseMaxOriginBody+1))
	if err != nil {
		return nil, err
	}
	if len(body) > reverseMaxOriginBody {
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

// runTunnelConnector is the tunnel transport's reconnect loop: dial the
// broker with TLS, attach identity metadata, and serve the reverse tunnel.
func runTunnelConnector(ctx context.Context, cfg connectorConfig, gateway string, tlsConfig *tls.Config) error {
	dialTarget, err := grpcDialTarget(gateway)
	if err != nil {
		return err
	}
	creds := credentials.NewTLS(tlsConfig)
	local := &net.Dialer{Timeout: 4 * time.Second, KeepAlive: 30 * time.Second}
	origin := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return local.DialContext(ctx, "tcp", cfg.Target)
		},
		IdleConnTimeout: 90 * time.Second, DisableCompression: true, ResponseHeaderTimeout: 30 * time.Second,
	}
	defer origin.CloseIdleConnections()
	server := &dispatchServer{origin: origin}
	delay := 300 * time.Millisecond
	for ctx.Err() == nil {
		started := time.Now()
		err := tunnelSession(ctx, cfg, dialTarget, creds, server)
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

// tunnelSession opens one reverse tunnel and serves pushed RPCs on it
// until it drops. Identity metadata rides the tunnel-opening stream and is
// validated by the sidecar before the tunnel establishes.
func tunnelSession(ctx context.Context, cfg connectorConfig, target string, creds credentials.TransportCredentials, server *dispatchServer) error {
	dialCtx, dialCancel := context.WithTimeout(ctx, 15*time.Second)
	defer dialCancel()
	conn, err := grpc.DialContext(dialCtx, target,
		grpc.WithTransportCredentials(creds),
		grpc.WithBlock(),
	)
	if err != nil {
		return err
	}
	defer conn.Close()
	tunnelServer := grpctunnel.NewReverseTunnelServer(tunnelpb.NewTunnelServiceClient(conn))
	dispatchpb.RegisterDispatchServiceServer(tunnelServer, server)
	sessionCtx := metadata.AppendToOutgoingContext(ctx,
		"x-source-member", cfg.WorkerID,
		"x-source-generation", strconv.FormatUint(cfg.Generation, 10),
		"authorization", cfg.Credential,
	)
	started, err := tunnelServer.Serve(sessionCtx)
	if !started && err != nil {
		return err
	}
	return err
}
