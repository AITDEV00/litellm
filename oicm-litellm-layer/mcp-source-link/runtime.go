package main

import (
	"net"
	"net/http"
)

func (r *runtime) addSession(m member) {
	r.mu.Lock()
	r.sessions[m.id()] = m
	r.mu.Unlock()
}

func (r *runtime) removeSession(m member) {
	r.mu.Lock()
	if r.sessions[m.id()] == m {
		delete(r.sessions, m.id())
	}
	r.mu.Unlock()
}

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
	closed := make([]member, 0, len(r.sessions))
	for id, session := range r.sessions {
		worker, ok := r.state.peer(id)
		if !ok {
			closed = append(closed, session)
			continue
		}
		if session.fenced(worker, ok) {
			closed = append(closed, session)
		}
	}
	r.mu.Unlock()
	for _, session := range closed {
		session.close()
	}
}

func (r *runtime) revoke(sourceID, workerID string) {
	r.mu.Lock()
	session := r.sessions[workerID]
	r.mu.Unlock()
	if session != nil && session.source() == sourceID {
		session.close()
	}
}

func (r *runtime) closeAllSessions() {
	r.mu.Lock()
	sessions := make([]member, 0, len(r.sessions))
	for _, session := range r.sessions {
		sessions = append(sessions, session)
	}
	r.mu.Unlock()
	for _, session := range sessions {
		session.close()
	}
}
