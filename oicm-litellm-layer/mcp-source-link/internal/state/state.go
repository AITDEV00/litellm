// Package state owns the connector's local, on-disk model of accounts and sources.
//
// The previous layout kept one user key, one bootstrap input and one connector
// identity per state directory, which meant a second account or a second source
// needed either a second login or a hand-edited file. This package replaces that
// with two independent relations:
//
//	account  one gateway login: alias, gateway URL, username, scoped user key
//	source   one exposed origin: owning account, derived name, target, worker identity
//
// One agent process can therefore serve several accounts and several sources at
// once, and adding a source reuses its account's stored credential instead of
// asking for a password again.
//
// Every file this package writes is owner-only and atomic. A password is never
// stored; only the scoped key the gateway issues for it.
package state

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"oicm-litellm-layer/mcp-source-link/internal/target"
)

// accountAliasPattern is the local account name grammar. It is deliberately
// stricter than the gateway's source alias rule: it is a directory name, so it
// may not contain a path separator, a dot that could be read as a relative path,
// or whitespace.
var accountAliasPattern = regexp.MustCompile(`^[a-z][a-z0-9-]{1,31}$`)

// sourceAliasPattern mirrors the gateway's source alias grammar exactly. DeriveName
// must always produce a value that matches it, because that value is sent to the
// gateway as the source alias and a mismatch is rejected as a 422.
var sourceAliasPattern = regexp.MustCompile(`^[a-z][a-z0-9_.]{2,127}$`)

// Account is one stored gateway login. It holds the scoped key, never a password.
//
// Key is the ordinary user's virtual key the gateway exchanged for the password.
// It is written to its own owner-only file (`<alias>.key`) so a listing that reads
// only `accounts.json` can never surface credential material.
type Account struct {
	Alias      string `json:"alias"`
	GatewayURL string `json:"gateway_url"`
	Username   string `json:"username"`
}

// Source is one exposed origin bound to an account.
//
// Name is derived from the normalized target and the account, not typed by the
// user, so the same origin registered twice in one account is one source.
// WorkerID, SourceID, Generation and Credential are what the gateway issued; they
// are empty until registration succeeds.
type Source struct {
	Name       string `json:"name"`
	Account    string `json:"account"`
	Target     string `json:"target"`
	DatasetID  string `json:"dataset_id"`
	WorkerID   string `json:"worker_id"`
	SourceID   string `json:"source_id"`
	Generation uint64 `json:"generation"`
	Credential string `json:"credential"`
	Enabled    bool   `json:"enabled"`
	SpecPath   string `json:"spec_path"`
}

// Registered reports whether the source carries a usable worker identity.
func (s Source) Registered() bool {
	return s.WorkerID != "" && s.SourceID != "" && s.Generation != 0 && len(s.Credential) == 64
}

// ConnectorConfig is the per-source runtime identity the connector dials with.
type ConnectorConfig struct {
	WorkerID   string `json:"worker_id"`
	SourceID   string `json:"source_id"`
	Target     string `json:"target"`
	Generation uint64 `json:"generation"`
	Credential string `json:"credential"`
}

// AsConnector converts a registered source to its runtime identity.
func (s Source) AsConnector() ConnectorConfig {
	return ConnectorConfig{
		WorkerID:   s.WorkerID,
		SourceID:   s.SourceID,
		Target:     s.Target,
		Generation: s.Generation,
		Credential: s.Credential,
	}
}

// Store is the on-disk account and source store rooted at one directory.
type Store struct {
	base string
}

// Open resolves the store under base, creating it owner-only if absent.
func Open(base string) (*Store, error) {
	if err := os.MkdirAll(base, 0o700); err != nil {
		return nil, err
	}
	return &Store{base: base}, nil
}

// Base is the directory this store owns.
func (s *Store) Base() string { return s.base }

func (s *Store) accountsPath() string { return filepath.Join(s.base, "accounts.json") }
func (s *Store) selectedPath() string { return filepath.Join(s.base, "selected") }
func (s *Store) sourcesPath() string  { return filepath.Join(s.base, "sources.json") }
func (s *Store) accountKeyPath(alias string) string {
	return filepath.Join(s.base, "accounts", alias+".key")
}

type accountsFile struct {
	Accounts []Account `json:"accounts"`
}

type sourcesFile struct {
	Sources []Source `json:"sources"`
}

// Accounts lists stored accounts sorted by alias.
func (s *Store) Accounts() ([]Account, error) {
	var file accountsFile
	if err := readJSON(s.accountsPath(), &file); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	accounts := append([]Account(nil), file.Accounts...)
	sort.Slice(accounts, func(left, right int) bool { return accounts[left].Alias < accounts[right].Alias })
	return accounts, nil
}

// Account returns one stored account by alias.
func (s *Store) Account(alias string) (Account, bool, error) {
	accounts, err := s.Accounts()
	if err != nil {
		return Account{}, false, err
	}
	for _, account := range accounts {
		if account.Alias == alias {
			return account, true, nil
		}
	}
	return Account{}, false, nil
}

// PutAccount stores an account and its scoped key. A repeat addition of the same
// account is refused rather than silently replacing a live credential; rotating
// one is a separate, explicit act.
func (s *Store) PutAccount(account Account, key string) error {
	if !accountAliasPattern.MatchString(account.Alias) {
		return fmt.Errorf("account alias %q must match %s", account.Alias, accountAliasPattern)
	}
	if strings.TrimSpace(key) == "" {
		return errors.New("an account key is required")
	}
	existing, err := s.Accounts()
	if err != nil {
		return err
	}
	for _, found := range existing {
		if found.Alias == account.Alias {
			return fmt.Errorf("account %q already exists; use an explicit rotation to change it", account.Alias)
		}
	}
	if err := writeOwnerOnly(s.accountKeyPath(account.Alias), []byte(key)); err != nil {
		return fmt.Errorf("persist account key: %w", err)
	}
	accounts := append(existing, account)
	sort.Slice(accounts, func(left, right int) bool { return accounts[left].Alias < accounts[right].Alias })
	if err := writeJSONOwnerOnly(s.accountsPath(), accountsFile{Accounts: accounts}); err != nil {
		return fmt.Errorf("persist accounts: %w", err)
	}
	if _, err := s.Selected(); err != nil {
		return err
	}
	return nil
}

// AccountKey reads one account's scoped key.
func (s *Store) AccountKey(alias string) (string, error) {
	contents, err := readOwnerOnly(s.accountKeyPath(alias))
	if err != nil {
		return "", err
	}
	key := strings.TrimSpace(string(contents))
	if key == "" {
		return "", fmt.Errorf("account %q has an empty key", alias)
	}
	return key, nil
}

// Select records the default account a source addition or connector run uses.
func (s *Store) Select(alias string) error {
	if _, found, err := s.Account(alias); err != nil {
		return err
	} else if !found {
		return fmt.Errorf("account %q is not stored", alias)
	}
	return writeOwnerOnly(s.selectedPath(), []byte(alias))
}

// Selected returns the currently selected account alias, or "" when none is set.
func (s *Store) Selected() (string, error) {
	if _, err := os.Stat(s.selectedPath()); errors.Is(err, os.ErrNotExist) {
		return "", nil
	}
	contents, err := readOwnerOnly(s.selectedPath())
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(contents)), nil
}

// ResolveAccount picks the account an operation targets: an explicit alias when
// given, otherwise the selected one. It returns the stored account so the caller
// also gets its gateway URL and username.
func (s *Store) ResolveAccount(explicit string) (Account, error) {
	alias := strings.TrimSpace(explicit)
	if alias == "" {
		selected, err := s.Selected()
		if err != nil {
			return Account{}, err
		}
		if selected == "" {
			return Account{}, errors.New("no account selected; run: source-link account select <alias>")
		}
		alias = selected
	}
	account, found, err := s.Account(alias)
	if err != nil {
		return Account{}, err
	}
	if !found {
		return Account{}, fmt.Errorf("account %q is not stored", alias)
	}
	return account, nil
}

// Sources lists every stored source, sorted by name.
func (s *Store) Sources() ([]Source, error) {
	var file sourcesFile
	if err := readJSON(s.sourcesPath(), &file); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	sources := append([]Source(nil), file.Sources...)
	sort.Slice(sources, func(left, right int) bool { return sources[left].Name < sources[right].Name })
	return sources, nil
}

// SourcesForAccount lists the sources owned by one account.
func (s *Store) SourcesForAccount(alias string) ([]Source, error) {
	all, err := s.Sources()
	if err != nil {
		return nil, err
	}
	var owned []Source
	for _, source := range all {
		if source.Account == alias {
			owned = append(owned, source)
		}
	}
	return owned, nil
}

// Source returns one source by its derived name.
func (s *Store) Source(name string) (Source, bool, error) {
	all, err := s.Sources()
	if err != nil {
		return Source{}, false, err
	}
	for _, source := range all {
		if source.Name == name {
			return source, true, nil
		}
	}
	return Source{}, false, nil
}

// PutSource inserts or replaces one source by name.
func (s *Store) PutSource(source Source) error {
	all, err := s.Sources()
	if err != nil {
		return err
	}
	replaced := false
	for index, existing := range all {
		if existing.Name == source.Name {
			all[index] = source
			replaced = true
			break
		}
	}
	if !replaced {
		all = append(all, source)
	}
	sort.Slice(all, func(left, right int) bool { return all[left].Name < all[right].Name })
	return writeJSONOwnerOnly(s.sourcesPath(), sourcesFile{Sources: all})
}

// RemoveSource deletes one source by name.
func (s *Store) RemoveSource(name string) error {
	all, err := s.Sources()
	if err != nil {
		return err
	}
	remaining := make([]Source, 0, len(all))
	for _, source := range all {
		if source.Name != name {
			remaining = append(remaining, source)
		}
	}
	return writeJSONOwnerOnly(s.sourcesPath(), sourcesFile{Sources: remaining})
}

// DeriveName builds the stable source name for one target in one account.
//
// The name is a pure function of the account alias and the normalized target, so
// the same origin added twice in one account is recognized as the same source
// rather than creating a second. A different account yields a different name for
// the same host:port, which is what makes an equal target under two owners two
// separate sources.
//
// The gateway's source alias grammar is `^[a-z][a-z0-9_.]{2,127}$`: letters,
// digits, dots and underscores, starting with a letter. Hyphens are not allowed,
// so the host's dots are preserved as separators and every other run of unsafe
// characters collapses to a single dot.
func DeriveName(accountAlias string, parsed target.Target) string {
	return normalizeName(accountAlias + "." + parsed.Host + "." + portText(parsed.Port))
}

var nameSeparator = regexp.MustCompile(`[^a-z0-9]+`)

func normalizeName(value string) string {
	cleaned := nameSeparator.ReplaceAllString(strings.ToLower(value), ".")
	cleaned = strings.Trim(cleaned, ".")
	if cleaned == "" {
		return ""
	}
	if cleaned[0] < 'a' || cleaned[0] > 'z' {
		cleaned = "s." + cleaned
	}
	return cleaned
}

func portText(port int) string {
	return fmt.Sprintf("%d", port)
}

// NewCredential generates a random hex token, used for the local source scope
// identifier the gateway does not supply.
func NewCredential() (string, error) {
	buffer := make([]byte, 16)
	if _, err := rand.Read(buffer); err != nil {
		return "", err
	}
	return hex.EncodeToString(buffer), nil
}

func readJSON(path string, out any) error {
	contents, err := readOwnerOnly(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(contents, out)
}

func readOwnerOnly(path string) ([]byte, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Mode().Perm()&0o077 != 0 {
		return nil, fmt.Errorf("%s must be a regular owner-only file (mode 0600)", path)
	}
	return os.ReadFile(path)
}

func writeJSONOwnerOnly(path string, value any) error {
	encoded, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	return writeOwnerOnly(path, encoded)
}

// writeOwnerOnly writes atomically: a sibling temp file, fsync, then rename, so a
// crash can never truncate an existing file and the result is 0600 regardless of
// the process umask.
func writeOwnerOnly(path string, contents []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	temp, err := os.CreateTemp(filepath.Dir(path), ".source-agent-*")
	if err != nil {
		return err
	}
	name := temp.Name()
	defer func() { _ = os.Remove(name) }()
	if err := temp.Chmod(0o600); err != nil {
		_ = temp.Close()
		return err
	}
	if _, err := temp.Write(contents); err != nil {
		_ = temp.Close()
		return err
	}
	if err := temp.Sync(); err != nil {
		_ = temp.Close()
		return err
	}
	if err := temp.Close(); err != nil {
		return err
	}
	return os.Rename(name, path)
}
