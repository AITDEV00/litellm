package pool

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/control"
)

// fakeMember is the smallest Member that lets a routing decision be observed.
type fakeMember struct {
	id      string
	source  string
	ready   bool
	capable bool
	fenced  bool
	closed  int
}

func (m *fakeMember) ID() string     { return m.id }
func (m *fakeMember) Source() string { return m.source }
func (m *fakeMember) Ready(string) bool {
	return m.ready
}
func (m *fakeMember) Capable(string) bool { return m.capable }
func (m *fakeMember) Fenced(config.Peer, bool) bool {
	return m.fenced
}
func (m *fakeMember) Close() { m.closed++ }
func (m *fakeMember) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, nil
}

func peerFor(id, source string, fingerprint string) config.Peer {
	worker := config.Peer{
		WorkerID: id, SourceID: source, Phase: "active", Generation: 1,
		ExpectedCatalog: "catalog-digest",
		// Refresh rejects any peer whose digest is not 64 hex characters, so a
		// fixture that leaves this empty makes the client retry for two minutes.
		CredentialDigest: strings.Repeat("0", 64),
	}
	if fingerprint != "" {
		worker.CatalogFingerprint = &fingerprint
	}
	return worker
}

// peerMCP is an active MCP peer. It carries no catalog fingerprint, because an
// MCP source publishes no OpenAPI document, so its recent probe is its proof.
func peerMCP(id, source string) config.Peer {
	return config.Peer{
		WorkerID: id, SourceID: source, Phase: "active", Generation: 1,
		Kind:             "mcp",
		ExpectedCatalog:  "",
		CredentialDigest: strings.Repeat("0", 64),
	}
}

// recordingMember is a fakeMember that records the request it was asked to
// serve, so a Forward test can prove which member the request reached.
type recordingMember struct {
	fakeMember
	served *http.Request
}

func (m *recordingMember) RoundTrip(req *http.Request) (*http.Response, error) {
	m.served = req
	return &http.Response{StatusCode: http.StatusOK, Body: http.NoBody, Header: make(http.Header)}, nil
}

func forwardRequest(t *testing.T, sourceID, path string, headers map[string]string) *http.Request {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/sources/"+sourceID+"/"+path, nil)
	req.SetPathValue("source", sourceID)
	req.SetPathValue("path", path)
	req.Header.Set("Authorization", "Bearer "+token)
	for name, value := range headers {
		req.Header.Set(name, value)
	}
	return req
}

// The eligibility predicate is the contract every routing decision shares.
// These cases pin each clause, so dropping any one of them fails a test.
func TestAdmittedRequiresEveryCondition(t *testing.T) {
	active := peerFor("w1", "s1", "catalog-digest")
	base := func() *fakeMember {
		return &fakeMember{id: "w1", source: "s1", ready: true, capable: true}
	}
	if !admitted(base(), active, true) {
		t.Fatal("a healthy active member was rejected")
	}

	cases := []struct {
		name   string
		member *fakeMember
		worker config.Peer
		exists bool
	}{
		{"peer gone", base(), active, false},
		{"member not ready", &fakeMember{id: "w1", ready: false, capable: true}, active, true},
		{"member not capable", &fakeMember{id: "w1", ready: true, capable: false}, active, true},
		{"member fenced", &fakeMember{id: "w1", ready: true, capable: true, fenced: true}, active, true},
	}
	for _, testCase := range cases {
		if admitted(testCase.member, testCase.worker, testCase.exists) {
			t.Fatalf("%s: still admitted", testCase.name)
		}
	}

	draining := active
	draining.Phase = "draining"
	if admitted(base(), draining, true) {
		t.Fatal("draining peer still admitted")
	}

	noFingerprint := active
	noFingerprint.CatalogFingerprint = nil
	if admitted(base(), noFingerprint, true) {
		t.Fatal("peer without a catalog fingerprint still admitted")
	}

	staleFingerprint := active
	wrong := "some-other-catalog"
	staleFingerprint.CatalogFingerprint = &wrong
	if admitted(base(), staleFingerprint, true) {
		t.Fatal("peer whose catalog does not match the expected one still admitted")
	}
}

// An MCP peer has no catalog fingerprint by design, so it must be admitted on
// the strength of its recent probe alone. If it were held to the OpenAPI rule it
// would never be dispatchable and every MCP tool call would 503.
func TestAdmittedMCPPeerDoesNotNeedACatalogFingerprint(t *testing.T) {
	member := &fakeMember{id: "w1", source: "s1", ready: true, capable: true}
	if !admitted(member, peerMCP("w1", "s1"), true) {
		t.Fatal("a healthy MCP peer without a catalog fingerprint was rejected")
	}
	notReady := &fakeMember{id: "w1", source: "s1", ready: false, capable: true}
	if admitted(notReady, peerMCP("w1", "s1"), true) {
		t.Fatal("an MCP peer that has not probed recently was admitted")
	}
	// An MCP peer carrying a stray fingerprint is not the shape the control plane
	// publishes, so it is not trusted.
	stray := peerMCP("w1", "s1")
	digest := "catalog-digest"
	stray.CatalogFingerprint = &digest
	if admitted(member, stray, true) {
		t.Fatal("an MCP peer with a catalog fingerprint was admitted")
	}
}

// A stateful MCP request carries the Mcp-Session-Id the client got at
// initialize. With one member the destination is unambiguous, so it must reach
// the member rather than be refused as an unsupported request.
func TestForwardServesStatefulMCPRequestOnSingleMember(t *testing.T) {
	client := mustClient(t, peerMCP("w1", "s1"))
	runtime := New(client)
	member := &recordingMember{fakeMember: fakeMember{id: "w1", source: "s1", ready: true, capable: true}}
	runtime.AddSession(member)

	recorder := httptest.NewRecorder()
	runtime.Forward(recorder, forwardRequest(t, "s1", "mcp", map[string]string{"Mcp-Session-Id": "session-1"}))

	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", recorder.Code, recorder.Body.String())
	}
	if member.served == nil {
		t.Fatal("the stateful request never reached the member")
	}
	if got := member.served.Header.Get("Mcp-Session-Id"); got != "session-1" {
		t.Fatalf("member saw Mcp-Session-Id %q, want session-1", got)
	}
}

// A redirect from the private origin must not leak its Location to the caller.
// The private host belongs to internal topology and, if followed, would send an
// external client to a destination the operator never approved.
func TestForwardStripsPrivateRedirectLocation(t *testing.T) {
	client := mustClient(t, peerMCP("w1", "s1"))
	runtime := New(client)
	member := &redirectMember{fakeMember: fakeMember{id: "w1", source: "s1", ready: true, capable: true}}
	runtime.AddSession(member)

	recorder := httptest.NewRecorder()
	runtime.Forward(recorder, forwardRequest(t, "s1", "mcp", nil))

	if recorder.Code != http.StatusFound {
		t.Fatalf("status = %d, want the origin 302 to stand", recorder.Code)
	}
	if location := recorder.Header().Get("Location"); location != "" {
		t.Fatalf("relay leaked redirect Location %q, want it stripped", location)
	}
}

type redirectMember struct{ fakeMember }

func (m *redirectMember) RoundTrip(*http.Request) (*http.Response, error) {
	header := make(http.Header)
	header.Set("Location", "http://127.0.0.1:18203/elsewhere")
	return &http.Response{StatusCode: http.StatusFound, Header: header, Body: http.NoBody}, nil
}

// With two members the session lives on exactly one origin, so routing it to the// wrong member would break the session. The relay must refuse rather than guess.
// A GET is used so the multi-member write guard cannot mask this one.
func TestForwardRefusesStatefulMCPRequestWithMultipleMembers(t *testing.T) {
	client := mustClient(t, peerMCP("w1", "s1"), peerMCP("w2", "s1"))
	runtime := New(client)
	runtime.AddSession(&fakeMember{id: "w1", source: "s1", ready: true, capable: true})
	runtime.AddSession(&fakeMember{id: "w2", source: "s1", ready: true, capable: true})

	for _, header := range []string{"Mcp-Session-Id", "Last-Event-Id"} {
		req := forwardRequest(t, "s1", "mcp", map[string]string{header: "x"})
		req.Method = http.MethodGet
		recorder := httptest.NewRecorder()
		runtime.Forward(recorder, req)
		if recorder.Code != http.StatusConflict {
			t.Fatalf("%s with two members: status = %d, want 409", header, recorder.Code)
		}
	}
}

func TestEligibleFiltersBySourceAndExclusion(t *testing.T) {
	client := mustClient(t,
		peerFor("w1", "s1", "catalog-digest"),
		peerFor("w2", "s1", "catalog-digest"),
		peerFor("w3", "s2", "catalog-digest"),
	)
	runtime := New(client)
	for _, member := range []*fakeMember{
		{id: "w1", source: "s1", ready: true, capable: true},
		{id: "w2", source: "s1", ready: true, capable: true},
		{id: "w3", source: "s2", ready: true, capable: true},
	} {
		runtime.AddSession(member)
	}
	if got := len(runtime.eligible("s1", "")); got != 2 {
		t.Fatalf("s1 eligible = %d, want 2", got)
	}
	if got := len(runtime.eligible("s2", "")); got != 1 {
		t.Fatalf("s2 eligible = %d, want 1", got)
	}
	if got := len(runtime.eligible("s1", "w1")); got != 1 {
		t.Fatalf("excluding w1 left %d, want 1", got)
	}
	if got := len(runtime.eligible("absent", "")); got != 0 {
		t.Fatalf("unknown source eligible = %d, want 0", got)
	}
}

func TestPickIsLeastLoadedThenRoundRobin(t *testing.T) {
	client := mustClient(t,
		peerFor("w1", "s1", "catalog-digest"),
		peerFor("w2", "s1", "catalog-digest"),
	)
	runtime := New(client)
	first := &fakeMember{id: "w1", source: "s1", ready: true, capable: true}
	second := &fakeMember{id: "w2", source: "s1", ready: true, capable: true}
	runtime.AddSession(first)
	runtime.AddSession(second)

	// With equal load, both members must get used; never one twice running.
	seen := map[string]int{}
	for range 6 {
		member := runtime.pick("s1", "")
		if member == nil {
			t.Fatal("pick returned nil with two eligible members")
		}
		seen[member.ID()]++
		runtime.release(member)
	}
	if seen["w1"] != 3 || seen["w2"] != 3 {
		t.Fatalf("round-robin distribution = %v, want 3/3", seen)
	}

	// Holding one member in flight must steer the next pick to the other,
	// because the least-loaded tie-break counts in-flight requests even when
	// DispatchInFlightCap is 0 (the cap is off, the balancing is not).
	held := runtime.pick("s1", "")
	if next := runtime.pick("s1", ""); next == nil || next.ID() == held.ID() {
		t.Fatalf("next pick reused the in-flight member %s", held.ID())
	}
	runtime.release(held)
}

// With every pick released, consecutive picks must alternate: the least-loaded
// tie-break is what prevents one connector absorbing a burst.
func TestPickAlternatesWhenEachCallCompletes(t *testing.T) {
	client := mustClient(t,
		peerFor("w1", "s1", "catalog-digest"),
		peerFor("w2", "s1", "catalog-digest"),
	)
	runtime := New(client)
	runtime.AddSession(&fakeMember{id: "w1", source: "s1", ready: true, capable: true})
	runtime.AddSession(&fakeMember{id: "w2", source: "s1", ready: true, capable: true})

	seen := map[string]int{}
	for range 6 {
		member := runtime.pick("s1", "")
		if member == nil {
			t.Fatal("pick returned nil with two eligible members")
		}
		seen[member.ID()]++
		runtime.release(member)
	}
	if seen["w1"] != 3 || seen["w2"] != 3 {
		t.Fatalf("released-pick distribution = %v, want 3/3", seen)
	}
}

func TestPickReturnsNilWhenNothingIsEligible(t *testing.T) {
	client := mustClient(t, peerFor("w1", "s1", "catalog-digest"))
	runtime := New(client)
	runtime.AddSession(&fakeMember{id: "w1", source: "s1", ready: false, capable: true})
	if member := runtime.pick("s1", ""); member != nil {
		t.Fatalf("picked an unready member: %s", member.ID())
	}
}

func TestCleanSourcePath(t *testing.T) {
	for _, allowed := range []string{"", "/", "/read", "/a/b", "/a/b/"} {
		if !CleanSourcePath(allowed) {
			t.Fatalf("rejected a safe path %q", allowed)
		}
	}
	for _, blocked := range []string{"read", "/../etc", "/a/../b", "/a/./b", "/a//b", "/a?x=1", "/a#x", "/a%b", "/a\\b", "/a\rb", "/a\nb", "/a\tb"} {
		if CleanSourcePath(blocked) {
			t.Fatalf("accepted a dangerous path %q", blocked)
		}
	}
}

// mustClient builds a real control client backed by an httptest server that
// serves the given peers, so pool tests exercise the production fetch path
// rather than reaching into control's internals.
func mustClient(t *testing.T, peers ...config.Peer) *control.Client {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		_ = json.NewEncoder(w).Encode(peers)
	}))
	t.Cleanup(server.Close)
	client, err := control.New(context.Background(), config.Config{ControlURL: server.URL, ServiceToken: token}, server.Client())
	if err != nil {
		t.Fatal(err)
	}
	return client
}

const token = "0123456789abcdef0123456789abcdef"

// Reconcile must close fenced sessions and keep healthy ones, which is the
// difference between a revoke and a transient probe miss.
func TestReconcileClosesOnlyFencedSessions(t *testing.T) {
	client := mustClient(t,
		peerFor("live", "s1", "catalog-digest"),
		peerFor("dead", "s1", "catalog-digest"),
	)
	runtime := New(client)
	live := &fakeMember{id: "live", source: "s1", ready: true, capable: true}
	dead := &fakeMember{id: "dead", source: "s1", ready: true, capable: true, fenced: true}
	runtime.AddSession(live)
	runtime.AddSession(dead)

	runtime.Reconcile()

	if dead.closed != 1 {
		t.Fatalf("fenced session closed %d times, want 1", dead.closed)
	}
	if live.closed != 0 {
		t.Fatalf("healthy session was closed %d times", live.closed)
	}
}

func TestRevokeOnlyClosesMatchingSource(t *testing.T) {
	client := mustClient(t, peerFor("w1", "s1", "catalog-digest"), peerFor("w2", "s2", "catalog-digest"))
	runtime := New(client)
	one := &fakeMember{id: "w1", source: "s1", ready: true, capable: true}
	two := &fakeMember{id: "w2", source: "s2", ready: true, capable: true}
	runtime.AddSession(one)
	runtime.AddSession(two)

	runtime.Revoke("s2", "w1")
	if one.closed != 0 {
		t.Fatal("revoked a session whose source did not match")
	}
	runtime.Revoke("s1", "w1")
	if one.closed != 1 {
		t.Fatal("did not close the matching session")
	}
	if two.closed != 0 {
		t.Fatal("closed an unrelated session")
	}
}

// A session must survive a probe miss but be closed once it is fenced.
func TestReadyGateIsSeparateFromFencing(t *testing.T) {
	member := &fakeMember{id: "w1", source: "s1", ready: true, capable: true}
	worker := peerFor("w1", "s1", "catalog-digest")
	if !admitted(member, worker, true) {
		t.Fatal("baseline member not admitted")
	}
	member.ready = false
	if admitted(member, worker, true) {
		t.Fatal("unready member admitted")
	}
	member.ready = true
	member.fenced = true
	if admitted(member, worker, true) {
		t.Fatal("fenced member admitted")
	}
}
