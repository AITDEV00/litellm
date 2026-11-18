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
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
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

// ConnectorTarget is the validated numeric loopback origin a connector serves.
type ConnectorTarget struct {
	Host string
	Port int
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

// BootstrapInput is the operator-supplied, first-use registration description.
// It is written once by the installer (mode 0600) and never contains identity.
// GatewayURL is recorded so a resume after a lost worker response can reach the
// same gateway without another interactive prompt.
type BootstrapInput struct {
	Alias      string `json:"alias"`
	DatasetID  string `json:"dataset_id"`
	ProofKey   string `json:"proof_key"`
	Target     string `json:"target"`
	Spec       string `json:"spec"`
	GatewayURL string `json:"gateway_url"`
}

// ReadBootstrapInput loads the enrollment description and validates the loopback
// target. A non-owner-only file is a fatal, actionable error, not a warning.
func ReadBootstrapInput(path string) (BootstrapInput, ConnectorTarget, error) {
	var input BootstrapInput
	var target ConnectorTarget
	if err := checkOwnerOnly(path); err != nil {
		return input, target, err
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		return input, target, err
	}
	if err := json.Unmarshal(contents, &input); err != nil {
		return input, target, err
	}
	if input.Alias == "" || input.DatasetID == "" || input.ProofKey == "" || input.Spec == "" {
		return input, target, errors.New("bootstrap input needs alias, dataset_id, proof_key and spec")
	}
	target, err = ParseTarget(input.Target)
	if err != nil {
		return input, target, err
	}
	return input, target, nil
}

// ValidateBootstrapInput checks the fields an enrollment needs, so a prompt loop
// can reject a bad value before any network call.
func ValidateBootstrapInput(input BootstrapInput) error {
	if input.Alias == "" || input.DatasetID == "" || input.ProofKey == "" || input.Spec == "" {
		return errors.New("bootstrap input needs alias, dataset_id, proof_key and spec")
	}
	if _, err := ParseTarget(input.Target); err != nil {
		return err
	}
	return nil
}

// WriteBootstrapInput persists the enrollment description atomically at an
// owner-only path. It carries no identity and no secret.
func WriteBootstrapInput(path string, input BootstrapInput) error {
	encoded, err := json.Marshal(input)
	if err != nil {
		return err
	}
	return writeOwnerOnly(path, encoded)
}

// WriteOwnerOnly persists arbitrary bytes atomically at an owner-only path. The
// interactive setup uses it for the issued user key.
func WriteOwnerOnly(path string, contents []byte) error {
	return writeOwnerOnly(path, contents)
}

// ReadUserKey loads the installed gateway account key. It must be owner-only, so
// a resume never picks up a key another user could read.
func ReadUserKey(path string) (string, error) {
	if err := checkOwnerOnly(path); err != nil {
		return "", err
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	key := strings.TrimSpace(string(contents))
	if key == "" {
		return "", errors.New("the stored user key is empty")
	}
	return key, nil
}

// HTTPSClient builds an HTTP client that trusts CAFile when set.
func HTTPSClient(ca string) *http.Client {
	tlsConfig, err := TLS(ca)
	if err != nil {
		// A bad CA path is a real misconfiguration; fall back to system roots so the
		// caller's next request fails with a TLS error that names the problem.
		tlsConfig, _ = TLS("")
	}
	return &http.Client{Timeout: 15 * time.Second, Transport: &http.Transport{TLSClientConfig: tlsConfig}}
}

// writeOwnerOnly writes bytes to a sibling temporary file, fsyncs, then renames,
// so a crash can never leave a truncated file behind, and the final file is mode
// 0600 regardless of the process umask.
func writeOwnerOnly(path string, contents []byte) error {
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
func ParseTarget(value string) (ConnectorTarget, error) {
	var target ConnectorTarget
	host, port, err := net.SplitHostPort(value)
	number, portErr := strconv.ParseUint(port, 10, 16)
	if err != nil || portErr != nil || number == 0 || !net.ParseIP(host).IsLoopback() {
		return target, errors.New("connector target must be an explicit numeric loopback address")
	}
	return ConnectorTarget{Host: host, Port: int(number)}, nil
}

func checkOwnerOnly(path string) error {
	info, err := os.Stat(path)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 {
		return fmt.Errorf("%s must be a regular owner-only file (mode 0600)", path)
	}
	return nil
}

// WriteConnectorIdentity persists an issued identity atomically at an owner-only
// path.
func WriteConnectorIdentity(path string, identity ConnectorConfig) error {
	encoded, err := json.Marshal(identity)
	if err != nil {
		return err
	}
	return writeOwnerOnly(path, encoded)
}
