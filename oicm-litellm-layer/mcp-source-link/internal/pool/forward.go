package pool

import (
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"

	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/control"
)

// forwardHeaders are the request headers a source may receive. Everything
// else, including hop-by-hop and credential headers, is dropped.
var forwardHeaders = [...]string{
	"Accept", "Accept-Encoding", "Content-Type", "Mcp-Session-Id",
	"Mcp-Protocol-Version", "Last-Event-Id", "If-None-Match", "If-Modified-Since",
}

// CleanSourcePath rejects paths that could escape the source root or smuggle
// a second origin, and rejects control characters outright.
func CleanSourcePath(path string) bool {
	return path == "" || (strings.HasPrefix(path, "/") && !strings.ContainsAny(path, "%\\?#\r\n\t") &&
		!strings.Contains(path, "//") && !strings.Contains(path+"/", "/../") && !strings.Contains(path+"/", "/./"))
}

// pick chooses the least loaded eligible member, round-robin on ties.
func (r *Runtime) pick(sourceID string, exclude string) Member {
	r.mu.Lock()
	defer r.mu.Unlock()
	var chosen Member
	for id, session := range r.sessions {
		worker, ok := r.state.Peer(id)
		if worker.WorkerID == exclude {
			continue
		}
		if !admitted(session, worker, ok) || worker.SourceID != sourceID {
			continue
		}
		if DispatchInFlightCap > 0 && r.inFlight[id] >= DispatchInFlightCap {
			continue
		}
		if chosen == nil || r.inFlight[id] < r.inFlight[chosen.ID()] ||
			(r.inFlight[id] == r.inFlight[chosen.ID()] && r.last[id] < r.last[chosen.ID()]) {
			chosen = session
		}
	}
	if chosen != nil {
		r.serial++
		r.last[chosen.ID()] = r.serial
		r.inFlight[chosen.ID()]++
	}
	return chosen
}

func (r *Runtime) release(m Member) {
	r.mu.Lock()
	r.inFlight[m.ID()]--
	r.mu.Unlock()
}

// Forward serves one gateway request from an eligible member.
//
// A stateful MCP source carries its session on one member's origin: the
// Mcp-Session-Id a client echoes after initialize, or the SSE stream a
// tools/call keeps open, is only meaningful on the member that opened it.
// Routing either to a different member would hit an origin that never saw the
// session, so they are served only when exactly one member can serve the
// source. With a single member the destination is unambiguous; with several the
// request is refused rather than silently mis-routed.
func (r *Runtime) Forward(w http.ResponseWriter, req *http.Request) {
	if !control.SameSecret(r.state.TokenDigest(), control.Bearer(req)) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	sourceID, path := req.PathValue("source"), "/"+req.PathValue("path")
	if sourceID == "" || !CleanSourcePath(path) || req.Header.Get("Upgrade") != "" || req.Method == http.MethodConnect {
		http.Error(w, "unsupported source request", http.StatusBadRequest)
		return
	}
	eligible := r.eligible(sourceID, "")
	stateful := req.Header.Get("Mcp-Session-Id") != "" ||
		req.Header.Get("Last-Event-Id") != "" ||
		strings.Contains(strings.ToLower(req.Header.Get("Accept")), "text/event-stream")
	if stateful && len(eligible) > 1 {
		http.Error(w, "stateful mcp routing requires a single connected member", http.StatusConflict)
		return
	}
	if len(eligible) > 1 && req.Method != http.MethodGet && req.Method != http.MethodHead {
		http.Error(w, "multi-member write routing requires an explicit tool policy", http.StatusConflict)
		return
	}
	member := r.pick(sourceID, "")
	if member == nil {
		w.Header().Set("Retry-After", "1")
		http.Error(w, "no eligible connector", http.StatusServiceUnavailable)
		return
	}
	r.forwardOnce(w, req, sourceID, path, member)
}

func (r *Runtime) forwardOnce(w http.ResponseWriter, req *http.Request, sourceID, path string, m Member) {
	released := false
	release := func() {
		if !released {
			r.release(m)
			released = true
		}
	}
	defer release()
	proxy := &httputil.ReverseProxy{
		Rewrite: func(out *httputil.ProxyRequest) {
			out.SetURL(&url.URL{Scheme: "http", Host: config.Slot(sourceID)})
			out.Out.URL.Path = path
			out.Out.URL.RawPath = ""
			out.Out.URL.RawQuery = req.URL.RawQuery
			out.Out.Host = ""
			out.Out.Header = make(http.Header)
			for _, name := range forwardHeaders {
				for _, value := range req.Header.Values(name) {
					out.Out.Header.Add(name, value)
				}
			}
		},
		ModifyResponse: func(resp *http.Response) error {
			resp.Header.Del("Set-Cookie")
			return nil
		},
		Transport:     m,
		FlushInterval: -1,
		ErrorHandler: func(w http.ResponseWriter, req *http.Request, err error) {
			http.Error(w, "source upstream unavailable", http.StatusBadGateway)
		},
	}
	proxy.ServeHTTP(w, req)
}

// RevokeHTTP is the loopback revocation endpoint the control app calls.
func (r *Runtime) RevokeHTTP(w http.ResponseWriter, req *http.Request) {
	if !control.SameSecret(r.state.TokenDigest(), control.Bearer(req)) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	host, _, err := net.SplitHostPort(req.RemoteAddr)
	if err != nil || !net.ParseIP(host).IsLoopback() {
		http.Error(w, "loopback control required", http.StatusForbidden)
		return
	}
	memberID := req.Header.Get("X-Source-Member")
	if member, ok := r.state.Peer(memberID); ok && member.SourceID != req.Header.Get("X-Source-Id") {
		http.Error(w, "source binding mismatch", http.StatusBadRequest)
		return
	}
	if err := r.state.Refresh(req.Context()); err != nil {
		http.Error(w, "lifecycle state unavailable", http.StatusServiceUnavailable)
		return
	}
	r.Revoke(req.Header.Get("X-Source-Id"), memberID)
	r.Reconcile()
	w.WriteHeader(http.StatusNoContent)
}
