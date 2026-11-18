// Package setup owns the connector's interactive commands: adding an account,
// adding a source, and the small amount of prompting each needs.
//
// It follows the shape of the MinIO `mc` client. Adding an account asks only for
// a gateway URL, a username and a password; the password is exchanged once for a
// scoped user key and never stored. Adding a source reuses the selected account's
// stored key, so a second source never asks for a password again.
//
// The package is terminal-facing only. It does no HTTP itself; the gateway login
// and the registration both go through typed clients so tests can drive them with
// a fake terminal and a fake gateway.
package setup

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"

	"golang.org/x/term"

	"oicm-litellm-layer/mcp-source-link/internal/auth"
	"oicm-litellm-layer/mcp-source-link/internal/bootstrap"
	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/state"
	"oicm-litellm-layer/mcp-source-link/internal/target"
)

// Source kinds. "openapi" is an HTTP service described by an OpenAPI document the
// gateway builds tools from. "mcp" is a standard MCP server the gateway connects to
// over the MCP protocol at the source's path, with no catalog.
const (
	KindOpenAPI = "openapi"
	KindMCP     = "mcp"
)

// sourceKind normalizes a requested kind, defaulting to openapi. Anything other
// than an explicit "mcp" is openapi, so the existing behavior is unchanged.
func sourceKind(value string) string {
	if strings.EqualFold(strings.TrimSpace(value), KindMCP) {
		return KindMCP
	}
	return KindOpenAPI
}

// Prompter abstracts the terminal so tests can drive it without a TTY.
type Prompter interface {
	Line(prompt string) (string, error)
	LineDefault(prompt, defaultValue string) (string, error)
	Secret(prompt string) (string, error)
}

// Terminal is the real Prompter. Password entry goes through x/term so it is not
// echoed; line entry is plain buffered stdin.
type Terminal struct {
	In  *os.File
	Out io.Writer
}

// Line reads one trimmed line.
func (t Terminal) Line(prompt string) (string, error) {
	return t.LineDefault(prompt, "")
}

// LineDefault reads one trimmed line, returning defaultValue when the answer is
// empty. This is what makes a derived default acceptable to press Enter on.
func (t Terminal) LineDefault(prompt, defaultValue string) (string, error) {
	if defaultValue != "" {
		prompt = fmt.Sprintf("%s [%s]: ", prompt, defaultValue)
	}
	if _, err := fmt.Fprint(t.Out, prompt); err != nil {
		return "", err
	}
	line, err := bufio.NewReader(t.In).ReadString('\n')
	if err != nil && !errors.Is(err, io.EOF) {
		return "", err
	}
	value := strings.TrimSpace(line)
	if value == "" {
		return defaultValue, nil
	}
	return value, nil
}

// Secret reads a line with terminal echo disabled.
func (t Terminal) Secret(prompt string) (string, error) {
	if _, err := fmt.Fprint(t.Out, prompt); err != nil {
		return "", err
	}
	raw, err := term.ReadPassword(int(t.In.Fd()))
	if _, werr := fmt.Fprintln(t.Out); werr != nil && err == nil {
		return "", werr
	}
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(raw)), nil
}

// IsInteractive reports whether stdin is a terminal.
func IsInteractive() bool {
	return term.IsTerminal(int(os.Stdin.Fd()))
}

// AddAccount performs the whole account login: prompt for gateway, username and
// password, exchange them for a scoped key, and persist the account.
//
// It asks nothing else. The alias names the local account and is what later
// commands select; it is never derived from the gateway.
func AddAccount(
	ctx context.Context, store *state.Store, prompt Prompter, login auth.LoginClient, alias string,
) (state.Account, error) {
	if strings.TrimSpace(alias) == "" {
		value, err := prompt.Line("Account alias (local name, e.g. work): ")
		if err != nil {
			return state.Account{}, err
		}
		alias = value
	}
	if _, found, err := store.Account(alias); err != nil {
		return state.Account{}, err
	} else if found {
		return state.Account{}, fmt.Errorf("account %q already exists", alias)
	}

	gatewayURL, err := prompt.Line("Gateway URL (https://...): ")
	if err != nil {
		return state.Account{}, err
	}
	gatewayURL, err = config.ValidateGatewayURL(gatewayURL)
	if err != nil {
		return state.Account{}, err
	}
	username, err := prompt.Line("Username: ")
	if err != nil {
		return state.Account{}, err
	}
	if username == "" {
		return state.Account{}, errors.New("a username is required")
	}
	password, err := prompt.Secret("Password (not echoed): ")
	if err != nil {
		return state.Account{}, err
	}
	if password == "" {
		return state.Account{}, errors.New("a password is required")
	}

	key, err := login.Password(ctx, gatewayURL, username, password)
	password = ""
	if err != nil {
		return state.Account{}, err
	}
	account := state.Account{Alias: alias, GatewayURL: gatewayURL, Username: username}
	if err := store.PutAccount(account, key); err != nil {
		return state.Account{}, err
	}
	selected, err := store.Selected()
	if err != nil {
		return state.Account{}, err
	}
	if selected == "" {
		if err := store.Select(alias); err != nil {
			return state.Account{}, err
		}
	}
	return account, nil
}

// AddSourceInput carries everything AddSource needs that is not prompted.
//
// Kind is "openapi" (default) or "mcp". An mcp source needs neither a spec nor a
// spec file, only an optional Path for its MCP endpoint (default /mcp). An openapi
// source uses SpecPath when given, else Fetcher pulls the origin's own
// /openapi.json, so a local file is optional rather than required.
type AddSourceInput struct {
	Account   string
	Name      string
	DatasetID string
	Kind      string
	Path      string
	Target    string
	SpecPath  string
	Fetcher   SpecFetcher
}

// AlreadyExists reports that the requested source is already registered under the
// same derived name. It carries the existing source so the caller can show its
// identity. It is an error so a caller cannot mistake a no-op for a new source;
// the CLI prints it as a clear result rather than a failure.
type AlreadyExists struct {
	Source state.Source
}

func (a AlreadyExists) Error() string {
	return "MCP source already exists as " + a.Source.Name
}

// AddSource registers one origin under an existing account and persists the
// identity. It never prompts for a gateway URL, username or password: it uses the
// selected account, or an explicit one, and that account's stored key.
//
// The target is the only thing a human must supply. The source name and dataset
// id are derived from the account and target unless explicitly given.
func AddSource(
	ctx context.Context,
	store *state.Store,
	prompt Prompter,
	gw *bootstrap.Client,
	input AddSourceInput,
) (state.Source, error) {
	return addSource(ctx, store, prompt, gw, input, DefaultRetryPolicy())
}

func addSource(
	ctx context.Context,
	store *state.Store,
	prompt Prompter,
	gw *bootstrap.Client,
	input AddSourceInput,
	policy RetryPolicy,
) (state.Source, error) {
	account, err := store.ResolveAccount(input.Account)
	if err != nil {
		return state.Source{}, err
	}
	targetText := strings.TrimSpace(input.Target)
	if targetText == "" {
		targetText, err = prompt.Line("Target (127.0.0.1:port or service.namespace.svc:port): ")
		if err != nil {
			return state.Source{}, err
		}
	}
	origin, err := target.Parse(targetText)
	if err != nil {
		return state.Source{}, err
	}
	name := strings.TrimSpace(input.Name)
	if name == "" {
		name = state.DeriveName(account.Alias, origin)
	}
	datasetID := strings.TrimSpace(input.DatasetID)
	if datasetID == "" {
		datasetID = name
	}
	// A source with this derived name already exists locally: it is the same
	// target in the same account. Return the existing registration without a
	// network call, so a repeat never creates a second gateway row or worker.
	if existing, found, err := store.Source(name); err != nil {
		return state.Source{}, err
	} else if found {
		return existing, AlreadyExists{Source: existing}
	}
	specPath := strings.TrimSpace(input.SpecPath)
	kind := sourceKind(input.Kind)
	mcpPath := strings.TrimSpace(input.Path)
	if mcpPath == "" {
		mcpPath = "/mcp"
	}
	var spec []byte
	specSource := ""
	if kind == KindOpenAPI {
		fetcher := input.Fetcher
		if fetcher == nil {
			fetcher = defaultSpecFetcher()
		}
		spec, specSource, err = resolveOpenAPIDocument(ctx, origin, specPath, fetcher)
		if err != nil {
			return state.Source{}, err
		}
	}

	key, err := store.AccountKey(account.Alias)
	if err != nil {
		return state.Source{}, err
	}
	client := gw
	if client == nil {
		httpClient, err := config.HTTPSClient(config.Environment("MCP_SOURCE_LINK_CA_FILE"))
		if err != nil {
			return state.Source{}, err
		}
		client, err = bootstrap.New(account.GatewayURL, key, httpClient)
		if err != nil {
			return state.Source{}, err
		}
	}

	enrollment := bootstrap.EnrollmentInput{
		Name: name, DatasetID: datasetID, Kind: kind, Path: mcpPath, Spec: string(spec),
	}
	identity, err := registerWithRetry(ctx, policy, func() (state.ConnectorConfig, error) {
		return bootstrap.Register(ctx, client, enrollment, origin)
	})
	if err != nil {
		return state.Source{}, err
	}
	source := state.Source{
		Name:       name,
		Account:    account.Alias,
		Target:     origin.String(),
		DatasetID:  datasetID,
		Kind:       kind,
		Path:       mcpPath,
		WorkerID:   identity.WorkerID,
		SourceID:   identity.SourceID,
		Generation: identity.Generation,
		Credential: identity.Credential,
		Enabled:    true,
		SpecPath:   specSource,
	}
	if err := store.PutSource(source); err != nil {
		return state.Source{}, err
	}
	return source, nil
}

// AddSourceWithRetry is AddSource with an injectable retry policy, for tests that
// must not sleep.
func AddSourceWithRetry(
	ctx context.Context,
	store *state.Store,
	prompt Prompter,
	gw *bootstrap.Client,
	input AddSourceInput,
	policy RetryPolicy,
) (state.Source, error) {
	return addSource(ctx, store, prompt, gw, input, policy)
}

// ListAccounts returns the account listing a human sees: aliases, usernames and
// gateway URLs, and which one is selected. It never includes key material.
func ListAccounts(store *state.Store) (string, error) {
	accounts, err := store.Accounts()
	if err != nil {
		return "", err
	}
	selected, err := store.Selected()
	if err != nil {
		return "", err
	}
	if len(accounts) == 0 {
		return "no accounts configured; run: adma account add <alias>\n", nil
	}
	var builder strings.Builder
	for _, account := range accounts {
		marker := " "
		if account.Alias == selected {
			marker = "*"
		}
		fmt.Fprintf(&builder, "%s %-12s %-40s %s\n", marker, account.Alias, account.GatewayURL, account.Username)
	}
	return builder.String(), nil
}

// ListSources returns the source listing, sorted by name.
func ListSources(store *state.Store) (string, error) {
	sources, err := store.Sources()
	if err != nil {
		return "", err
	}
	if len(sources) == 0 {
		return "no sources configured; run: adma source add\n", nil
	}
	sort.Slice(sources, func(left, right int) bool { return sources[left].Name < sources[right].Name })
	var builder strings.Builder
	for _, source := range sources {
		status := "unregistered"
		if source.Registered() {
			status = "registered"
			if !source.Enabled {
				status = "disabled"
			}
		}
		kind := source.Kind
		if kind == "" {
			kind = KindOpenAPI
		}
		fmt.Fprintf(&builder, "%-40s %-12s %-8s %-30s %s\n", source.Name, source.Account, kind, source.Target, status)
	}
	return builder.String(), nil
}
