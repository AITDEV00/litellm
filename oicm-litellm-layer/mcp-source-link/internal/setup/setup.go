// Package setup owns the connector's interactive initialization.
//
// It is the only place that asks a human for anything. Everything it collects
// is turned into the same artifacts the non-interactive path uses: an owner-only
// user-key file, an owner-only bootstrap input, and (after the gateway issues it)
// an owner-only identity. No secret is written to a log, echoed, or passed as a
// command argument.
//
// The package is deliberately terminal-facing only. It does no HTTP itself; the
// gateway login and the enrollment both go through typed clients so tests can
// drive them with a fake terminal and a fake gateway.
package setup

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/term"

	"oicm-litellm-layer/mcp-source-link/internal/auth"
	"oicm-litellm-layer/mcp-source-link/internal/bootstrap"
	"oicm-litellm-layer/mcp-source-link/internal/config"
)

// Paths is the on-disk layout initialization writes and later reads.
type Paths struct {
	UserKey    string
	Bootstrap  string
	Identity   string
	SpecSource string
}

// PathsFor resolves the standard layout under one base directory, so a test can
// point it at a temporary HOME.
func PathsFor(base string) Paths {
	return Paths{
		UserKey:   filepath.Join(base, "user.key"),
		Bootstrap: filepath.Join(base, "bootstrap.json"),
		Identity:  filepath.Join(base, "identity.json"),
	}
}

// Prompter abstracts the terminal so tests can drive it without a TTY.
type Prompter interface {
	Line(prompt string) (string, error)
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
	if _, err := fmt.Fprint(t.Out, prompt); err != nil {
		return "", err
	}
	reader := bufio.NewReader(t.In)
	line, err := reader.ReadString('\n')
	if err != nil && !errors.Is(err, io.EOF) {
		return "", err
	}
	return strings.TrimSpace(line), nil
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

// IsInteractive reports whether stdin is a terminal. Initialization without one
// must not read stdin, because under systemd it would block forever.
func IsInteractive() bool {
	return term.IsTerminal(int(os.Stdin.Fd()))
}

// Answers is what the prompts collected. It carries the password only long
// enough to make the login request.
type Answers struct {
	GatewayURL string
	Username   string
	Password   string
	Alias      string
	DatasetID  string
	Target     string
	ProofKey   string
	SpecPath   string
}

// Initialize runs the whole first-use setup: authenticate, read and validate the
// OpenAPI document, enroll, register the worker, and persist everything at mode
// 0600. It returns the identity so the caller can start the tunnel.
//
// A failed first attempt must not force a second interactive run. If the earlier
// run already persisted the user key and the bootstrap input, only the issued
// identity is missing, so this resumes registration with the stored input and
// stored key instead of prompting again or minting a second key. Every case
// starts from what is actually on disk, so a permanent conflict is never masked
// by an earlier attempt's in-process state.
func Initialize(ctx context.Context, paths Paths, prompt Prompter, login auth.LoginClient, gw *bootstrap.Client) (
	config.ConnectorConfig, error,
) {
	if existing, err := config.ReadConnector(paths.Identity); err == nil {
		return existing, nil
	}

	stored, target, err := config.ReadBootstrapInput(paths.Bootstrap)
	if err != nil {
		return InitializeFresh(ctx, paths, prompt, login, gw)
	}
	key, err := config.ReadUserKey(paths.UserKey)
	if err != nil {
		return InitializeFresh(ctx, paths, prompt, login, gw)
	}
	if stored.GatewayURL == "" {
		return InitializeFresh(ctx, paths, prompt, login, gw)
	}
	client, err := gatewayClient(gw, stored.GatewayURL, key)
	if err != nil {
		return config.ConnectorConfig{}, err
	}
	identity, err := bootstrap.Register(ctx, client, stored, target)
	if err != nil {
		return config.ConnectorConfig{}, err
	}
	if err := config.WriteConnectorIdentity(paths.Identity, identity); err != nil {
		return config.ConnectorConfig{}, fmt.Errorf("persist connector identity: %w", err)
	}
	return identity, nil
}

// InitializeFresh is the from-scratch prompt-and-register path. It is separate so
// resume reuses exactly the same post-login sequence.
func InitializeFresh(ctx context.Context, paths Paths, prompt Prompter, login auth.LoginClient, gw *bootstrap.Client) (
	config.ConnectorConfig, error,
) {
	answers, err := collect(prompt, paths.SpecSource)
	if err != nil {
		return config.ConnectorConfig{}, err
	}
	defer func() { answers.Password = "" }()

	spec, err := os.ReadFile(answers.SpecPath)
	if err != nil {
		return config.ConnectorConfig{}, fmt.Errorf("read OpenAPI document %q: %w", answers.SpecPath, err)
	}
	target, err := config.ParseTarget(answers.Target)
	if err != nil {
		return config.ConnectorConfig{}, err
	}
	input := config.BootstrapInput{
		Alias:      answers.Alias,
		DatasetID:  answers.DatasetID,
		ProofKey:   answers.ProofKey,
		Target:     answers.Target,
		Spec:       string(spec),
		GatewayURL: answers.GatewayURL,
	}
	if err := config.ValidateBootstrapInput(input); err != nil {
		return config.ConnectorConfig{}, err
	}

	base, err := url.Parse(answers.GatewayURL)
	if err != nil {
		return config.ConnectorConfig{}, fmt.Errorf("gateway URL: %w", err)
	}
	if base.Scheme != "https" {
		return config.ConnectorConfig{}, errors.New("the gateway URL must start with https://")
	}
	key, err := login.Password(ctx, answers.GatewayURL, answers.Username, answers.Password)
	if err != nil {
		return config.ConnectorConfig{}, err
	}
	answers.Password = ""

	if err := config.WriteOwnerOnly(paths.UserKey, []byte(key)); err != nil {
		return config.ConnectorConfig{}, fmt.Errorf("persist user key: %w", err)
	}
	if err := config.WriteBootstrapInput(paths.Bootstrap, input); err != nil {
		return config.ConnectorConfig{}, fmt.Errorf("persist bootstrap input: %w", err)
	}

	client, err := gatewayClient(gw, answers.GatewayURL, key)
	if err != nil {
		return config.ConnectorConfig{}, err
	}
	identity, err := bootstrap.Register(ctx, client, input, target)
	if err != nil {
		return config.ConnectorConfig{}, err
	}
	if err := config.WriteConnectorIdentity(paths.Identity, identity); err != nil {
		return config.ConnectorConfig{}, fmt.Errorf("persist connector identity: %w", err)
	}
	return identity, nil
}

func gatewayClient(gw *bootstrap.Client, gatewayURL string, key string) (*bootstrap.Client, error) {
	if gw != nil {
		return gw, nil
	}
	return bootstrap.New(gatewayURL, key, config.HTTPSClient(""))
}

func collect(prompt Prompter, defaultSpec string) (Answers, error) {
	var answers Answers
	fields := []struct {
		target *string
		label  string
	}{
		{&answers.GatewayURL, "Gateway URL (https://...): "},
		{&answers.Username, "Gateway username: "},
		{&answers.Alias, "Source name: "},
		{&answers.DatasetID, "Dataset ID: "},
		{&answers.Target, "Local loopback target (127.0.0.1:port): "},
		{&answers.ProofKey, "Origin proof public key (base64 Ed25519): "},
	}
	for _, field := range fields {
		value, err := prompt.Line(field.label)
		if err != nil {
			return answers, err
		}
		if value == "" {
			return answers, fmt.Errorf("%s is required", strings.TrimSuffix(field.label, ": "))
		}
		*field.target = value
	}
	specPrompt := "Path to your service's OpenAPI document: "
	if defaultSpec != "" {
		specPrompt = "Path to your service's OpenAPI document [" + defaultSpec + "]: "
	}
	specPath, err := prompt.Line(specPrompt)
	if err != nil {
		return answers, err
	}
	if specPath == "" {
		specPath = defaultSpec
	}
	if specPath == "" {
		return answers, errors.New("an OpenAPI document path is required")
	}
	answers.SpecPath = specPath
	password, err := prompt.Secret("Gateway password (not echoed): ")
	if err != nil {
		return answers, err
	}
	if password == "" {
		return answers, errors.New("a password is required")
	}
	answers.Password = password
	return answers, nil
}
