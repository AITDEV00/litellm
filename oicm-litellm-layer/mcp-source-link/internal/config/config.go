// Package config owns process configuration: environment-derived worker settings,
// the control-plane peer records both planes read, and TLS client construction.
//
// The connector identity files it used to read are gone: accounts and sources now
// live in the state package, which is the single owner of on-disk connector state.
// What remains here is the worker's own runtime configuration and the shared peer
// projection the control plane reports.
package config

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

// Config is the worker's process configuration.
type Config struct {
	ControlURL    string
	ServiceToken  string
	DataListen    string
	ReverseListen string
	CAFile        string
}

// Peer is one connector as the control plane reports it.
type Peer struct {
	WorkerID           string  `json:"worker_id"`
	SourceID           string  `json:"source_id"`
	Label              string  `json:"label"`
	Target             string  `json:"target"`
	DatasetID          string  `json:"dataset_id"`
	ProofKey           string  `json:"proof_key"`
	Generation         uint64  `json:"generation"`
	Phase              string  `json:"phase"`
	CredentialDigest   string  `json:"credential_digest"`
	ExpectedCatalog    string  `json:"expected_catalog"`
	CatalogFingerprint *string `json:"catalog_fingerprint"`
}

// HealthReport is the probe outcome posted back to the control plane.
type HealthReport struct {
	WorkerID           string  `json:"worker_id"`
	Generation         uint64  `json:"generation"`
	CatalogFingerprint *string `json:"catalog_fingerprint"`
}

// ConnectorConfig is the per-source runtime identity a connector dials with.
type ConnectorConfig struct {
	WorkerID   string `json:"worker_id"`
	SourceID   string `json:"source_id"`
	Target     string `json:"target"`
	Generation uint64 `json:"generation"`
	Credential string `json:"credential"`
}

// Environment returns a trimmed environment value, treating blank as unset.
func Environment(name string) string {
	return strings.TrimSpace(os.Getenv(name))
}

// Insecure reports whether TLS certificate verification is disabled.
//
// This exists for dev gateways fronted by a self-signed certificate, where the
// operator has no CA to hand the binary. It is off by default and must never be
// set against a production gateway, because it accepts any certificate.
func Insecure() bool {
	value := strings.ToLower(Environment("MCP_SOURCE_LINK_INSECURE"))
	return value == "1" || value == "true" || value == "yes"
}

// ValidateGatewayURL enforces the https/host/no-userinfo contract shared by
// registration and tunnel startup, so both paths accept exactly the same values.
func ValidateGatewayURL(value string) (string, error) {
	parsed, err := url.Parse(strings.TrimSpace(value))
	if err != nil {
		return "", fmt.Errorf("gateway URL: %w", err)
	}
	if parsed.Scheme != "https" {
		return "", errors.New("gateway URL must be https")
	}
	if parsed.Host == "" {
		return "", errors.New("gateway URL must include a host")
	}
	if parsed.User != nil {
		return "", errors.New("gateway URL must not embed credentials")
	}
	if parsed.Path != "" && parsed.Path != "/" {
		return "", errors.New("gateway URL must not include a path")
	}
	if parsed.RawQuery != "" || parsed.Fragment != "" {
		return "", errors.New("gateway URL must not include a query or fragment")
	}
	return strings.TrimSuffix(parsed.String(), "/"), nil
}

// Load reads and validates the worker configuration.
func Load() (Config, error) {
	cfg := Config{
		ControlURL:    Environment("MCP_SOURCE_LINK_CONTROL_URL"),
		ServiceToken:  Environment("MCP_SOURCE_BROKER_SERVICE_TOKEN"),
		DataListen:    Environment("MCP_SOURCE_LINK_DATA_LISTEN"),
		ReverseListen: Environment("MCP_SOURCE_LINK_REVERSE_LISTEN"),
		CAFile:        Environment("MCP_SOURCE_LINK_CA_FILE"),
	}
	if cfg.ControlURL == "" || len(cfg.ServiceToken) < 32 {
		return cfg, errors.New("control URL and service token are required")
	}
	if cfg.ReverseListen == "" {
		return cfg, errors.New("a tunnel listener address is required")
	}
	for _, address := range []string{cfg.DataListen, cfg.ReverseListen} {
		host, port, err := net.SplitHostPort(address)
		if err != nil || (host != "0.0.0.0" && host != "127.0.0.1") || port == "" {
			return cfg, errors.New("data and reverse listeners must be numeric addresses")
		}
	}
	return cfg, nil
}

// TLS builds the client TLS configuration, trusting CAFile when set.
//
// Insecure mode (MCP_SOURCE_LINK_INSECURE) skips certificate verification for a
// dev gateway with a self-signed certificate, where there is no CA to trust.
func TLS(ca string) (*tls.Config, error) {
	config := &tls.Config{MinVersion: tls.VersionTLS12}
	if Insecure() {
		config.InsecureSkipVerify = true
		return config, nil
	}
	if ca == "" {
		return config, nil
	}
	contents, err := os.ReadFile(ca)
	if err != nil {
		return nil, err
	}
	roots, err := x509.SystemCertPool()
	if err != nil {
		return nil, err
	}
	if !roots.AppendCertsFromPEM(contents) {
		return nil, errors.New("CA file contains no certificates")
	}
	config.RootCAs = roots
	return config, nil
}

// ControlClient is the HTTP client used against the control plane.
func ControlClient(cfg Config) (*http.Client, error) {
	tlsConfig, err := TLS(cfg.CAFile)
	if err != nil {
		return nil, err
	}
	return &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{TLSClientConfig: tlsConfig}}, nil
}

// HTTPSClient builds an HTTP client that trusts CAFile when set.
//
// An unreadable or certificate-less CA is returned as an error rather than
// downgraded to system roots. Falling back silently would surface the
// misconfiguration later as an opaque TLS handshake failure against the gateway,
// which reads as a network problem instead of the bad CA path it actually is.
func HTTPSClient(ca string) (*http.Client, error) {
	tlsConfig, err := TLS(ca)
	if err != nil {
		return nil, err
	}
	return &http.Client{Timeout: 15 * time.Second, Transport: &http.Transport{TLSClientConfig: tlsConfig}}, nil
}

// Slot is the synthetic host that identifies one source to the worker data plane.
func Slot(sourceID string) string {
	return sourceID + ".source.invalid:1"
}
