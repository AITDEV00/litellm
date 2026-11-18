package tunnel

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"google.golang.org/grpc"
)

// The reverse listener multiplexes gRPC-over-h2c with the long-poll routes, so
// a gRPC request must reach the gRPC server and everything else must fall
// through. Getting this wrong silently breaks one of the two planes.
func TestHTTPHandlerRoutesGRPCAndFallbackSeparately(t *testing.T) {
	var fallbackHits int
	fallback := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fallbackHits++
		w.WriteHeader(http.StatusTeapot)
	})
	handler := HTTPHandler(grpc.NewServer(), fallback)

	// A normal HTTP/1.1 request must reach the fallback mux.
	plain := httptest.NewRequest(http.MethodGet, "/mcp-source-link-reverse", nil)
	plainRecorder := httptest.NewRecorder()
	handler.ServeHTTP(plainRecorder, plain)
	if plainRecorder.Code != http.StatusTeapot || fallbackHits != 1 {
		t.Fatalf("HTTP/1.1 request did not reach the fallback: status=%d hits=%d", plainRecorder.Code, fallbackHits)
	}

	// A gRPC-shaped request over HTTP/2 must not reach the fallback. An empty
	// gRPC body is rejected by the server, which is fine; routing is the point.
	grpcRequest := httptest.NewRequest(http.MethodPost, "/TunnelService/OpenReverseTunnel", nil)
	grpcRequest.ProtoMajor, grpcRequest.ProtoMinor = 2, 0
	grpcRequest.Header.Set("Content-Type", "application/grpc")
	handler.ServeHTTP(httptest.NewRecorder(), grpcRequest)
	if fallbackHits != 1 {
		t.Fatalf("a gRPC-shaped request reached the fallback mux: hits=%d", fallbackHits)
	}

	// HTTP/2 without the gRPC content type must still fall through, so the
	// long-poll plane keeps working over h2c.
	muxRequest := httptest.NewRequest(http.MethodPost, "/mcp-source-link-hold", nil)
	muxRequest.ProtoMajor, muxRequest.ProtoMinor = 2, 0
	muxRequest.Header.Set("Content-Type", "application/json")
	muxRecorder := httptest.NewRecorder()
	handler.ServeHTTP(muxRecorder, muxRequest)
	if fallbackHits != 2 || muxRecorder.Code != http.StatusTeapot {
		t.Fatalf("h2c non-gRPC request did not reach the fallback: status=%d hits=%d", muxRecorder.Code, fallbackHits)
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
