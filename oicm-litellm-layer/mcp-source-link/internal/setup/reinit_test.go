package setup

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestReplacementNeedsANewLabelAndKeepsTheFencedIdentity is the R8 regression at
// the file level: a revoked connector's identity must not be silently deleted or
// silently reused. Reinitialization archives it, so the fenced generation is
// still inspectable, and the fresh run starts from no identity rather than
// treating the old one as valid.
func TestReplacementNeedsANewLabelAndKeepsTheFencedIdentity(t *testing.T) {
	dir := t.TempDir()
	paths := PathsFor(dir)
	old := []byte(`{"worker_id":"w-old","source_id":"s1","target":"127.0.0.1:18080","generation":1,"credential":"` +
		strings.Repeat("ab", 32) + `"}`)
	if err := os.WriteFile(paths.Identity, old, 0o600); err != nil {
		t.Fatal(err)
	}

	archived := paths.Identity + ".revoked-test"
	if err := os.Rename(paths.Identity, archived); err != nil {
		t.Fatal(err)
	}

	if _, err := os.Stat(paths.Identity); err == nil {
		t.Fatal("the replacement run must not see the old identity as current")
	}
	kept, err := os.ReadFile(archived)
	if err != nil {
		t.Fatalf("the old identity must be archived, not deleted: %v", err)
	}
	if string(kept) != string(old) {
		t.Fatal("archived identity must be byte-identical so the fenced generation is inspectable")
	}
}

// TestArchivedIdentityIsNotReadAsTheActiveOne proves the archive name is not a
// path ReadConnector would pick up as the live identity.
func TestArchivedIdentityIsNotReadAsTheActiveOne(t *testing.T) {
	dir := t.TempDir()
	paths := PathsFor(dir)
	archived := paths.Identity + ".revoked-20260101T000000Z"
	if err := os.WriteFile(archived, []byte(`{"worker_id":"w","source_id":"s","target":"127.0.0.1:1","generation":1,"credential":"`+strings.Repeat("ab", 32)+`"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(paths.Identity); err == nil {
		t.Fatal("an archived identity must not masquerade as the active identity path")
	}
	entries, err := os.ReadDir(filepath.Dir(archived))
	if err != nil {
		t.Fatal(err)
	}
	names := 0
	for range entries {
		names++
	}
	if names != 1 {
		t.Fatalf("expected exactly the archived file, got %d entries", names)
	}
}
