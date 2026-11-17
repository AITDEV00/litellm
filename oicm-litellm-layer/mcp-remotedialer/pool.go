package main

import (
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"sync"
)

type dispatchMember struct {
	reverse *reverseSession
}

func (d dispatchMember) workerID() string {
	return d.reverse.workerID
}

func (d dispatchMember) roundTrip(req *http.Request) (*http.Response, error) {
	return d.reverse.roundTrip(req)
}

type runtime struct {
	state    *control
	mu       sync.Mutex
	reverse  map[string]*reverseSession
	serial   uint64
	last     map[string]uint64
	inFlight map[string]int
}

func newRuntime(state *control) *runtime {
	return &runtime{state: state, reverse: make(map[string]*reverseSession), last: make(map[string]uint64), inFlight: make(map[string]int)}
}

func (r *runtime) pick(sourceID string, exclude string) *dispatchMember {
	r.mu.Lock()
	defer r.mu.Unlock()
	var chosen *dispatchMember
	for id, session := range r.reverse {
		worker, ok := r.state.peer(id)
		if !ok || worker.SourceID != sourceID || worker.Phase != "active" || worker.WorkerID == exclude ||
			(worker.CatalogFingerprint == nil || *worker.CatalogFingerprint != worker.ExpectedCatalog) ||
			!session.ready(sourceID) || r.inFlight[id] >= 64 {
			continue
		}
		if chosen == nil || r.inFlight[id] < r.inFlight[chosen.workerID()] ||
			(r.inFlight[id] == r.inFlight[chosen.workerID()] && r.last[id] < r.last[chosen.workerID()]) {
			chosen = &dispatchMember{reverse: session}
		}
	}
	if chosen != nil {
		r.serial++
		r.last[chosen.workerID()] = r.serial
		r.inFlight[chosen.workerID()]++
	}
	return chosen
}

func (r *runtime) release(member *dispatchMember) {
	r.mu.Lock()
	r.inFlight[member.workerID()]--
	r.mu.Unlock()
}

func (r *runtime) hasActive(sourceID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	for id, session := range r.reverse {
		worker, ok := r.state.peer(id)
		if ok && worker.SourceID == sourceID && worker.Phase == "active" && worker.CatalogFingerprint != nil &&
			*worker.CatalogFingerprint == worker.ExpectedCatalog && session.ready(sourceID) {
			return true
		}
	}
	return false
}

func cleanSourcePath(path string) bool {
	return path == "" || (strings.HasPrefix(path, "/") && !strings.ContainsAny(path, "%\\?#\r\n\t") &&
		!strings.Contains(path, "//") && !strings.Contains(path+"/", "/../") && !strings.Contains(path+"/", "/./"))
}

func (r *runtime) forward(w http.ResponseWriter, req *http.Request) {
	if !sameSecret(r.state.tokenDigest, bearer(req)) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	sourceID, path := req.PathValue("source"), "/"+req.PathValue("path")
	if sourceID == "" || !cleanSourcePath(path) || req.Header.Get("Upgrade") != "" || req.Header.Get("Mcp-Session-Id") != "" || req.Method == http.MethodConnect {
		http.Error(w, "unsupported source request", http.StatusBadRequest)
		return
	}
	if (req.Header.Get("Last-Event-Id") != "" || strings.Contains(strings.ToLower(req.Header.Get("Accept")), "text/event-stream")) && !r.hasActive(sourceID) {
		http.Error(w, "stream routing requires a connected member", http.StatusBadRequest)
		return
	}
	if len(r.eligibleMembers(sourceID)) > 1 && req.Method != http.MethodGet && req.Method != http.MethodHead {
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

func (r *runtime) forwardOnce(w http.ResponseWriter, req *http.Request, sourceID, path string, member *dispatchMember) {
	released := false
	release := func() {
		if !released {
			r.release(member)
			released = true
		}
	}
	defer release()
	proxy := &httputil.ReverseProxy{
		Rewrite: func(out *httputil.ProxyRequest) {
			out.SetURL(&url.URL{Scheme: "http", Host: slot(sourceID)})
			out.Out.URL.Path = path
			out.Out.URL.RawPath = ""
			out.Out.URL.RawQuery = req.URL.RawQuery
			out.Out.Host = ""
			out.Out.Header = make(http.Header)
			for _, name := range []string{"Accept", "Accept-Encoding", "Content-Type", "Mcp-Session-Id", "Mcp-Protocol-Version", "Last-Event-Id", "If-None-Match", "If-Modified-Since"} {
				for _, value := range req.Header.Values(name) {
					out.Out.Header.Add(name, value)
				}
			}
		},
		ModifyResponse: func(resp *http.Response) error {
			resp.Header.Del("Set-Cookie")
			return nil
		},
		Transport: roundTripFunc(member.roundTrip),
		ErrorHandler: func(w http.ResponseWriter, req *http.Request, err error) {
			http.Error(w, "source upstream unavailable", http.StatusBadGateway)
		},
	}
	proxy.ServeHTTP(w, req)
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

func (r *runtime) eligibleMembers(sourceID string) []dispatchMember {
	r.mu.Lock()
	defer r.mu.Unlock()
	members := make([]dispatchMember, 0, len(r.reverse))
	for id, session := range r.reverse {
		worker, ok := r.state.peer(id)
		if ok && worker.SourceID == sourceID && worker.Phase == "active" && worker.CatalogFingerprint != nil &&
			*worker.CatalogFingerprint == worker.ExpectedCatalog && session.ready(sourceID) {
			members = append(members, dispatchMember{reverse: session})
		}
	}
	return members
}
