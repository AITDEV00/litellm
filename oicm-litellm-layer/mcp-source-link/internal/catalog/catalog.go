// Package catalog owns origin catalog validation: fetching a source's OpenAPI
// document, canonicalizing it, and computing the digest the broker records as the
// catalog fingerprint.
//
// The old contract also required the origin to return an Ed25519 signature over a
// nonce, source id, dataset id and digest. That is removed from this product path:
// the broker reaches the origin through the connector's own tunnel, so a connector
// that controls the target response and supplies its own key proves nothing about
// who handled the request. The signature is therefore no longer requested or
// required. When an origin does supply one, Verify still checks it against the
// pinned key, so an independent origin integration can opt in later without another
// protocol change.
package catalog

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"

	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/wire"
)

// Canonical returns the hex SHA-256 of a catalog after confirming it parses as
// OpenAPI 3 with a paths object. JSON decoding normalizes key order, so the digest
// is stable across equivalent serializations.
func Canonical(contents []byte) (string, error) {
	var document map[string]any
	decoder := json.NewDecoder(strings.NewReader(string(contents)))
	decoder.UseNumber()
	if err := decoder.Decode(&document); err != nil {
		return "", errors.New("catalog is not JSON")
	}
	version, ok := document["openapi"].(string)
	if !ok || !strings.HasPrefix(version, "3.") {
		return "", errors.New("catalog is not OpenAPI 3")
	}
	paths, ok := document["paths"].(map[string]any)
	if !ok || paths == nil {
		return "", errors.New("catalog has no paths")
	}
	encoded, err := json.Marshal(document)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(encoded)
	return hex.EncodeToString(sum[:]), nil
}

// Verify returns the catalog digest and, when a signature is supplied, confirms it
// against the pinned origin key.
//
// A signature is no longer required for the local/OpenAPI flow, so an empty
// signature is accepted once the catalog itself is canonical. A supplied signature
// is still checked, and a wrong one is refused rather than ignored, so an origin
// that opts into proving its identity cannot have that proof silently bypassed.
func Verify(worker config.Peer, nonceText string, contents []byte, signature []byte) (string, error) {
	actual, err := Canonical(contents)
	if err != nil {
		return "", err
	}
	if len(signature) == 0 {
		return actual, nil
	}
	key, err := base64.StdEncoding.DecodeString(worker.ProofKey)
	if err != nil || len(key) != ed25519.PublicKeySize {
		return "", errors.New("origin supplied a signature but the pinned proof key is unusable")
	}
	message := nonceText + "\n" + worker.SourceID + "\n" + worker.DatasetID + "\n" + actual
	if !ed25519.Verify(ed25519.PublicKey(key), []byte(message), signature) {
		return "", errors.New("origin proof signature mismatch")
	}
	return actual, nil
}

// Fetch runs one catalog probe over transport and returns the verified digest.
func Fetch(ctx context.Context, transport http.RoundTripper, worker config.Peer) (string, error) {
	nonce := make([]byte, 32)
	if _, err := rand.Read(nonce); err != nil {
		return "", err
	}
	nonceText := base64.RawURLEncoding.EncodeToString(nonce)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://"+config.Slot(worker.SourceID)+"/openapi.json", nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("X-Source-Nonce", nonceText)
	resp, err := transport.RoundTrip(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	contents, err := io.ReadAll(io.LimitReader(resp.Body, wire.MaxCatalogBody+1))
	if err != nil || resp.StatusCode != http.StatusOK {
		return "", errors.New("source catalog probe failed")
	}
	if len(contents) > wire.MaxCatalogBody {
		return "", errors.New("source catalog exceeds the size limit")
	}
	// A signature is optional now; an absent header is not an error. A malformed
	// one is passed through so Verify reports it rather than being ignored here.
	signature, _ := base64.StdEncoding.DecodeString(resp.Header.Get("X-Source-Signature"))
	return Verify(worker, nonceText, contents, signature)
}
