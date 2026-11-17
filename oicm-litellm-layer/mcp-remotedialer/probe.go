package main

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
	"time"
)

func canonicalCatalog(contents []byte) (string, error) {
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

func probeCatalog(ctx context.Context, transport http.RoundTripper, worker peer) (string, error) {
	nonce := make([]byte, 32)
	if _, err := rand.Read(nonce); err != nil {
		return "", err
	}
	nonceText := base64.RawURLEncoding.EncodeToString(nonce)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://"+slot(worker.SourceID)+"/openapi.json", nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("X-Source-Nonce", nonceText)
	resp, err := transport.RoundTrip(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	contents, err := io.ReadAll(io.LimitReader(resp.Body, 600000))
	if err != nil || resp.StatusCode != http.StatusOK {
		return "", errors.New("source catalog probe failed")
	}
	actual, err := canonicalCatalog(contents)
	if err != nil {
		return "", err
	}
	key, err := base64.StdEncoding.DecodeString(worker.ProofKey)
	if err != nil || len(key) != ed25519.PublicKeySize {
		return "", errors.New("invalid origin proof key")
	}
	signature, err := base64.StdEncoding.DecodeString(resp.Header.Get("X-Source-Signature"))
	if err != nil {
		return "", errors.New("missing origin proof signature")
	}
	message := nonceText + "\n" + worker.SourceID + "\n" + worker.DatasetID + "\n" + actual
	if !ed25519.Verify(ed25519.PublicKey(key), []byte(message), signature) {
		return "", errors.New("origin proof signature mismatch")
	}
	return actual, nil
}

func (s *reverseSession) probe(worker peer) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	return probeCatalog(ctx, roundTripFunc(s.roundTrip), worker)
}

func (r *runtime) reverseProbeLoop(ctx context.Context, session *reverseSession) {
	defer session.close()
	for {
		select {
		case <-ctx.Done():
			return
		case <-session.closed:
			return
		default:
		}
		worker, ok := r.state.peer(session.workerID)
		if !ok || worker.Phase == "revoked" || worker.Generation != session.generation {
			return
		}
		if worker.Phase != "active" {
			select {
			case <-ctx.Done():
				return
			case <-session.closed:
				return
			case <-time.After(2 * time.Second):
			}
			continue
		}
		fingerprint, err := session.probe(worker)
		if err != nil || fingerprint != worker.ExpectedCatalog {
			fingerprint = ""
		}
		report := healthReport{WorkerID: session.workerID, Generation: session.generation, CatalogFingerprint: nil}
		if fingerprint != "" {
			report.CatalogFingerprint = &fingerprint
		}
		if admitted, err := r.state.report(ctx, report); err == nil && admitted && fingerprint != "" {
			session.mu.Lock()
			session.lastProbe = time.Now()
			session.mu.Unlock()
		}
		select {
		case <-ctx.Done():
			return
		case <-session.closed:
			return
		case <-time.After(2 * time.Second):
		}
	}
}
