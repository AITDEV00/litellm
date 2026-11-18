// Package auth performs the connector's CLI login against the gateway.
//
// It exchanges an ordinary user's username and password for a durable,
// route-scoped virtual key, using the gateway's dedicated password endpoint. It
// deliberately does not use the dashboard login, whose session JWT is a
// short-lived UI cookie rather than a credential a daemon can hold.
package auth

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
)

// LoginClient is the seam the setup flow depends on, so tests can supply a fake
// gateway without a network.
type LoginClient interface {
	Password(ctx context.Context, gatewayURL, username, password string) (string, error)
}

// PasswordLogin is the real client. It talks only to the gateway's public HTTPS
// base and never logs the password or the issued key.
type PasswordLogin struct {
	HTTP *http.Client
}

// Password authenticates and returns the issued user key.
func (p PasswordLogin) Password(ctx context.Context, gatewayURL, username, password string) (string, error) {
	body, err := json.Marshal(map[string]string{"username": username, "password": password})
	if err != nil {
		return "", err
	}
	endpoint := strings.TrimSuffix(gatewayURL, "/") + "/sso/cli/password"
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	request.Header.Set("Content-Type", "application/json")
	client := p.HTTP
	if client == nil {
		client = http.DefaultClient
	}
	response, err := client.Do(request)
	if err != nil {
		return "", fmt.Errorf("sign in to the gateway: %w", err)
	}
	defer response.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return "", err
	}
	switch response.StatusCode {
	case http.StatusOK:
	case http.StatusUnauthorized, http.StatusForbidden, http.StatusTooManyRequests:
		// The body is an error detail, never a credential. Surface the status only,
		// so a mis-typed password cannot be echoed back through the terminal.
		return "", fmt.Errorf("sign in failed (%s); check your username and password", response.Status)
	default:
		return "", fmt.Errorf("sign in failed (%s)", response.Status)
	}
	var result struct {
		Key string `json:"key"`
	}
	if err := json.Unmarshal(payload, &result); err != nil {
		return "", errors.New("the gateway login response was not valid JSON")
	}
	if result.Key == "" {
		return "", errors.New("the gateway login response did not include a credential")
	}
	return result.Key, nil
}
