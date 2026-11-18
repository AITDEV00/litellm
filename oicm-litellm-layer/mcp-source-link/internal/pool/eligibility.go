// Package pool owns which connector serves a request. It holds the session
// registry, the eligibility predicate every routing decision shares, and the
// reverse proxy that carries a gateway request to the chosen member.
//
// Members are opaque here: the grpctunnel plane supplies the only Member
// implementation, and pool never learns how a member is built.
package pool

import (
	"net/http"

	"oicm-litellm-layer/mcp-source-link/internal/config"
)

// Member is one live connector session, whichever transport plane owns it.
type Member interface {
	ID() string
	Source() string
	Ready(sourceID string) bool
	Capable(rpc string) bool
	Fenced(worker config.Peer, exists bool) bool
	RoundTrip(req *http.Request) (*http.Response, error)
	Close()
}

// RoundTripFunc adapts a function to http.RoundTripper.
type RoundTripFunc func(*http.Request) (*http.Response, error)

// RoundTrip calls f.
func (f RoundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

// eligible is the single eligibility predicate. Every routing decision reads
// this one function, so "which members may serve this source" can only change
// in one place. It was previously copy-pasted across pick, hasActive, and
// eligibleMembers, where the copies could drift.
func (r *Runtime) eligible(sourceID string, exclude string) []Member {
	r.mu.Lock()
	defer r.mu.Unlock()
	members := make([]Member, 0, len(r.sessions))
	for id, session := range r.sessions {
		worker, ok := r.state.Peer(id)
		if worker.WorkerID == exclude {
			continue
		}
		if !admitted(session, worker, ok) || worker.SourceID != sourceID {
			continue
		}
		members = append(members, session)
	}
	return members
}

// admitted reports whether a session may currently serve executed calls:
// the peer still exists, is active, is not fenced by a generation change or
// revocation, has a catalog fingerprint matching what the control plane
// expects, has probed recently, and advertises the execute capability.
func admitted(session Member, worker config.Peer, exists bool) bool {
	return exists &&
		worker.Phase == "active" &&
		!session.Fenced(worker, exists) &&
		worker.CatalogFingerprint != nil &&
		*worker.CatalogFingerprint == worker.ExpectedCatalog &&
		session.Ready(worker.SourceID) &&
		session.Capable("execute")
}
