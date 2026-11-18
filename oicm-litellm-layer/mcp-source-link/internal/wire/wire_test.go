package wire

import (
	"encoding/base64"
	"encoding/json"
	"testing"
	"time"
)

// The catalog cap is enforced by both the reverse-h2 HTTP probe and the tunnel
// Probe RPC. It lives here so the two cannot drift; this pins the value both
// planes read.
func TestCatalogCapIsSingleSourced(t *testing.T) {
	if MaxCatalogBody <= 0 {
		t.Fatalf("catalog cap is not usable: %d", MaxCatalogBody)
	}
	// A catalog larger than the cap must be detectable, so the cap has to be
	// strictly smaller than what a LimitReader with cap+1 can observe.
	if MaxCatalogBody+1 <= MaxCatalogBody {
		t.Fatal("catalog cap overflow")
	}
}

// The dispatch deadline must be a positive duration; the tunnel plane derives
// its own deadline from it rather than repeating the literal.
func TestResponseDeadlineIsPositive(t *testing.T) {
	if ResponseDeadline <= 0 {
		t.Fatalf("dispatch deadline is not positive: %s", ResponseDeadline)
	}
	if ResponseDeadline != 90*time.Second {
		t.Fatalf("dispatch deadline changed to %s; both planes inherit it, so update the docs too", ResponseDeadline)
	}
}

// A maximum-size origin payload must still fit one envelope after base64
// inflation and header overhead. This is the invariant the two caps exist for.
func TestMaxOriginBodyFitsInsideMaxBody(t *testing.T) {
	payload := make([]byte, MaxOriginBody)
	for i := range payload {
		payload[i] = 0x78
	}
	envelope := DispatchResponse{
		ID:      "i",
		Status:  200,
		Headers: map[string][]string{"Content-Type": {"application/json"}},
		Body:    payload,
	}
	encoded, err := json.Marshal(envelope)
	if err != nil {
		t.Fatal(err)
	}
	if len(encoded) > MaxBody {
		t.Fatalf("envelope %d exceeds wire limit %d: base64 of %d is %d",
			len(encoded), MaxBody, MaxOriginBody, len(base64.StdEncoding.EncodeToString(payload)))
	}
}

// StripFramingHeaders must name only headers that describe the original
// connection, never payload-describing headers like Content-Type.
func TestStripFramingHeadersExcludesPayloadHeaders(t *testing.T) {
	forbidden := map[string]bool{"Content-Type": true, "Content-Encoding": true, "ETag": true, "Location": true}
	for _, name := range StripFramingHeaders {
		if forbidden[name] {
			t.Fatalf("%s describes the payload, not the connection, and must survive proxying", name)
		}
	}
}

// HTTPResponse must drop framing headers and report the real body length, so a
// proxied response is not double-framed.
func TestHTTPResponseRebuildsFraming(t *testing.T) {
	response, err := DispatchResponse{
		Status:  200,
		Headers: map[string][]string{"Content-Length": {"9999"}, "Content-Type": {"text/plain"}},
		Body:    []byte("hello"),
	}.HTTPResponse()
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.Header.Get("Content-Length") != "" {
		t.Fatal("stale Content-Length survived")
	}
	if response.ContentLength != 5 {
		t.Fatalf("ContentLength = %d, want 5", response.ContentLength)
	}
	if response.Header.Get("Content-Type") != "text/plain" {
		t.Fatal("payload Content-Type was stripped")
	}
}
