package main

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"sync"
	"time"

	"github.com/rancher/remotedialer"
)

var errSessionClosed = errors.New("remotedialer session closed")

type memberSession struct {
	key        string
	sourceID   string
	workerID   string
	generation uint64
	mu         sync.Mutex
	conn       net.Conn
	transport  *http.Transport
	closeOnce  sync.Once
	closed     chan struct{}
	onClose    func()
	serial     uint64
	lastProbe  time.Time
}

type transportWithSession struct {
	http.RoundTripper
	session *memberSession
}

func (t transportWithSession) RoundTrip(req *http.Request) (*http.Response, error) {
	select {
	case <-t.session.closed:
		return nil, errSessionClosed
	default:
	}
	return t.RoundTripper.RoundTrip(req)
}

func newMemberSession(relay *remotedialer.Server, worker peer, onClose func()) *memberSession {
	member := &memberSession{
		key: worker.WorkerID, sourceID: worker.SourceID, workerID: worker.WorkerID,
		generation: worker.Generation, closed: make(chan struct{}), onClose: onClose,
	}
	inner := &http.Transport{
		MaxIdleConns: 32, MaxIdleConnsPerHost: 16, IdleConnTimeout: 90 * time.Second,
		ResponseHeaderTimeout: 30 * time.Second, DisableCompression: true,
		DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			return relay.Dialer(member.key)(ctx, "tcp", slot(member.sourceID))
		},
	}
	member.transport = inner
	return member
}

func (m *memberSession) close() {
	m.closeOnce.Do(func() {
		close(m.closed)
		m.mu.Lock()
		conn := m.conn
		m.conn = nil
		m.mu.Unlock()
		if conn != nil {
			_ = conn.Close()
		}
		m.transport.CloseIdleConnections()
		m.onClose()
	})
}

type trackedUpgrade struct {
	http.ResponseWriter
	onHijack func(net.Conn) bool
}

func (w trackedUpgrade) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	conn, rw, err := http.NewResponseController(w.ResponseWriter).Hijack()
	if err == nil && !w.onHijack(conn) {
		_ = conn.Close()
		return nil, nil, errors.New("worker revoked before upgrade")
	}
	return conn, rw, err
}

type workerKey struct{}

func (r *runtime) revokeHTTP(w http.ResponseWriter, req *http.Request) {
	if !sameSecret(r.state.tokenDigest, bearer(req)) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	host, _, err := net.SplitHostPort(req.RemoteAddr)
	if err != nil || !net.ParseIP(host).IsLoopback() {
		http.Error(w, "loopback control required", http.StatusForbidden)
		return
	}
	memberID := req.Header.Get("X-Source-Member")
	if member, ok := r.state.peer(memberID); ok && member.SourceID != req.Header.Get("X-Source-Id") {
		http.Error(w, "source binding mismatch", http.StatusBadRequest)
		return
	}
	if err := r.state.refresh(req.Context()); err != nil {
		http.Error(w, "lifecycle state unavailable", http.StatusServiceUnavailable)
		return
	}
	r.revoke(req.Header.Get("X-Source-Id"), memberID)
	r.reconcile()
	w.WriteHeader(http.StatusNoContent)
}

func (r *runtime) reconcile() {
	r.mu.Lock()
	closed := make([]*memberSession, 0, len(r.sessions))
	for _, member := range r.sessions {
		worker, ok := r.state.peer(member.workerID)
		if !ok {
			closed = append(closed, member)
			continue
		}
		r.drained[member.workerID] = worker.Phase == "draining"
		if worker.Generation != member.generation || worker.Phase == "revoked" {
			closed = append(closed, member)
		}
	}
	r.mu.Unlock()
	for _, member := range closed {
		member.close()
	}
}

func (r *runtime) revoke(sourceID, workerID string) {
	r.mu.Lock()
	member := r.sessions[workerID]
	r.mu.Unlock()
	if member != nil && member.sourceID == sourceID {
		member.close()
	}
}

func (r *runtime) worker(w http.ResponseWriter, req *http.Request) {
	workerID, generation, credential := req.Header.Get("X-Source-Member"), req.Header.Get("X-Source-Generation"), bearer(req)
	if workerID == "" || generation == "" || credential == "" {
		http.Error(w, "connector credentials required", http.StatusUnauthorized)
		return
	}
	r.mu.Lock()
	worker, ok := r.state.peer(workerID)
	if !ok || worker.Phase != "active" || fmt.Sprint(worker.Generation) != generation ||
		!sameSecret(worker.CredentialDigest, credential) || r.sessions[workerID] != nil {
		r.mu.Unlock()
		http.Error(w, "connector unauthorized", http.StatusUnauthorized)
		return
	}
	var member *memberSession
	member = newMemberSession(r.relay, worker, func() { r.remove(member) })
	r.sessions[workerID] = member
	r.mu.Unlock()
	ctx, cancel := context.WithCancel(req.Context())
	defer cancel()
	defer member.close()
	go r.probeLoop(ctx, member)

	wrapped := trackedUpgrade{ResponseWriter: w, onHijack: func(conn net.Conn) bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		current, valid := r.state.peer(workerID)
		if r.sessions[workerID] != member || !valid || current.Phase != "active" || current.Generation != worker.Generation {
			return false
		}
		member.mu.Lock()
		member.conn = conn
		member.mu.Unlock()
		return true
	}}
	r.relay.ServeHTTP(wrapped, req.WithContext(context.WithValue(req.Context(), workerKey{}, workerID)))
}

func (r *runtime) remove(member *memberSession) {
	r.mu.Lock()
	if r.sessions[member.workerID] == member {
		delete(r.sessions, member.workerID)
	}
	r.mu.Unlock()
}
