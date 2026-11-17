package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
)

func (r *runtime) registerReverse(req *http.Request) (*reverseSession, error) {
	workerID := req.Header.Get("X-Source-Member")
	generation := req.Header.Get("X-Source-Generation")
	credential := bearer(req)
	if workerID == "" || generation == "" || credential == "" {
		return nil, errors.New("connector credentials required")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	worker, ok := r.state.peer(workerID)
	if !ok || worker.Phase != "active" || fmt.Sprint(worker.Generation) != generation ||
		!sameSecret(worker.CredentialDigest, credential) {
		return nil, errors.New("connector unauthorized")
	}
	if r.sessions[workerID] != nil {
		return nil, errReverseSessionExists
	}
	session := &reverseSession{
		workerID: worker.WorkerID, sourceID: worker.SourceID, generation: worker.Generation, deadline: reverseResponseDeadline,
		closed: make(chan struct{}), pending: make(chan dispatchRequest, 128), inflight: make(map[string]chan dispatchResponse),
	}
	session.onClose = func() { r.removeSession(session) }
	r.sessions[workerID] = session
	return session, nil
}

func (r *runtime) removeReverse(session *reverseSession) {
	r.mu.Lock()
	if r.sessions[session.workerID] == session {
		delete(r.sessions, session.workerID)
	}
	r.mu.Unlock()
}

func (r *runtime) reverseRegister(w http.ResponseWriter, req *http.Request) {
	session, err := r.registerReverse(req)
	if errors.Is(err, errReverseSessionExists) {
		http.Error(w, "reverse session already exists for this connector", http.StatusConflict)
		return
	}
	if err != nil {
		http.Error(w, "reverse connector unauthorized", http.StatusUnauthorized)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(`{"worker_id":"` + session.workerID + `"}`))
}

// reverseDispatchAuth revalidates the per-worker credential and liveness
// of the session that owns this connector's streams.
func (r *runtime) reverseDispatchAuth(req *http.Request) *reverseSession {
	r.mu.Lock()
	m := r.sessions[req.Header.Get("X-Source-Member")]
	r.mu.Unlock()
	session, ok := m.(*reverseSession)
	if !ok {
		return nil
	}
	select {
	case <-session.closed:
		return nil
	default:
	}
	worker, ok := r.state.peer(session.workerID)
	if !ok || worker.Phase == "revoked" || worker.Generation != session.generation {
		return nil
	}
	if !sameSecret(worker.CredentialDigest, bearer(req)) {
		return nil
	}
	return session
}

func (r *runtime) reverseHold(w http.ResponseWriter, req *http.Request) {
	session := r.reverseDispatchAuth(req)
	if session == nil {
		http.Error(w, "reverse session required", http.StatusUnauthorized)
		return
	}
	ctx, cancel := context.WithCancel(req.Context())
	defer cancel()
	defer session.close()
	go r.reverseProbeLoop(ctx, session)
	select {
	case <-ctx.Done():
	case <-session.closed:
	}
}

func (r *runtime) reverseDispatch(w http.ResponseWriter, req *http.Request) {
	session := r.reverseDispatchAuth(req)
	if session == nil {
		http.Error(w, "reverse session required", http.StatusUnauthorized)
		return
	}
	item, err := session.take(req.Context())
	if err != nil {
		http.Error(w, "dispatch stream ended", http.StatusGone)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(item)
}

func (r *runtime) reverseRespond(w http.ResponseWriter, req *http.Request) {
	session := r.reverseDispatchAuth(req)
	if session == nil {
		http.Error(w, "reverse session required", http.StatusUnauthorized)
		return
	}
	var payload dispatchResponse
	decode := json.NewDecoder(io.LimitReader(req.Body, reverseMaxBody))
	if err := decode.Decode(&payload); err != nil {
		// Drain the rejected upload so the connector's body write can
		// finish and observe the status instead of wedging on flow
		// control with the response already refused.
		_, _ = io.Copy(io.Discard, io.LimitReader(req.Body, 2*reverseMaxBody))
		http.Error(w, "invalid dispatch response", http.StatusBadRequest)
		return
	}
	if !session.deliver(payload) {
		http.Error(w, "unknown dispatch response", http.StatusNotFound)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}
