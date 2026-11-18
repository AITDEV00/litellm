// Package tunnel owns the grpctunnel data plane. One long-lived reverse
// tunnel stream carries typed Admit, Probe, and Execute RPCs, multiplexed by
// jhump/grpctunnel so a connector needs a single outbound connection.
package tunnel

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"io"
	"log"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/jhump/grpctunnel"
	"github.com/jhump/grpctunnel/tunnelpb"

	"oicm-litellm-layer/mcp-source-link/dispatchpb"
	"oicm-litellm-layer/mcp-source-link/internal/catalog"
	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/control"
	"oicm-litellm-layer/mcp-source-link/internal/pool"
	"oicm-litellm-layer/mcp-source-link/internal/wire"
)

const (
	// ProbeTimeout bounds one Probe RPC.
	ProbeTimeout = 2 * time.Second
	// AdmitTimeout bounds the capability handshake pushed down a fresh tunnel.
	AdmitTimeout = 10 * time.Second
	// MaxCatalog caps the catalog bytes a Probe response may carry.
	MaxCatalog = wire.MaxCatalogBody
	// HealthGracePeriod is how long a last-verified catalog stays trusted
	// after a transient probe failure.
	HealthGracePeriod = 30 * time.Second
	// probePollCadence is how long the probe loop waits between rounds.
	probePollCadence = 2 * time.Second
)

// ResponseDeadline is the per-Execute deadline, taken from wire so the probe
// and dispatch paths cannot drift.
var ResponseDeadline = wire.ResponseDeadline

// Member is one live connector tunnel: a grpctunnel channel plus the
// capability set its connector advertised at admission.
type Member struct {
	stub         dispatchpb.DispatchServiceClient
	channel      grpctunnel.TunnelChannel
	workerID     string
	sourceID     string
	generation   uint64
	capabilities map[string]struct{}
	closeOnce    sync.Once
	closed       chan struct{}
	errorCount   atomic.Uint64
	mu           sync.Mutex
	lastProbe    time.Time
}

func (m *Member) markClosed() {
	m.closeOnce.Do(func() { close(m.closed) })
}

// ID implements pool.Member.
func (m *Member) ID() string { return m.workerID }

// Source implements pool.Member.
func (m *Member) Source() string { return m.sourceID }

// Ready implements pool.Member.
func (m *Member) Ready(sourceID string) bool {
	select {
	case <-m.closed:
		return false
	default:
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return !m.lastProbe.IsZero() && time.Since(m.lastProbe) < HealthGracePeriod && m.sourceID == sourceID
}

// Capable implements pool.Member.
func (m *Member) Capable(rpc string) bool {
	_, ok := m.capabilities[rpc]
	return ok
}

// capabilitiesList returns the admitted capability set in sorted order, so the
// admission log line is stable across runs.
func (m *Member) capabilitiesList() []string {
	names := make([]string, 0, len(m.capabilities))
	for name := range m.capabilities {
		names = append(names, name)
	}
	slices.Sort(names)
	return names
}

// Fenced implements pool.Member.
func (m *Member) Fenced(worker config.Peer, exists bool) bool {
	return !exists || worker.Generation != m.generation || worker.Phase == "revoked"
}

// Close implements pool.Member. It ends the tunnel and cancels in-flight
// Execute RPCs.
func (m *Member) Close() {
	m.markClosed()
	m.channel.Close()
}

// RoundTrip implements pool.Member with one Execute RPC down the tunnel. The
// per-RPC context deadline carries caller cancellation natively.
func (m *Member) RoundTrip(req *http.Request) (*http.Response, error) {
	select {
	case <-m.closed:
		return nil, ErrClosed
	default:
	}
	var body []byte
	if req.Body != nil {
		read, err := io.ReadAll(io.LimitReader(req.Body, wire.MaxOriginBody+1))
		if err != nil {
			return nil, err
		}
		if len(read) > wire.MaxOriginBody {
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
	ctx, cancel := context.WithTimeout(req.Context(), ResponseDeadline)
	defer cancel()
	resp, err := m.stub.Execute(ctx, request)
	if err != nil {
		count := m.errorCount.Add(1)
		if count <= 3 || count%1000 == 0 {
			log.Printf("tunnel execute failure worker=%s count=%d: %v", m.ID(), count, err)
		}
		return nil, err
	}
	header := make(http.Header)
	for _, h := range resp.Headers {
		header.Add(h.Name, h.Value)
	}
	for _, name := range wire.StripFramingHeaders {
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
// catalog proof. The nonce is generated here, so a captured response cannot
// be replayed for a later probe.
func (m *Member) probe(worker config.Peer) (string, error) {
	nonce := make([]byte, 32)
	if _, err := rand.Read(nonce); err != nil {
		return "", err
	}
	nonceText := base64.RawURLEncoding.EncodeToString(nonce)
	ctx, cancel := context.WithTimeout(context.Background(), ProbeTimeout)
	defer cancel()
	resp, err := m.stub.Probe(ctx, &dispatchpb.ProbeRequest{Nonce: nonceText})
	if err != nil {
		return "", err
	}
	if len(resp.CatalogJson) > MaxCatalog {
		return "", errors.New("probe catalog exceeds the size limit")
	}
	return catalog.Verify(worker, nonceText, resp.CatalogJson, resp.ProofSignature)
}

// ErrClosed reports that the tunnel ended before the call completed.
var ErrClosed = errors.New("reverse session closed")

// Runtime owns the grpctunnel handler and the worker-indexed members.
type Runtime struct {
	pool    *pool.Runtime
	handler *grpctunnel.TunnelServiceHandler
	mu      sync.Mutex
	members map[string]*Member
}

// New builds the tunnel runtime over the shared session registry.
func New(p *pool.Runtime) *Runtime {
	tr := &Runtime{pool: p, members: map[string]*Member{}}
	tr.handler = grpctunnel.NewTunnelServiceHandler(grpctunnel.TunnelServiceHandlerOptions{
		OnReverseTunnelOpen: func(ch grpctunnel.TunnelChannel) { tr.onOpen(ch) },
		OnReverseTunnelClose: func(ch grpctunnel.TunnelChannel) {
			if err := ch.Err(); err != nil {
				log.Printf("reverse tunnel closed with error: %v", err)
			}
			tr.forget(ch)
		},
	})
	return tr
}

// GRPCServer returns the gRPC server with the tunnel service registered,
// ready to mount on the h2c reverse listener via ServeHTTP.
func (tr *Runtime) GRPCServer() *grpc.Server {
	server := grpc.NewServer(grpc.StreamInterceptor(tr.authStreamInterceptor))
	tunnelpb.RegisterTunnelServiceServer(server, tr.handler.Service())
	return server
}

// authStreamInterceptor validates connector identity on the tunnel-opening
// stream before the tunnel establishes.
func (tr *Runtime) authStreamInterceptor(srv any, ss grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
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
	worker, ok := tr.pool.State().Peer(member)
	if !ok || worker.Phase != "active" || worker.Generation != generation || tr.pool.Existing(member) != nil {
		return status.Error(codes.Unauthenticated, "connector unauthorized")
	}
	if !control.SameSecret(worker.CredentialDigest, credential) {
		return status.Error(codes.Unauthenticated, "connector unauthorized")
	}
	return handler(srv, ss)
}

// admit performs the capability handshake on a freshly opened tunnel. The
// recvLoop goroutine is already pumping, so the pushed RPC is answerable.
func (tr *Runtime) admit(ch grpctunnel.TunnelChannel) (*Member, error) {
	md, _ := metadata.FromIncomingContext(ch.Context())
	memberID := firstMetadata(md, "x-source-member")
	generation, err := strconv.ParseUint(firstMetadata(md, "x-source-generation"), 10, 64)
	if err != nil {
		return nil, err
	}
	worker, ok := tr.pool.State().Peer(memberID)
	if !ok {
		return nil, errors.New("worker state missing")
	}
	ctx, cancel := context.WithTimeout(context.Background(), AdmitTimeout)
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
	return &Member{
		stub: stub, channel: ch, workerID: worker.WorkerID, sourceID: worker.SourceID,
		generation: generation, capabilities: capabilities, closed: make(chan struct{}),
	}, nil
}

// onOpen validates, admits, registers, and starts the probe loop. A member
// becomes visible to the pool only after admission succeeds.
func (tr *Runtime) onOpen(ch grpctunnel.TunnelChannel) {
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
	log.Printf("tunnel admitted worker=%s source=%s generation=%d capabilities=%v",
		member.workerID, member.sourceID, member.generation, member.capabilitiesList())
	tr.pool.AddSession(member)
	go tr.probeLoop(member)
}

// forget drops a tunnel from the member map when its channel closes.
func (tr *Runtime) forget(ch grpctunnel.TunnelChannel) {
	tr.mu.Lock()
	var member *Member
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
		tr.pool.RemoveSession(member)
	}
}

// probeLoop is the per-member probe cycle: probe, report health, and gate on
// the control state's generation and phase.
func (tr *Runtime) probeLoop(m *Member) {
	if !m.Capable("probe") {
		return
	}
	for {
		select {
		case <-m.closed:
			return
		case <-time.After(probePollCadence):
		}
		worker, ok := tr.pool.State().Peer(m.ID())
		if m.Fenced(worker, ok) {
			m.Close()
			return
		}
		if worker.Phase != "active" {
			continue
		}
		fingerprint, err := m.probe(worker)
		if err != nil {
			log.Printf("tunnel probe failed worker=%s: %v; retaining last verified catalog for %s", m.ID(), err, HealthGracePeriod)
			continue
		}
		report := config.HealthReport{WorkerID: m.ID(), Generation: m.generation}
		if fingerprint == worker.ExpectedCatalog {
			report.CatalogFingerprint = &fingerprint
		}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		admitted, err := tr.pool.State().Report(ctx, report)
		cancel()
		if err != nil {
			log.Printf("tunnel health report failed worker=%s: %v; retaining last verified catalog for %s", m.ID(), err, HealthGracePeriod)
			continue
		}
		m.mu.Lock()
		if admitted {
			m.lastProbe = time.Now()
		} else {
			m.lastProbe = time.Time{}
		}
		m.mu.Unlock()
	}
}

// CloseAll ends every tunnel; the shutdown path.
func (tr *Runtime) CloseAll() {
	tr.mu.Lock()
	members := make([]*Member, 0, len(tr.members))
	for _, m := range tr.members {
		members = append(members, m)
	}
	tr.mu.Unlock()
	for _, m := range members {
		m.Close()
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

// HTTPHandler serves gRPC-over-h2c on the reverse listener. Anything that is
// not a gRPC request is rejected: the tunnel is the only thing this listener
// speaks.
func HTTPHandler(grpcServer *grpc.Server) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.ProtoMajor == 2 && strings.HasPrefix(req.Header.Get("Content-Type"), "application/grpc") {
			grpcServer.ServeHTTP(w, req)
			return
		}
		http.Error(w, "this listener serves gRPC only", http.StatusNotFound)
	})
}
