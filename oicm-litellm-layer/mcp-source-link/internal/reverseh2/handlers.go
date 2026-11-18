package reverseh2

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"

	"oicm-litellm-layer/mcp-source-link/internal/catalog"
	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/control"
	"oicm-litellm-layer/mcp-source-link/internal/pool"
	"oicm-litellm-layer/mcp-source-link/internal/wire"
)

// Register authenticates the connector by header credential and generation,
// then creates and registers its session.
func (p *Plane) Register(req *http.Request) (*Session, error) {
	workerID := req.Header.Get("X-Source-Member")
	generation := req.Header.Get("X-Source-Generation")
	credential := control.Bearer(req)
	if workerID == "" || generation == "" || credential == "" {
		return nil, errors.New("connector credentials required")
	}
	worker, ok := p.pool.State().Peer(workerID)
	if !ok || worker.Phase != "active" || fmt.Sprint(worker.Generation) != generation ||
		!control.SameSecret(worker.CredentialDigest, credential) {
		return nil, errors.New("connector unauthorized")
	}
	if p.pool.Existing(workerID) != nil {
		return nil, ErrSessionExists
	}
	session := NewSession(worker, ResponseDeadline)
	session.SetOnClose(func() { p.pool.RemoveSession(session) })
	p.pool.AddSession(session)
	return session, nil
}

// RegisterHandler serves POST /mcp-source-link-reverse.
func (p *Plane) RegisterHandler(w http.ResponseWriter, req *http.Request) {
	session, err := p.Register(req)
	if errors.Is(err, ErrSessionExists) {
		http.Error(w, "reverse session already exists for this connector", http.StatusConflict)
		return
	}
	if err != nil {
		http.Error(w, "reverse connector unauthorized", http.StatusUnauthorized)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(`{"worker_id":"` + session.ID() + `"}`))
}

// authenticate revalidates the per-worker credential and liveness of the
// session that owns this connector's streams.
func (p *Plane) authenticate(req *http.Request) *Session {
	session, ok := p.pool.Existing(req.Header.Get("X-Source-Member")).(*Session)
	if !ok {
		return nil
	}
	select {
	case <-session.closed:
		return nil
	default:
	}
	worker, ok := p.pool.State().Peer(session.workerID)
	if !ok || worker.Phase == "revoked" || worker.Generation != session.generation {
		return nil
	}
	if !control.SameSecret(worker.CredentialDigest, control.Bearer(req)) {
		return nil
	}
	return session
}

// HoldHandler serves POST /mcp-source-link-hold: it keeps the session alive
// and runs the probe loop until the connector disconnects.
func (p *Plane) HoldHandler(w http.ResponseWriter, req *http.Request) {
	session := p.authenticate(req)
	if session == nil {
		http.Error(w, "reverse session required", http.StatusUnauthorized)
		return
	}
	ctx, cancel := context.WithCancel(req.Context())
	defer cancel()
	defer session.Close()
	go p.probeLoop(ctx, session)
	select {
	case <-ctx.Done():
	case <-session.closed:
	}
}

// DispatchHandler serves POST /mcp-source-link-dispatch: one queued request
// per call, or 410 when the stream has ended.
func (p *Plane) DispatchHandler(w http.ResponseWriter, req *http.Request) {
	session := p.authenticate(req)
	if session == nil {
		http.Error(w, "reverse session required", http.StatusUnauthorized)
		return
	}
	item, err := session.Take(req.Context())
	if err != nil {
		http.Error(w, "dispatch stream ended", http.StatusGone)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(item)
}

// RespondHandler serves POST /mcp-source-link-respond: the connector posts
// the origin response here, correlated by ID.
func (p *Plane) RespondHandler(w http.ResponseWriter, req *http.Request) {
	session := p.authenticate(req)
	if session == nil {
		http.Error(w, "reverse session required", http.StatusUnauthorized)
		return
	}
	var payload wire.DispatchResponse
	decode := json.NewDecoder(io.LimitReader(req.Body, wire.MaxBody))
	if err := decode.Decode(&payload); err != nil {
		// Drain the rejected upload so the connector's body write can finish
		// and observe the status instead of wedging on flow control with the
		// response already refused.
		_, _ = io.Copy(io.Discard, io.LimitReader(req.Body, 2*wire.MaxBody))
		http.Error(w, "invalid dispatch response", http.StatusBadRequest)
		return
	}
	if !session.Deliver(payload) {
		http.Error(w, "unknown dispatch response", http.StatusNotFound)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// probeLoop is the per-session probe cycle: prove the origin catalog, report
// health, and gate on the control state's generation and phase.
func (p *Plane) probeLoop(ctx context.Context, session *Session) {
	defer session.Close()
	for {
		select {
		case <-ctx.Done():
			return
		case <-session.closed:
			return
		default:
		}
		worker, ok := p.pool.State().Peer(session.workerID)
		if !ok || worker.Phase == "revoked" || worker.Generation != session.generation {
			return
		}
		if worker.Phase != "active" {
			if !sleepOrDone(ctx, session.closed) {
				return
			}
			continue
		}
		fingerprint, err := p.probe(ctx, session, worker)
		if err != nil || fingerprint != worker.ExpectedCatalog {
			fingerprint = ""
		}
		report := config.HealthReport{WorkerID: session.workerID, Generation: session.generation}
		if fingerprint != "" {
			report.CatalogFingerprint = &fingerprint
		}
		if admitted, err := p.pool.State().Report(ctx, report); err == nil && admitted && fingerprint != "" {
			session.MarkProbed()
		}
		if !sleepOrDone(ctx, session.closed) {
			return
		}
	}
}

func (p *Plane) probe(ctx context.Context, session *Session, worker config.Peer) (string, error) {
	probeCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	return catalog.Fetch(probeCtx, pool.RoundTripFunc(session.RoundTrip), worker)
}

// sleepOrDone waits the probe interval, reporting false when shut down.
func sleepOrDone(ctx context.Context, closed <-chan struct{}) bool {
	select {
	case <-ctx.Done():
		return false
	case <-closed:
		return false
	case <-time.After(2 * time.Second):
		return true
	}
}
