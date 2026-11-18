// Package catalog owns origin catalog validation: canonicalizing a source's
// OpenAPI document and computing the digest the broker records as the catalog
// fingerprint.
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
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"

	"oicm-litellm-layer/mcp-source-link/internal/config"
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
