package catalog

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"testing"

	"oicm-litellm-layer/mcp-source-link/internal/config"
)

const signedCatalog = `{"openapi":"3.1.0","info":{"title":"experiment","version":"1"},"paths":{"/read":{"get":{"operationId":"read_source"}}}}`

func TestCanonicalRejectsNonOpenAPI(t *testing.T) {
	for _, contents := range []string{
		`not json`,
		`{"openapi":"2.0","paths":{}}`,
		`{"openapi":"3.1.0"}`,
		`{"openapi":"3.1.0","paths":null}`,
	} {
		if _, err := Canonical([]byte(contents)); err == nil {
			t.Fatalf("accepted a non-OpenAPI catalog: %s", contents)
		}
	}
}

func TestCanonicalIgnoresKeyOrderAndWhitespace(t *testing.T) {
	first, err := Canonical([]byte(signedCatalog))
	if err != nil {
		t.Fatal(err)
	}
	reordered, err := Canonical([]byte(`{"paths":{"/read":{"get":{"operationId":"read_source"}}},  "info":{"version":"1","title":"experiment"},"openapi":"3.1.0"}`))
	if err != nil {
		t.Fatal(err)
	}
	if first != reordered {
		t.Fatal("equivalent catalogs produced different digests")
	}
	different, err := Canonical([]byte(`{"openapi":"3.1.0","info":{"title":"other","version":"1"},"paths":{"/read":{}}}`))
	if err != nil {
		t.Fatal(err)
	}
	if first == different {
		t.Fatal("different catalogs produced the same digest")
	}
}

func TestVerifyRejectsWrongKeyWrongMessageAndBadSignature(t *testing.T) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	worker := config.Peer{
		SourceID: "source-1", DatasetID: "dataset-1",
		ProofKey: base64.StdEncoding.EncodeToString(pub),
	}
	digest, err := Canonical([]byte(signedCatalog))
	if err != nil {
		t.Fatal(err)
	}
	nonce := "nonce-value"
	message := nonce + "\n" + worker.SourceID + "\n" + worker.DatasetID + "\n" + digest
	good := ed25519.Sign(priv, []byte(message))

	if got, err := Verify(worker, nonce, []byte(signedCatalog), good); err != nil || got != digest {
		t.Fatalf("valid signature rejected: %q %v", got, err)
	}

	// A signature over a different nonce must not verify, which is what stops
	// a captured probe response from being replayed.
	if _, err := Verify(worker, "other-nonce", []byte(signedCatalog), good); err == nil {
		t.Fatal("accepted a signature bound to a different nonce")
	}
	// A signature bound to another dataset must not verify.
	otherDataset := worker
	otherDataset.DatasetID = "dataset-2"
	if _, err := Verify(otherDataset, nonce, []byte(signedCatalog), good); err == nil {
		t.Fatal("accepted a signature bound to a different dataset")
	}
	// Tampered signature bytes must not verify.
	tampered := append([]byte(nil), good...)
	tampered[0] ^= 0xff
	if _, err := Verify(worker, nonce, []byte(signedCatalog), tampered); err == nil {
		t.Fatal("accepted a tampered signature")
	}
	// A proof key that is not an ed25519 key is a configuration error.
	short := worker
	short.ProofKey = base64.StdEncoding.EncodeToString([]byte("short"))
	if _, err := Verify(short, nonce, []byte(signedCatalog), good); err == nil {
		t.Fatal("accepted a non-ed25519 proof key")
	}
}
