// Package catalog owns origin proof: fetching a source's OpenAPI catalog,
// canonicalizing it, and verifying the connector's signature over the nonce
// so a broker never trusts a catalog it did not challenge for.
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

// Canonical returns the hex SHA-256 of a catalog after confirming it parses
// as OpenAPI 3 with a paths object. JSON decoding normalizes key order, so
// the digest is stable across equivalent serializations.
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
	return hex.EncodeToString(func() []byte { sum := sha256.Sum256(encoded); return sum[:] }()), nil
}

// Verify checks the connector's signature over nonce, source, dataset, and
// catalog digest, returning the verified digest.
func Verify(worker config.Peer, nonceText string, contents []byte, signature []byte) (string, error) {
	actual, err := Canonical(contents)
	if err != nil {
		return "", err
	}
	key, err := base64.StdEncoding.DecodeString(worker.ProofKey)
	if err != nil || len(key) != ed25519.PublicKeySize {
		return "", errors.New("invalid origin proof key")
	}
	message := nonceText + "\n" + worker.SourceID + "\n" + worker.DatasetID + "\n" + actual
	if !ed25519.Verify(ed25519.PublicKey(key), []byte(message), signature) {
		return "", errors.New("origin proof signature mismatch")
	}
	return actual, nil
}

// Fetch runs one challenged probe over transport and returns the verified
// catalog digest.
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
	signature, err := base64.StdEncoding.DecodeString(resp.Header.Get("X-Source-Signature"))
	if err != nil {
		return "", errors.New("missing origin proof signature")
	}
	return Verify(worker, nonceText, contents, signature)
}
