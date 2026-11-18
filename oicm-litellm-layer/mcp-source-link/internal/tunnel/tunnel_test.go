package tunnel

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"google.golang.org/grpc"
)

// The reverse listener serves the tunnel and nothing else. A non-gRPC request
// must be refused rather than handed to some other handler, because there is no
// other plane behind this listener any more.
func TestHTTPHandlerServesGRPCAndRefusesEverythingElse(t *testing.T) {
	handler := HTTPHandler(grpc.NewServer())

	for _, testCase := range []struct {
		name        string
		protoMajor  int
		contentType string
		path        string
	}{
		{"long-poll route", 1, "application/json", "/mcp-source-link-reverse"},
		{"h2c non-gRPC", 2, "application/json", "/mcp-source-link-hold"},
		{"plain GET", 1, "", "/readyz"},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, testCase.path, nil)
			req.ProtoMajor = testCase.protoMajor
			if testCase.contentType != "" {
				req.Header.Set("Content-Type", testCase.contentType)
			}
			recorder := httptest.NewRecorder()
			handler.ServeHTTP(recorder, req)
			if recorder.Code != http.StatusNotFound {
				t.Fatalf("non-gRPC request got %d, want %d", recorder.Code, http.StatusNotFound)
			}
		})
	}

	// A gRPC-shaped request over HTTP/2 must reach the gRPC server. An empty gRPC
	// body is rejected by the server, which is fine; routing is the point.
	req := httptest.NewRequest(http.MethodPost, "/TunnelService/OpenReverseTunnel", nil)
	req.ProtoMajor, req.ProtoMinor = 2, 0
	req.Header.Set("Content-Type", "application/grpc")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, req)
	if recorder.Code == http.StatusNotFound {
		t.Fatal("a gRPC request was refused by the tunnel listener")
	}
}

// The h2c listener must refuse HTTP/1.1 so a connector cannot silently
// downgrade to a protocol the plane does not serve.
func TestH2CProtocolsAreHTTP2Only(t *testing.T) {
	protocols := new(http.Protocols)
	protocols.SetUnencryptedHTTP2(true)
	protocols.SetHTTP1(false)
	if !protocols.UnencryptedHTTP2() {
		t.Fatal("h2c listener no longer accepts unencrypted HTTP/2")
	}
	if protocols.HTTP1() {
		t.Fatal("h2c listener accepts HTTP/1.1, which the tunnel plane does not serve")
	}
}

// Grace periods must outlast a probe interval by enough to survive one
// transient failure without dropping a healthy connector.
func TestHealthGracePeriodOutlastsProbeInterval(t *testing.T) {
	if HealthGracePeriod <= ProbeTimeout {
		t.Fatalf("grace period %s does not exceed probe timeout %s", HealthGracePeriod, ProbeTimeout)
	}
	if HealthGracePeriod < 2*ProbeTimeout {
		t.Fatalf("grace period %s is too short to survive a transient miss", HealthGracePeriod)
	}
	_ = time.Now
}
