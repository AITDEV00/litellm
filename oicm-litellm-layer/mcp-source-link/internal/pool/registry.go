package pool

import (
	"sync"

	"oicm-litellm-layer/mcp-source-link/internal/control"
)

// DispatchInFlightCap bounds how many requests one member may hold in flight
// at once. It is 0 (unbounded) because the grpctunnel plane multiplexes every
// dispatch inside one HTTP/2 stream, so the transport has no per-request
// resource beyond a goroutine and a 64KB flow-control window, and the nginx
// body cap that used to kill long-lived tunnels has been lifted.
const DispatchInFlightCap = 0

// Runtime is the session registry and the routing state for it.
type Runtime struct {
	state    *control.Client
	mu       sync.Mutex
	sessions map[string]Member
	serial   uint64
	last     map[string]uint64
	inFlight map[string]int
}

// New builds an empty registry over the given control-plane view.
func New(state *control.Client) *Runtime {
	return &Runtime{state: state, sessions: make(map[string]Member), last: make(map[string]uint64), inFlight: make(map[string]int)}
}

// State exposes the control-plane view for the plane packages.
func (r *Runtime) State() *control.Client { return r.state }

// AddSession registers a member under its worker ID.
func (r *Runtime) AddSession(m Member) {
	r.mu.Lock()
	r.sessions[m.ID()] = m
	r.mu.Unlock()
}

// RemoveSession drops a member if it is still the registered one.
func (r *Runtime) RemoveSession(m Member) {
	r.mu.Lock()
	if r.sessions[m.ID()] == m {
		delete(r.sessions, m.ID())
	}
	r.mu.Unlock()
}

// Existing returns the registered member for a worker, if any.
func (r *Runtime) Existing(workerID string) Member {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.sessions[workerID]
}

// Sessions returns a snapshot of the registered members.
func (r *Runtime) Sessions() []Member {
	r.mu.Lock()
	defer r.mu.Unlock()
	members := make([]Member, 0, len(r.sessions))
	for _, session := range r.sessions {
		members = append(members, session)
	}
	return members
}

// CloseAllSessions ends every session; the shutdown path.
func (r *Runtime) CloseAllSessions() {
	r.mu.Lock()
	sessions := make([]Member, 0, len(r.sessions))
	for _, session := range r.sessions {
		sessions = append(sessions, session)
	}
	r.mu.Unlock()
	for _, session := range sessions {
		session.Close()
	}
}

// Reconcile closes sessions whose peer vanished or was fenced.
func (r *Runtime) Reconcile() {
	r.mu.Lock()
	closed := make([]Member, 0, len(r.sessions))
	for id, session := range r.sessions {
		worker, ok := r.state.Peer(id)
		if !ok || session.Fenced(worker, ok) {
			closed = append(closed, session)
		}
	}
	r.mu.Unlock()
	for _, session := range closed {
		session.Close()
	}
}

// Revoke closes the named session when it still belongs to sourceID.
func (r *Runtime) Revoke(sourceID, workerID string) {
	r.mu.Lock()
	session := r.sessions[workerID]
	r.mu.Unlock()
	if session != nil && session.Source() == sourceID {
		session.Close()
	}
}
