package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/jhump/grpctunnel"
	"github.com/jhump/grpctunnel/tunnelpb"

	"oicm-litellm-layer/mcp-source-link/dispatchpb"
)

const (
	// tunnelProbeTimeout bounds one Probe RPC, matching the reverse-h2 probe.
	tunnelProbeTimeout = 2 * time.Second
	// tunnelAdmitTimeout bounds the capability handshake pushed down a
	// freshly opened tunnel.
	tunnelAdmitTimeout = 10 * time.Second
	// tunnelResponseDeadline is the per-Execute deadline, identical to the
	// reverse-h2 dispatch deadline.
	tunnelResponseDeadline = 90 * time.Second
	// tunnelMaxCatalog caps the catalog bytes a Probe response may carry,
	// matching the reverse-h2 probe read limit.
	tunnelMaxCatalog = 600000
)

// tunnelMember is one live connector tunnel: a grpctunnel channel plus the
// capability set its connector advertised at admission.
type tunnelMember struct {
	stub         dispatchpb.DispatchServiceClient
	channel      grpctunnel.TunnelChannel
	workerID     string
	sourceID     string
	generation   uint64
	capabilities map[string]struct{}
	closeOnce    sync.Once
	closed       chan struct{}
	mu           sync.Mutex
	lastProbe    time.Time
}

func (m *tunnelMember) markClosed() {
	m.closeOnce.Do(func() { close(m.closed) })
}

func (m *tunnelMember) id() string     { return m.workerID }
func (m *tunnelMember) source() string { return m.sourceID }

func (m *tunnelMember) ready(sourceID string) bool {
	select {
	case <-m.closed:
		return false
	default:
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return !m.lastProbe.IsZero() && time.Since(m.lastProbe) < 2*reverseProbeInterval && m.sourceID == sourceID
}

func (m *tunnelMember) capable(rpc string) bool {
	_, ok := m.capabilities[rpc]
	return ok
}

func (m *tunnelMember) fenced(worker peer, exists bool) bool {
	return !exists || worker.Generation != m.generation || worker.Phase == "revoked"
}

// close ends the tunnel, cancelling every in-flight Execute. It is the
// revoke/generation-fencing action, identical in effect to session.close.
func (m *tunnelMember) close() {
	m.markClosed()
	m.channel.Close()
}

// roundTrip performs one Execute RPC down the tunnel. The per-RPC context
// deadline carries caller cancellation natively.
func (m *tunnelMember) roundTrip(req *http.Request) (*http.Response, error) {
	select {
	case <-m.closed:
		return nil, errReverseClosed
	default:
	}
	var body []byte
	if req.Body != nil {
		read, err := io.ReadAll(io.LimitReader(req.Body, reverseMaxOriginBody+1))
		if err != nil {
			return nil, err
		}
		if len(read) > reverseMaxOriginBody {
			return nil, errors.New("reverse dispatch request exceeds the body limit")
		}
		body = read
		_ = req.Body.Close()
	}
	headers := make([]*dispatchpb.Header, 0, len(req.Header))
	for name, values := range req.Header {
		for _, value := range values {
			headers = append(headers, &dispatchpb.Header{Name: name, Value: value})
		}
	}
	request := &dispatchpb.DispatchRequest{
		Method:  req.Method,
		Path:    req.URL.Path,
		Query:   req.URL.RawQuery,
		Headers: headers,
		Body:    body,
	}
	ctx, cancel := context.WithTimeout(req.Context(), tunnelResponseDeadline)
	defer cancel()
	resp, err := m.stub.Execute(ctx, request)
	if err != nil {
		return nil, err
	}
	header := make(http.Header)
	for _, h := range resp.Headers {
		header.Add(h.Name, h.Value)
	}
	for _, name := range []string{"Content-Length", "Transfer-Encoding", "Connection", "Keep-Alive", "Trailer"} {
		header.Del(name)
	}
	return &http.Response{
		Status:        strconv.Itoa(int(resp.Status)),
		StatusCode:    int(resp.Status),
		Header:        header,
		Body:          io.NopCloser(bytes.NewReader(resp.Body)),
		ContentLength: int64(len(resp.Body)),
	}, nil
}

// probe pushes one Probe RPC down the tunnel and verifies the returned
// catalog proof with the shared verification logic. The nonce is generated
// here, so a captured response cannot be replayed for a later probe.
func (m *tunnelMember) probe(worker peer) (string, error) {
	nonce := make([]byte, 32)
	if _, err := rand.Read(nonce); err != nil {
		return "", err
	}
	nonceText := base64.RawURLEncoding.EncodeToString(nonce)
	ctx, cancel := context.WithTimeout(context.Background(), tunnelProbeTimeout)
	defer cancel()
	resp, err := m.stub.Probe(ctx, &dispatchpb.ProbeRequest{Nonce: nonceText})
	if err != nil {
		return "", err
	}
	if len(resp.CatalogJson) > tunnelMaxCatalog {
		return "", errors.New("probe catalog exceeds the size limit")
	}
	return verifyCatalog(worker, nonceText, resp.CatalogJson, resp.ProofSignature)
}

// tunnelRuntime owns the grpctunnel handler and the worker-indexed members.
type tunnelRuntime struct {
	runtime *runtime
	handler *grpctunnel.TunnelServiceHandler
	mu      sync.Mutex
	members map[string]*tunnelMember
}

func newTunnelRuntime(r *runtime) *tunnelRuntime {
	tr := &tunnelRuntime{runtime: r, members: map[string]*tunnelMember{}}
	tr.handler = grpctunnel.NewTunnelServiceHandler(grpctunnel.TunnelServiceHandlerOptions{
		OnReverseTunnelOpen:  func(ch grpctunnel.TunnelChannel) { tr.onOpen(ch) },
		OnReverseTunnelClose: func(ch grpctunnel.TunnelChannel) { tr.forget(ch) },
	})
	return tr
}

// grpcServer returns the gRPC server with the tunnel service registered,
// ready to mount on the h2c reverse listener via ServeHTTP.
func (tr *tunnelRuntime) grpcServer() *grpc.Server {
	server := grpc.NewServer(
		grpc.StreamInterceptor(tr.authStreamInterceptor),
	)
	tunnelpb.RegisterTunnelServiceServer(server, tr.handler.Service())
	return server
}

// authStreamInterceptor validates connector identity on the tunnel-opening
// stream before the tunnel establishes; an invalid credential fails the
// stream with Unauthenticated and no tunnel is created.
func (tr *tunnelRuntime) authStreamInterceptor(srv any, ss grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
	if info.FullMethod != tunnelpb.TunnelService_OpenReverseTunnel_FullMethodName {
		return handler(srv, ss)
	}
	md, _ := metadata.FromIncomingContext(ss.Context())
	member := firstMetadata(md, "x-source-member")
	credential := firstMetadata(md, "authorization")
	generation, err := strconv.ParseUint(firstMetadata(md, "x-source-generation"), 10, 64)
	if err != nil {
		return status.Error(codes.Unauthenticated, "connector credentials required")
	}
	r := tr.runtime
	r.mu.Lock()
	existing := r.sessions[member]
	r.mu.Unlock()
	worker, ok := r.state.peer(member)
	if !ok || worker.Phase != "active" || worker.Generation != generation || existing != nil {
		return status.Error(codes.Unauthenticated, "connector unauthorized")
	}
	if !sameSecret(worker.CredentialDigest, credential) {
		return status.Error(codes.Unauthenticated, "connector unauthorized")
	}
	return handler(srv, ss)
}

// admit performs the capability handshake on a freshly opened tunnel. The
// recvLoop goroutine is already pumping, so the pushed RPC is answerable.
// It runs from OnReverseTunnelOpen, before the tunnel enters the pool.
func (tr *tunnelRuntime) admit(ch grpctunnel.TunnelChannel) (*tunnelMember, error) {
	md, _ := metadata.FromIncomingContext(ch.Context())
	memberID := firstMetadata(md, "x-source-member")
	generation, err := strconv.ParseUint(firstMetadata(md, "x-source-generation"), 10, 64)
	if err != nil {
		return nil, err
	}
	worker, ok := tr.runtime.state.peer(memberID)
	if !ok {
		return nil, errors.New("worker state missing")
	}
	ctx, cancel := context.WithTimeout(context.Background(), tunnelAdmitTimeout)
	defer cancel()
	stub := dispatchpb.NewDispatchServiceClient(ch)
	resp, err := stub.Admit(ctx, &dispatchpb.AdmitRequest{BrokerCapabilities: []string{"execute", "probe"}})
	if err != nil {
		return nil, err
	}
	capabilities := make(map[string]struct{}, len(resp.Capabilities))
	for _, capability := range resp.Capabilities {
		capabilities[capability] = struct{}{}
	}
	return &tunnelMember{
		stub: stub, channel: ch, workerID: worker.WorkerID, sourceID: worker.SourceID,
		generation: generation, capabilities: capabilities, closed: make(chan struct{}),
	}, nil
}

// OnReverseTunnelOpen callback: validate, admit, register, and start the
// probe loop. A member is only visible to the pool after admission succeeds.
func (tr *tunnelRuntime) onOpen(ch grpctunnel.TunnelChannel) {
	member, err := tr.admit(ch)
	if err != nil {
		log.Printf("tunnel admission rejected: %v", err)
		ch.Close()
		return
	}
	tr.mu.Lock()
	if _, exists := tr.members[member.workerID]; exists {
		tr.mu.Unlock()
		ch.Close()
		return
	}
	tr.members[member.workerID] = member
	tr.mu.Unlock()
	tr.runtime.addSession(member)
	go tr.probeLoop(member)
}

// forget drops a tunnel from the member map when its channel closes.
func (tr *tunnelRuntime) forget(ch grpctunnel.TunnelChannel) {
	tr.mu.Lock()
	var member *tunnelMember
	for id, m := range tr.members {
		if m.channel == ch {
			member = m
			delete(tr.members, id)
			break
		}
	}
	tr.mu.Unlock()
	if member != nil {
		member.markClosed()
		tr.runtime.removeSession(member)
	}
}

// probeLoop is the shared per-member probe cycle: probe, report health,
// and gate on the control state's generation and phase.
func (tr *tunnelRuntime) probeLoop(m *tunnelMember) {
	if !m.capable("probe") {
		return
	}
	for {
		select {
		case <-m.closed:
			return
		case <-time.After(2 * time.Second):
		}
		worker, ok := tr.runtime.state.peer(m.id())
		if m.fenced(worker, ok) {
			m.close()
			return
		}
		if worker.Phase != "active" {
			continue
		}
		fingerprint, err := m.probe(worker)
		if err != nil || fingerprint != worker.ExpectedCatalog {
			fingerprint = ""
		}
		report := healthReport{WorkerID: m.id(), Generation: m.generation}
		if fingerprint != "" {
			report.CatalogFingerprint = &fingerprint
		}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		admitted, err := tr.runtime.state.report(ctx, report)
		cancel()
		if err == nil && admitted && fingerprint != "" {
			m.mu.Lock()
			m.lastProbe = time.Now()
			m.mu.Unlock()
		}
	}
}

// closeAll ends every tunnel; the shutdown path.
func (tr *tunnelRuntime) closeAll() {
	tr.mu.Lock()
	members := make([]*tunnelMember, 0, len(tr.members))
	for _, m := range tr.members {
		members = append(members, m)
	}
	tr.mu.Unlock()
	for _, m := range members {
		m.close()
	}
}

// firstMetadata returns the first value of a metadata key, or "".
func firstMetadata(md metadata.MD, key string) string {
	values := md.Get(key)
	if len(values) == 0 {
		return ""
	}
	return values[0]
}

// tunnelHTTPHandler multiplexes gRPC-over-h2c with the existing reverse-h2
// routes on the 8093 listener: tunnel RPCs to the gRPC server, everything
// else to the reverse mux.
func tunnelHTTPHandler(grpcServer *grpc.Server, reverse *http.ServeMux) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.ProtoMajor == 2 && strings.HasPrefix(req.Header.Get("Content-Type"), "application/grpc") {
			grpcServer.ServeHTTP(w, req)
			return
		}
		reverse.ServeHTTP(w, req)
	})
}
