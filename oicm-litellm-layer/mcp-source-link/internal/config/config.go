// Package config owns process configuration: environment-derived worker
// settings, the connector's on-disk identity, and the control-plane peer
// records both planes read. It performs validation only and makes no
// network calls.
package config

import (
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"os"
	"strconv"
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

// ConnectorConfig is the connector's on-disk identity file.
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
func TLS(ca string) (*tls.Config, error) {
	config := &tls.Config{MinVersion: tls.VersionTLS12}
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

// ReadConnector loads and validates the connector identity file. The file
// must be owner-only and name a numeric loopback target.
func ReadConnector(path string) (ConnectorConfig, error) {
	var cfg ConnectorConfig
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 {
		return cfg, errors.New("connector configuration must be a regular owner-only file")
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		return cfg, err
	}
	if err := json.Unmarshal(contents, &cfg); err != nil {
		return cfg, err
	}
	host, port, err := net.SplitHostPort(cfg.Target)
	number, portErr := strconv.ParseUint(port, 10, 16)
	if err != nil || portErr != nil || number == 0 || !net.ParseIP(host).IsLoopback() {
		return cfg, errors.New("connector target must be an explicit numeric loopback address")
	}
	if cfg.WorkerID == "" || cfg.SourceID == "" || cfg.Generation == 0 || len(cfg.Credential) != 64 {
		return cfg, errors.New("invalid connector identity")
	}
	if _, err := hex.DecodeString(cfg.Credential); err != nil {
		return cfg, errors.New("invalid connector credential")
	}
	return cfg, nil
}

// Slot is the synthetic host that identifies one source to the proxy.
func Slot(sourceID string) string {
	return sourceID + ".source.invalid:1"
}
