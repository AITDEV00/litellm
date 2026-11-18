// Package reverseh2 owns the long-poll data plane. A connector registers,
// holds a stream open, and pulls work; the broker queues a request, the
// connector executes it against its origin, and posts the response back
// correlated by ID.
package reverseh2

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/wire"
)

// ErrSessionClosed reports that the session ended mid-dispatch.
var ErrSessionClosed = errors.New("reverse session closed")

// ErrSessionExists reports a duplicate registration for a live worker,
// distinct from a credential failure so the operator sees the real cause
// instead of a misleading 401.
var ErrSessionExists = errors.New("reverse session exists")

// ResponseDeadline bounds how long a dispatch may wait for its response.
const ResponseDeadline = wire.ResponseDeadlineSeconds * time.Second

// ProbeInterval is how often the hold loop re-proves the origin catalog.
const ProbeInterval = 15 * time.Second

// Session is one connector's long-poll registration.
type Session struct {
	workerID   string
	sourceID   string
	generation uint64
	deadline   time.Duration
	closeOnce  sync.Once
	closed     chan struct{}
	onClose    func()
	mu         sync.Mutex
	pending    chan wire.DispatchRequest
	inflight   map[string]chan wire.DispatchResponse
	lastProbe  time.Time
}

// NewSession builds a registrable session for an already-authenticated worker.
func NewSession(worker config.Peer, deadline time.Duration) *Session {
	return &Session{
		workerID: worker.WorkerID, sourceID: worker.SourceID, generation: worker.Generation, deadline: deadline,
		closed: make(chan struct{}), pending: make(chan wire.DispatchRequest, 128), inflight: make(map[string]chan wire.DispatchResponse),
	}
}

// SetOnClose registers the registry callback, which must run exactly once.
func (s *Session) SetOnClose(fn func()) { s.onClose = fn }

// ID implements pool.Member.
func (s *Session) ID() string { return s.workerID }

// Source implements pool.Member.
func (s *Session) Source() string { return s.sourceID }

// Capable implements pool.Member. The long-poll plane carries every RPC.
func (s *Session) Capable(string) bool { return true }

// Fenced reports whether the control plane has moved past this session.
func (s *Session) Fenced(worker config.Peer, exists bool) bool {
	return !exists || worker.Generation != s.generation || worker.Phase == "revoked"
}

// Generation is the worker generation this session was opened for.
func (s *Session) Generation() uint64 { return s.generation }

// Closed is the channel closed when the session ends.
func (s *Session) Closed() <-chan struct{} { return s.closed }

// MarkProbed records a successful catalog proof.
func (s *Session) MarkProbed() {
	s.mu.Lock()
	s.lastProbe = time.Now()
	s.mu.Unlock()
}

// Ready implements pool.Member by requiring a recent successful probe.
func (s *Session) Ready(sourceID string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return !s.lastProbe.IsZero() && time.Since(s.lastProbe) < 2*ProbeInterval && s.sourceID == sourceID
}

// Close implements pool.Member. It fails every waiter so no caller blocks
// on a session that will never answer.
func (s *Session) Close() {
	s.closeOnce.Do(func() {
		close(s.closed)
		s.mu.Lock()
		inflight := s.inflight
		s.inflight = nil
		s.mu.Unlock()
		for _, waiter := range inflight {
			select {
			case waiter <- wire.DispatchResponse{Error: "reverse session closed"}:
			default:
			}
		}
		if s.onClose != nil {
			s.onClose()
		}
	})
}

var serial atomic.Uint64

// RoundTrip implements pool.Member: it serializes the request, queues it for
// the connector, and blocks for the correlated response.
func (s *Session) RoundTrip(req *http.Request) (*http.Response, error) {
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
	headers := make(map[string][]string, len(req.Header))
	for name, values := range req.Header {
		headers[name] = values
	}
	item := wire.DispatchRequest{
		ID:      fmt.Sprintf("%d-%d", time.Now().UnixNano(), serial.Add(1)),
		Method:  req.Method,
		Path:    req.URL.Path,
		Query:   req.URL.RawQuery,
		Headers: headers,
		Body:    body,
	}
	waiter := make(chan wire.DispatchResponse, 1)
	s.mu.Lock()
	select {
	case <-s.closed:
		s.mu.Unlock()
		return nil, ErrSessionClosed
	default:
	}
	s.inflight[item.ID] = waiter
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		delete(s.inflight, item.ID)
		s.mu.Unlock()
	}()
	select {
	case <-s.closed:
		return nil, ErrSessionClosed
	case <-req.Context().Done():
		return nil, req.Context().Err()
	case s.pending <- item:
	default:
		return nil, errors.New("reverse dispatch queue saturated")
	}
	select {
	case <-s.closed:
		return nil, ErrSessionClosed
	case <-req.Context().Done():
		return nil, req.Context().Err()
	case resp := <-waiter:
		return resp.HTTPResponse()
	case <-time.After(s.deadline):
		return nil, errors.New("reverse dispatch exceeded its deadline")
	}
}

// Take hands the next queued request to a connector dispatch stream.
func (s *Session) Take(ctx context.Context) (wire.DispatchRequest, error) {
	select {
	case <-ctx.Done():
		return wire.DispatchRequest{}, ctx.Err()
	case <-s.closed:
		return wire.DispatchRequest{}, ErrSessionClosed
	case item := <-s.pending:
		return item, nil
	}
}

// Deliver correlates a connector response with its waiting request.
func (s *Session) Deliver(resp wire.DispatchResponse) bool {
	s.mu.Lock()
	waiter := s.inflight[resp.ID]
	if waiter != nil {
		delete(s.inflight, resp.ID)
	}
	s.mu.Unlock()
	if waiter == nil {
		return false
	}
	waiter <- resp
	return true
}
