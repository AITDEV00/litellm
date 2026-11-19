package state

import (
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"oicm-litellm-layer/mcp-source-link/internal/target"
)

func open(t *testing.T) *Store {
	t.Helper()
	store, err := Open(t.TempDir())
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	return store
}

func mustTarget(t *testing.T, value string) target.Target {
	t.Helper()
	parsed, err := target.Parse(value)
	if err != nil {
		t.Fatalf("Parse(%q): %v", value, err)
	}
	return parsed
}

// The central requirement: several accounts coexist, each with its own key, and
// a listing never surfaces key material.
func TestAccountsCoexist(t *testing.T) {
	store := open(t)
	for _, account := range []Account{
		{Alias: "alpha", GatewayURL: "https://gw-a.example", Username: "alice"},
		{Alias: "beta", GatewayURL: "https://gw-b.example", Username: "bob"},
	} {
		if err := store.PutAccount(account, "sk-"+account.Alias+"-key-material"); err != nil {
			t.Fatalf("PutAccount(%s): %v", account.Alias, err)
		}
	}
	accounts, err := store.Accounts()
	if err != nil {
		t.Fatalf("Accounts: %v", err)
	}
	if len(accounts) != 2 {
		t.Fatalf("expected 2 accounts, got %d", len(accounts))
	}
	for _, account := range accounts {
		key, err := store.AccountKey(account.Alias)
		if err != nil {
			t.Fatalf("AccountKey(%s): %v", account.Alias, err)
		}
		if key != "sk-"+account.Alias+"-key-material" {
			t.Fatalf("account %s key mismatch", account.Alias)
		}
	}
}

// Repeating an account addition must not silently replace a live credential.
func TestDuplicateAccountRefused(t *testing.T) {
	store := open(t)
	account := Account{Alias: "alpha", GatewayURL: "https://gw.example", Username: "alice"}
	if err := store.PutAccount(account, "first"); err != nil {
		t.Fatalf("first PutAccount: %v", err)
	}
	if err := store.PutAccount(account, "second"); err == nil {
		t.Fatal("a duplicate account addition replaced a live credential")
	}
	key, err := store.AccountKey("alpha")
	if err != nil || key != "first" {
		t.Fatalf("original credential was overwritten: %q %v", key, err)
	}
}

// The stored key file must be unreadable by anyone else, or a second local user
// could act as this account.
func TestAccountKeyIsOwnerOnly(t *testing.T) {
	store := open(t)
	if err := store.PutAccount(Account{Alias: "alpha", GatewayURL: "https://gw", Username: "alice"}, "secret"); err != nil {
		t.Fatalf("PutAccount: %v", err)
	}
	info, err := os.Stat(filepath.Join(store.Base(), "accounts", "alpha.key"))
	if err != nil {
		t.Fatalf("stat key: %v", err)
	}
	if info.Mode().Perm()&0o077 != 0 {
		t.Fatalf("key file is group/world readable: %v", info.Mode().Perm())
	}
}

func TestSelectAndResolve(t *testing.T) {
	store := open(t)
	for _, alias := range []string{"alpha", "beta"} {
		if err := store.PutAccount(Account{Alias: alias, GatewayURL: "https://gw", Username: alias}, "k-"+alias); err != nil {
			t.Fatalf("PutAccount(%s): %v", alias, err)
		}
	}
	if _, err := store.ResolveAccount(""); err == nil {
		t.Fatal("resolving with no selection and no explicit alias succeeded")
	}
	if err := store.Select("beta"); err != nil {
		t.Fatalf("Select: %v", err)
	}
	if resolved, err := store.ResolveAccount(""); err != nil || resolved.Alias != "beta" {
		t.Fatalf("ResolveAccount default = %+v %v", resolved, err)
	}
	if resolved, err := store.ResolveAccount("alpha"); err != nil || resolved.Alias != "alpha" {
		t.Fatalf("ResolveAccount explicit = %+v %v", resolved, err)
	}
	if err := store.Select("missing"); err == nil {
		t.Fatal("selected a nonexistent account")
	}
}

// The derived name is the identity of a source. These cases pin that one target
// in one account is one name, a different account gives a different name, and a
// different port gives a different name.
func TestDeriveNameStableAndScoped(t *testing.T) {
	first := mustTarget(t, "127.0.0.1:18201")
	second := mustTarget(t, "127.0.0.1:18202")

	for _, testCase := range []struct {
		name    string
		account string
		target  target.Target
	}{
		{"first", "alpha", first},
		{"repeat", "alpha", first},
		{"other port", "alpha", second},
		{"other account", "beta", first},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			got := DeriveName(testCase.account, testCase.target)
			// The name must satisfy the gateway alias grammar regardless of the
			// readable prefix, because the gateway rejects a mismatch as a 422.
			if !sourceAliasPattern.MatchString(got) {
				t.Fatalf("DeriveName(%s,%v) = %q violates the gateway alias grammar", testCase.account, testCase.target, got)
			}
			// A stable digest suffix makes the name collision-resistant and unique.
			if digest := got[len(got)-10:]; !regexp.MustCompile(`^[0-9a-f]{10}$`).MatchString(digest) {
				t.Fatalf("DeriveName(%s,%v) = %q does not end in a 10-char hex digest", testCase.account, testCase.target, got)
			}
		})
	}

	// Repeating the same (account, target) must be byte-identical, and a different
	// port or account must differ, or two sources would collide on one name.
	repeat := DeriveName("alpha", first)
	if repeat != DeriveName("alpha", first) {
		t.Fatal("DeriveName is not deterministic for the same (account, target)")
	}
	if DeriveName("alpha", first) == DeriveName("alpha", second) {
		t.Fatal("two different ports produced the same derived name")
	}
	if DeriveName("alpha", first) == DeriveName("beta", first) {
		t.Fatal("two different accounts produced the same derived name")
	}
}

// A maximum-length valid Kubernetes target must still derive a name the gateway
// accepts. The earlier derivation embedded the whole readable slug and overflowed
// the 128-character grammar on a long service name.
func TestDeriveNameStaysWithinGatewayGrammarForLongTargets(t *testing.T) {
	// A maximum-length two-label service name under a long account alias pushes the
	// readable derivation past 128 characters, which the old formula produced
	// verbatim and the gateway rejected.
	long := mustTarget(t, strings.Repeat("a", 63)+".namespace-svc-name-that-is-long.svc:9001")
	got := DeriveName("a-rather-long-account-alias", long)
	if !sourceAliasPattern.MatchString(got) {
		t.Fatalf("DeriveName for a long target = %q (len %d) violates the gateway grammar", got, len(got))
	}
	if len(got) > 128 {
		t.Fatalf("DeriveName for a long target is %d chars, want <= 128", len(got))
	}
	// Two distinct long targets that share a readable prefix must not collide.
	other := mustTarget(t, strings.Repeat("a", 62)+"b.namespace-svc-name-that-is-long.svc:9001")
	if DeriveName("a-rather-long-account-alias", long) == DeriveName("a-rather-long-account-alias", other) {
		t.Fatal("distinct long targets collapsed to one derived name")
	}
}

// Two accounts registering the same host:port must remain distinct sources.
func TestSameTargetDifferentAccountsAreDistinct(t *testing.T) {
	store := open(t)
	shared := mustTarget(t, "10.34.104.99:4000")
	for _, alias := range []string{"alpha", "beta"} {
		source := Source{Name: DeriveName(alias, shared), Account: alias, Target: shared.String(), Enabled: true}
		if err := store.PutSource(source); err != nil {
			t.Fatalf("PutSource(%s): %v", alias, err)
		}
	}
	sources, err := store.Sources()
	if err != nil {
		t.Fatalf("Sources: %v", err)
	}
	if len(sources) != 2 {
		t.Fatalf("expected 2 distinct sources for two accounts, got %d", len(sources))
	}
	owned, err := store.SourcesForAccount("alpha")
	if err != nil || len(owned) != 1 {
		t.Fatalf("alpha sources = %v %v", owned, err)
	}
}

func TestPutSourceReplacesByName(t *testing.T) {
	store := open(t)
	source := Source{Name: "alpha.127.0.0.1.18201", Account: "alpha", Target: "127.0.0.1:18201", WorkerID: ""}
	if err := store.PutSource(source); err != nil {
		t.Fatalf("first PutSource: %v", err)
	}
	source.WorkerID = "w-1"
	source.SourceID = "s-1"
	source.Generation = 1
	source.Credential = "0000000000000000000000000000000000000000000000000000000000000000"
	if err := store.PutSource(source); err != nil {
		t.Fatalf("second PutSource: %v", err)
	}
	all, err := store.Sources()
	if err != nil {
		t.Fatalf("Sources: %v", err)
	}
	if len(all) != 1 || all[0].WorkerID != "w-1" {
		t.Fatalf("PutSource did not replace by name: %+v", all)
	}
	if !all[0].Registered() {
		t.Fatal("a fully populated source did not report Registered")
	}
}

func TestRemoveOnlySelectedSource(t *testing.T) {
	store := open(t)
	for _, name := range []string{"alpha.127.0.0.1.18201", "alpha.127.0.0.1.18202"} {
		if err := store.PutSource(Source{Name: name, Account: "alpha", Target: "127.0.0.1:1"}); err != nil {
			t.Fatalf("PutSource(%s): %v", name, err)
		}
	}
	if err := store.RemoveSource(SourceKey{Account: "alpha", Name: "alpha.127.0.0.1.18201"}); err != nil {
		t.Fatalf("RemoveSource: %v", err)
	}
	all, err := store.Sources()
	if err != nil {
		t.Fatalf("Sources: %v", err)
	}
	if len(all) != 1 || all[0].Name != "alpha.127.0.0.1.18202" {
		t.Fatalf("removal disturbed an unrelated source: %+v", all)
	}
}

func TestStateSurvivesReopen(t *testing.T) {
	base := t.TempDir()
	store, err := Open(base)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	if err := store.PutAccount(Account{Alias: "alpha", GatewayURL: "https://gw", Username: "alice"}, "k"); err != nil {
		t.Fatalf("PutAccount: %v", err)
	}
	if err := store.PutSource(Source{Name: "alpha.127.0.0.1.18201", Account: "alpha", Target: "127.0.0.1:18201"}); err != nil {
		t.Fatalf("PutSource: %v", err)
	}
	reopened, err := Open(base)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	if accounts, err := reopened.Accounts(); err != nil || len(accounts) != 1 {
		t.Fatalf("accounts after reopen = %v %v", accounts, err)
	}
	if sources, err := reopened.Sources(); err != nil || len(sources) != 1 {
		t.Fatalf("sources after reopen = %v %v", sources, err)
	}
}

func TestAccountAliasRejectsPathTraversal(t *testing.T) {
	store := open(t)
	for _, alias := range []string{"../escape", "a/b", "Uppercase", "-lead", "with space", ""} {
		if err := store.PutAccount(Account{Alias: alias, GatewayURL: "https://gw", Username: "u"}, "k"); err == nil {
			t.Fatalf("accepted unsafe account alias %q", alias)
		}
	}
}

// A state file made group-readable must be refused, so a shared host cannot leak
// one user's credential to another.
func TestTamperedPermissionsRefused(t *testing.T) {
	store := open(t)
	if err := store.PutAccount(Account{Alias: "alpha", GatewayURL: "https://gw", Username: "alice"}, "secret"); err != nil {
		t.Fatalf("PutAccount: %v", err)
	}
	path := filepath.Join(store.Base(), "accounts", "alpha.key")
	if err := os.Chmod(path, 0o644); err != nil {
		t.Fatalf("chmod: %v", err)
	}
	if _, err := store.AccountKey("alpha"); err == nil {
		t.Fatal("read a world-readable key file")
	} else if !errors.Is(err, os.ErrPermission) && err.Error() == "" {
		t.Fatalf("unexpected error text: %v", err)
	}
}

// Two accounts writing the same source name must not overwrite each other. The
// store keys on (account, name), so an explicit or colliding name under beta
// cannot replace alpha's row.
func TestPutSourceIsScopedToAccount(t *testing.T) {
	store := open(t)
	shared := "shared.name.test"
	if err := store.PutSource(Source{Name: shared, Account: "alpha", Target: "127.0.0.1:9001", Enabled: true}); err != nil {
		t.Fatalf("put alpha: %v", err)
	}
	if err := store.PutSource(Source{Name: shared, Account: "beta", Target: "127.0.0.1:9002", Enabled: true}); err != nil {
		t.Fatalf("put beta: %v", err)
	}
	sources, err := store.Sources()
	if err != nil {
		t.Fatalf("sources: %v", err)
	}
	if len(sources) != 2 {
		t.Fatalf("expected 2 rows, got %d: %+v", len(sources), sources)
	}
	for _, source := range sources {
		want := map[string]string{"alpha": "127.0.0.1:9001", "beta": "127.0.0.1:9002"}[source.Account]
		if source.Target != want {
			t.Fatalf("account %s target = %q, want %q (cross-account overwrite)", source.Account, source.Target, want)
		}
	}
}

// The same explicit display name under two accounts must stay two independent
// sources: an account-scoped lookup returns only its own, and a scoped removal
// deletes only its own. The old name-only Source/RemoveSource adopted alpha's
// record for beta and deleted both on one remove.
func TestAccountScopedLookupAndRemoveDoNotCrossAccounts(t *testing.T) {
	store := open(t)
	if err := store.PutSource(Source{Name: "shared", Account: "alpha", Target: "10.0.0.1:9001", Credential: strings.Repeat("a", 64)}); err != nil {
		t.Fatalf("put alpha: %v", err)
	}
	if err := store.PutSource(Source{Name: "shared", Account: "beta", Target: "10.0.0.2:9002", Credential: strings.Repeat("b", 64)}); err != nil {
		t.Fatalf("put beta: %v", err)
	}

	alpha, found, err := store.Source(SourceKey{Account: "alpha", Name: "shared"})
	if err != nil || !found {
		t.Fatalf("alpha lookup: found=%v err=%v", found, err)
	}
	if alpha.Target != "10.0.0.1:9001" {
		t.Fatalf("alpha lookup returned %q, adopted the wrong account", alpha.Target)
	}
	beta, found, err := store.Source(SourceKey{Account: "beta", Name: "shared"})
	if err != nil || !found || beta.Target != "10.0.0.2:9002" {
		t.Fatalf("beta lookup = %+v found=%v err=%v", beta, found, err)
	}

	if err := store.RemoveSource(SourceKey{Account: "alpha", Name: "shared"}); err != nil {
		t.Fatalf("remove alpha: %v", err)
	}
	all, err := store.Sources()
	if err != nil {
		t.Fatalf("sources: %v", err)
	}
	if len(all) != 1 || all[0].Account != "beta" {
		t.Fatalf("removing alpha/shared must leave only beta/shared, got %+v", all)
	}
}
