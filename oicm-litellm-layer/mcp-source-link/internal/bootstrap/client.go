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
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
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
	if issued.WorkerID == "" || issued.SourceID == "" || issued.Generation == 0 || len(issued.Credential) != 64 {
		return issued, errors.New("gateway worker response did not carry a complete identity")
	}
	return issued, nil
}
