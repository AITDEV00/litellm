package main

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
	"strconv"
	"strings"
	"time"
)

type config struct {
	ControlURL   string
	ServiceToken string
	WorkerListen string
	DataListen   string
	CAFile       string
}

type peer struct {
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

type healthReport struct {
	WorkerID           string  `json:"worker_id"`
	Generation         uint64  `json:"generation"`
	CatalogFingerprint *string `json:"catalog_fingerprint"`
}

type connectorConfig struct {
	WorkerID   string `json:"worker_id"`
	SourceID   string `json:"source_id"`
	Target     string `json:"target"`
	Generation uint64 `json:"generation"`
	Credential string `json:"credential"`
}

func environment(name string) string {
	return strings.TrimSpace(os.Getenv(name))
}

func loadConfig() (config, error) {
	cfg := config{
		ControlURL:   environment("MCP_REMOTEDIALER_CONTROL_URL"),
		ServiceToken: environment("MCP_SOURCE_BROKER_SERVICE_TOKEN"),
		WorkerListen: environment("MCP_REMOTEDIALER_WORKER_LISTEN"),
		DataListen:   environment("MCP_REMOTEDIALER_DATA_LISTEN"),
		CAFile:       environment("MCP_REMOTEDIALER_CA_FILE"),
	}
	if cfg.ControlURL == "" || len(cfg.ServiceToken) < 32 {
		return cfg, errors.New("control URL and service token are required")
	}
	for _, address := range []string{cfg.WorkerListen, cfg.DataListen} {
		host, port, err := net.SplitHostPort(address)
		if err != nil || (host != "0.0.0.0" && host != "127.0.0.1") || port == "" {
			return cfg, errors.New("worker and data listeners must be numeric addresses")
		}
	}
	return cfg, nil
}

func httpTLS(ca string) (*tls.Config, error) {
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

func controlClient(cfg config) (*http.Client, error) {
	tlsConfig, err := httpTLS(cfg.CAFile)
	if err != nil {
		return nil, err
	}
	return &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{TLSClientConfig: tlsConfig}}, nil
}

func readConnector(path string) (connectorConfig, error) {
	var cfg connectorConfig
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

func slot(sourceID string) string {
	return sourceID + ".source.invalid:1"
}

func address(listen string) string {
	return fmt.Sprintf("http://%s", listen)
}
