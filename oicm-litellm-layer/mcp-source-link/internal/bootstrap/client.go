// Package bootstrap performs the connector's first-use registration against the
// gateway's public REST surface, then persists the issued identity so later
// starts reuse it.
//
// It is deliberately separate from the tunnel: this package only speaks HTTP(S)
// REST, using the ordinary user's virtual key as its bearer, and never touches
// the service token, the pepper, or any private control route. Everything it
// writes is owner-only and atomic so a crash mid-write cannot truncate an
// existing identity.
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

	"oicm-litellm-layer/mcp-source-link/internal/config"
)

// EnrollmentInput is the operator-supplied description of the source being
// exposed. It deliberately carries no identity fields: identity is issued by the
// gateway, never computed locally.
type EnrollmentInput struct {
	Alias     string `json:"alias"`
	DatasetID string `json:"dataset_id"`
	ProofKey  string `json:"proof_key"`
	Spec      string `json:"spec"`
}

// SourceRequest is the POST /mcp-sources body. The gateway derives the owner from
// the bearer key, so no owner field is sent.
type SourceRequest struct {
	Alias  string        `json:"alias"`
	Target TargetRequest `json:"target"`
	Spec   string        `json:"spec"`
}

// TargetRequest is the loopback origin the source host exposes.
type TargetRequest struct {
	Host string `json:"host"`
	Port int    `json:"port"`
}

// SourceEnrolled mirrors the gateway's POST /mcp-sources response.
type SourceEnrolled struct {
	SourceID string `json:"source_id"`
	Owner    string `json:"owner"`
	Status   string `json:"status"`
}

// WorkerRequest is the POST /mcp-source-link-sources/{id}/workers body.
type WorkerRequest struct {
	Label     string        `json:"label"`
	DatasetID string        `json:"dataset_id"`
	ProofKey  string        `json:"proof_key"`
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

// ErrTerminalRegistration signals a gateway refusal that a retry cannot fix,
// such as a conflicting identifier or a revoked worker. Retrying it forever would
// look like a network outage while the operator sees nothing actionable, so it is
// reported distinctly and the caller stops.
var ErrTerminalRegistration = errors.New("gateway refused registration")

// Client performs enrollment and worker issuance against a fixed gateway base.
type Client struct {
	Base    *url.URL
	HTTP    *http.Client
	UserKey string
}

// New builds a client, validating that the base is HTTPS. The base is the
// gateway's public URL, the same one the tunnel dials, but joined as a REST base
// rather than a gRPC host:port.
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

func (c *Client) post(ctx context.Context, path string, body any, out any) error {
	encoded, err := json.Marshal(body)
	if err != nil {
		return err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint(path), bytes.NewReader(encoded))
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+c.UserKey)
	request.Header.Set("Content-Type", "application/json")
	response, err := c.HTTP.Do(request)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrNotYetAvailable, err)
	}
	defer response.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return err
	}
	if response.StatusCode != http.StatusCreated && response.StatusCode != http.StatusOK {
		// Never echo the Authorization header or the response body wholesale: a body
		// on an error path must not become a credential or admin-state leak.
		if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusTooManyRequests {
			return fmt.Errorf("%w: gateway %s returned %d", ErrNotYetAvailable, path, response.StatusCode)
		}
		if response.StatusCode >= 400 && response.StatusCode < 500 {
			return fmt.Errorf("%w: gateway %s returned %d", ErrTerminalRegistration, path, response.StatusCode)
		}
		return fmt.Errorf("gateway %s returned %d", path, response.StatusCode)
	}
	return json.Unmarshal(payload, out)
}

// Enroll registers the source and returns the gateway-authenticated enrollment.
func (c *Client) Enroll(ctx context.Context, input EnrollmentInput, target config.ConnectorTarget) (SourceEnrolled, error) {
	var enrolled SourceEnrolled
	body := SourceRequest{
		Alias:  input.Alias,
		Target: TargetRequest{Host: target.Host, Port: target.Port},
		Spec:   input.Spec,
	}
	if err := c.post(ctx, "/mcp-sources", body, &enrolled); err != nil {
		return enrolled, err
	}
	if enrolled.SourceID == "" || enrolled.Owner == "" {
		return enrolled, errors.New("gateway enrollment response is missing source_id or owner")
	}
	return enrolled, nil
}

// IssueWorker registers this connector instance and returns its issued identity.
func (c *Client) IssueWorker(
	ctx context.Context, sourceID string, input EnrollmentInput, target config.ConnectorTarget, label string,
) (IssuedIdentity, error) {
	var issued IssuedIdentity
	body := WorkerRequest{
		Label:     label,
		DatasetID: input.DatasetID,
		ProofKey:  input.ProofKey,
		Target:    TargetRequest{Host: target.Host, Port: target.Port},
	}
	if err := c.post(ctx, "/mcp-source-link-sources/"+url.PathEscape(sourceID)+"/workers", body, &issued); err != nil {
		return issued, err
	}
	if err := validateIssued(sourceID, issued); err != nil {
		return IssuedIdentity{}, err
	}
	return issued, nil
}

// validateIssued rejects an identity that would only fail later, on the next
// restart, after the response has already been persisted. A wrong source id, a
// non-loopback target, or a non-hex credential of the right length must never
// reach disk, or the connector would loop re-registering a broken identity.
func validateIssued(sourceID string, issued IssuedIdentity) error {
	if issued.WorkerID == "" || issued.SourceID == "" || issued.Generation == 0 {
		return errors.New("gateway worker response did not carry a complete identity")
	}
	if issued.SourceID != sourceID {
		return errors.New("gateway worker response named a different source than was enrolled")
	}
	if _, err := config.ParseTarget(issued.Target); err != nil {
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

// Register runs the whole first-use enrollment: enroll the source, then register
// this worker, returning the identity to persist.
func Register(
	ctx context.Context, client *Client, input config.BootstrapInput, target config.ConnectorTarget,
) (config.ConnectorConfig, error) {
	enrollment := EnrollmentInput{
		Alias: input.Alias, DatasetID: input.DatasetID, ProofKey: input.ProofKey, Spec: input.Spec,
	}
	enrolled, err := client.Enroll(ctx, enrollment, target)
	if err != nil {
		return config.ConnectorConfig{}, err
	}
	issued, err := client.IssueWorker(ctx, enrolled.SourceID, enrollment, target, WorkerLabel(input.Alias))
	if err != nil {
		return config.ConnectorConfig{}, err
	}
	return config.ConnectorConfig{
		WorkerID:   issued.WorkerID,
		SourceID:   issued.SourceID,
		Target:     issued.Target,
		Generation: issued.Generation,
		Credential: issued.Credential,
	}, nil
}

// WorkerLabel is the deterministic worker label for a source alias.
//
// The gateway's worker label pattern is `^[a-z][a-z0-9-]{1,31}$`: no underscores,
// 2 to 32 characters. The source alias allows underscores and 128 characters, so
// passing it straight through is rejected. Aliases that already fit are used
// verbatim, which keeps the existing deterministic label for the pod; others are
// normalized the same way every time so a restart derives the same label.
func WorkerLabel(alias string) string {
	if workerLabelPattern.MatchString(alias) {
		return alias
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
	}, alias)
	trimmed := strings.Trim(normalized, "-")
	if trimmed == "" {
		trimmed = "connector"
	}
	if len(trimmed) > 32 {
		trimmed = strings.TrimRight(trimmed[:32], "-")
	}
	if trimmed[0] < 'a' || trimmed[0] > 'z' {
		trimmed = "s" + trimmed
		if len(trimmed) > 32 {
			trimmed = trimmed[:32]
		}
	}
	// The gateway pattern requires at least two characters.
	if len(trimmed) < 2 {
		trimmed += "-1"
	}
	return trimmed
}

var workerLabelPattern = regexp.MustCompile(`^[a-z][a-z0-9-]{1,31}$`)
