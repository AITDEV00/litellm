// Package bootstrap performs a connector's source and worker registration against
// the gateway's public REST surface, then hands back the issued identity for the
// caller to persist.
//
// It is deliberately separate from the tunnel: this package only speaks HTTP(S)
// REST, using the ordinary user's virtual key as its bearer, and never touches the
// service token, the pepper, or any private control route.
//
// Registration is idempotent by design. A repeated call for a source the same
// owner already registered returns ErrAlreadyExists carrying the stored identity
// rather than creating a second source. The gateway enforces that inside its own
// transaction, so two clients racing the same registration still collapse to one.
package bootstrap

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"

	"oicm-litellm-layer/mcp-source-link/internal/state"
	"oicm-litellm-layer/mcp-source-link/internal/target"
)

// EnrollmentInput is what a source registration needs, and nothing more. The
// source name is derived from the account and target, not supplied here, and the
// origin proof key the old contract required is gone: it proved nothing a
// connector controlling both sides could not fabricate.
//
// Kind selects how the gateway will expose the origin. "openapi" (the default)
// describes an HTTP service by an OpenAPI document the gateway builds tools from,
// so Spec is required. "mcp" exposes a standard MCP server the gateway connects to
// over the MCP protocol at Path, so Spec is empty and Path is used.
type EnrollmentInput struct {
	Name      string
	DatasetID string
	Kind      string
	Path      string
	Spec      string
}

// SourceRequest is the POST /mcp-sources body.
//
// The gateway derives the owner from the bearer key, so no owner field is sent.
// The target is the origin's host and port; the gateway validates and authorizes
// it, including any non-loopback exposure. kind selects openapi vs mcp, and path
// is the MCP endpoint for an mcp source.
type SourceRequest struct {
	Alias  string        `json:"alias"`
	Target TargetRequest `json:"target"`
	Kind   string        `json:"kind"`
	Path   string        `json:"path,omitempty"`
	Spec   string        `json:"spec,omitempty"`
}

// TargetRequest is the origin a source host exposes.
type TargetRequest struct {
	Host string `json:"host"`
	Port int    `json:"port"`
}

// SourceEnrolled is the gateway's POST /mcp-sources response.
type SourceEnrolled struct {
	SourceID string `json:"source_id"`
	Owner    string `json:"owner"`
	Status   string `json:"status"`
}

// WorkerRequest is the POST /mcp-source-link-sources/{id}/workers body.
type WorkerRequest struct {
	Label     string        `json:"label"`
	DatasetID string        `json:"dataset_id"`
	Target    TargetRequest `json:"target"`
}

// IssuedIdentity mirrors the gateway's worker response and is exactly the on-disk
// connector identity shape, so it can be persisted without translation.
type IssuedIdentity struct {
	WorkerID   string `json:"worker_id"`
	SourceID   string `json:"source_id"`
	Target     string `json:"target"`
	Generation uint64 `json:"generation"`
	Credential string `json:"credential"`
}

// ErrNotYetAvailable signals the gateway could not be reached or the user key is
// not installed yet. The caller backs off and retries rather than exiting.
var ErrNotYetAvailable = errors.New("gateway enrollment not yet available")

// ErrTerminalRegistration signals a gateway refusal that a retry cannot fix, such
// as a conflicting identifier or a revoked worker.
var ErrTerminalRegistration = errors.New("gateway refused registration")

// ErrAlreadyExists signals that this owner already registered this exact source.
// Identity is the stored registration, so the caller can adopt it instead of
// creating a duplicate.
type ErrAlreadyExists struct {
	Identity state.ConnectorConfig
}

func (e ErrAlreadyExists) Error() string {
	return "MCP source already exists as " + e.Identity.SourceID
}

// Client performs registration against a fixed gateway base.
type Client struct {
	Base    *url.URL
	HTTP    *http.Client
	UserKey string
}

// New builds a client, validating that the base is HTTPS.
func New(gateway string, userKey string, httpClient *http.Client) (*Client, error) {
	if strings.TrimSpace(userKey) == "" {
		return nil, errors.New("the connector requires the user's gateway account key")
	}
	parsed, err := url.Parse(gateway)
	if err != nil {
		return nil, err
	}
	if parsed.Scheme != "https" {
		return nil, errors.New("gateway base must be https")
	}
	return &Client{Base: parsed, HTTP: httpClient, UserKey: userKey}, nil
}

func (c *Client) endpoint(path string) string {
	base := *c.Base
	base.Path = strings.TrimSuffix(base.Path, "/") + "/" + strings.TrimPrefix(path, "/")
	base.RawQuery = ""
	base.Fragment = ""
	return base.String()
}

// post sends one request and decodes the response.
func (c *Client) post(ctx context.Context, path string, body any, out any) (int, error) {
	encoded, err := json.Marshal(body)
	if err != nil {
		return 0, err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint(path), bytes.NewReader(encoded))
	if err != nil {
		return 0, err
	}
	request.Header.Set("Authorization", "Bearer "+c.UserKey)
	request.Header.Set("Content-Type", "application/json")
	response, err := c.HTTP.Do(request)
	if err != nil {
		return 0, fmt.Errorf("%w: %v", ErrNotYetAvailable, err)
	}
	defer response.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return 0, err
	}
	if response.StatusCode != http.StatusCreated && response.StatusCode != http.StatusOK {
		// Never echo the Authorization header or the response body wholesale: a body
		// on an error path must not become a credential or admin-state leak.
		if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusTooManyRequests {
			return response.StatusCode, fmt.Errorf("%w: gateway %s returned %d", ErrNotYetAvailable, path, response.StatusCode)
		}
		if response.StatusCode >= 400 && response.StatusCode < 500 {
			return response.StatusCode, fmt.Errorf("%w: gateway %s returned %d", ErrTerminalRegistration, path, response.StatusCode)
		}
		return response.StatusCode, fmt.Errorf("gateway %s returned %d", path, response.StatusCode)
	}
	return response.StatusCode, json.Unmarshal(payload, out)
}

// Enroll registers the source and returns the gateway-authenticated enrollment.
func (c *Client) Enroll(ctx context.Context, input EnrollmentInput, origin target.Target) (SourceEnrolled, error) {
	var enrolled SourceEnrolled
	body := SourceRequest{
		Alias:  input.Name,
		Target: TargetRequest{Host: origin.Host, Port: origin.Port},
		Kind:   input.kindOrDefault(),
		Path:   input.Path,
		Spec:   input.Spec,
	}
	if _, err := c.post(ctx, "/mcp-sources", body, &enrolled); err != nil {
		return enrolled, err
	}
	if enrolled.SourceID == "" || enrolled.Owner == "" {
		return enrolled, errors.New("gateway enrollment response is missing source_id or owner")
	}
	return enrolled, nil
}

// kindOrDefault reports the transport kind, defaulting to openapi.
func (i EnrollmentInput) kindOrDefault() string {
	if i.Kind == "" {
		return "openapi"
	}
	return i.Kind
}

// IssueWorker registers this connector instance and returns its issued identity.
func (c *Client) IssueWorker(
	ctx context.Context, sourceID string, input EnrollmentInput, origin target.Target, label string,
) (IssuedIdentity, error) {
	var issued IssuedIdentity
	body := WorkerRequest{
		Label:     label,
		DatasetID: input.DatasetID,
		Target:    TargetRequest{Host: origin.Host, Port: origin.Port},
	}
	if _, err := c.post(ctx, "/mcp-source-link-sources/"+url.PathEscape(sourceID)+"/workers", body, &issued); err != nil {
		return issued, err
	}
	if err := validateIssued(sourceID, issued); err != nil {
		return IssuedIdentity{}, err
	}
	return issued, nil
}

// validateIssued rejects an identity that would only fail later, on the next
// restart, after the response has already been persisted.
func validateIssued(sourceID string, issued IssuedIdentity) error {
	if issued.WorkerID == "" || issued.SourceID == "" || issued.Generation == 0 {
		return errors.New("gateway worker response did not carry a complete identity")
	}
	if issued.SourceID != sourceID {
		return errors.New("gateway worker response named a different source than was enrolled")
	}
	if _, err := target.Parse(issued.Target); err != nil {
		return fmt.Errorf("gateway worker response carried an invalid target: %w", err)
	}
	if len(issued.Credential) != 64 {
		return errors.New("gateway worker credential is not 64 characters")
	}
	if _, err := hex.DecodeString(issued.Credential); err != nil {
		return errors.New("gateway worker credential is not hexadecimal")
	}
	return nil
}

// Register runs the whole enrollment for one source: enroll the source, then
// register this worker, returning the issued identity.
func Register(
	ctx context.Context, client *Client, input EnrollmentInput, origin target.Target,
) (state.ConnectorConfig, error) {
	enrolled, err := client.Enroll(ctx, input, origin)
	if err != nil {
		return state.ConnectorConfig{}, err
	}
	issued, err := client.IssueWorker(ctx, enrolled.SourceID, input, origin, WorkerLabel(input.Name))
	if err != nil {
		return state.ConnectorConfig{}, err
	}
	return state.ConnectorConfig{
		WorkerID:   issued.WorkerID,
		SourceID:   issued.SourceID,
		Target:     issued.Target,
		Generation: issued.Generation,
		Credential: issued.Credential,
	}, nil
}

// workerLabelPattern is the gateway's worker label grammar: no underscores, 2 to
// 32 characters.
var workerLabelPattern = regexp.MustCompile(`^[a-z][a-z0-9-]{1,31}$`)

// WorkerLabel is the deterministic worker label for a source name.
//
// The name is already derived from the account and target, so it is stable; this
// only makes it fit the gateway's label rule. Names that already fit are used
// verbatim, which keeps the label deterministic across restarts.
func WorkerLabel(name string) string {
	if workerLabelPattern.MatchString(name) {
		return name
	}
	normalized := strings.Map(func(r rune) rune {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9', r == '-':
			return r
		case r >= 'A' && r <= 'Z':
			return r + ('a' - 'A')
		default:
			return '-'
		}
	}, name)
	trimmed := strings.Trim(normalized, "-")
	// The gateway label must start with a letter and be at least two characters.
	// A name that starts with a digit, or is too short, is prefixed so the result
	// always matches; this is deterministic, so a restart derives the same label.
	if trimmed == "" || trimmed[0] < 'a' || trimmed[0] > 'z' || len(trimmed) < 2 {
		trimmed = "s-" + trimmed
	}
	if len(trimmed) > 32 {
		trimmed = strings.Trim(trimmed[:32], "-")
	}
	return trimmed
}
