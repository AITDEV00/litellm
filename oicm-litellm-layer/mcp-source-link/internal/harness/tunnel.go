package harness

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"math/big"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"

	"github.com/jhump/grpctunnel"
	"github.com/jhump/grpctunnel/tunnelpb"

	"oicm-litellm-layer/mcp-source-link/dispatchpb"
	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/connector"
	"oicm-litellm-layer/mcp-source-link/internal/listen"
	"oicm-litellm-layer/mcp-source-link/internal/tunnel"
)

// TunnelServer stands up the tunnel plane exactly as production wires it: one
// h2c listener serving the tunnel gRPC service. It returns the listener address.
func (f *Fixture) TunnelServer(t *testing.T) string {
	t.Helper()
	server := listen.Server()
	server.Handler = tunnel.HTTPHandler(f.Tunnels.GRPCServer())
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = server.Close() })
	t.Cleanup(f.Tunnels.CloseAll)
	go func() { _ = server.Serve(listener) }()
	return listener.Addr().String()
}

// TLSGateway serves the same tunnel gRPC handler over TLS and returns its
// https URL plus the CA bundle path a connector must trust. The shipped
// connector refuses any gateway URL that is not https, so a test driving the
// real binary cannot point it at the plaintext TunnelServer.
func (f *Fixture) TLSGateway(t *testing.T) (string, string) {
	t.Helper()
	certificate, caPath := selfSignedCert(t)
	server := listen.Server()
	server.Handler = tunnel.HTTPHandler(f.Tunnels.GRPCServer())
	// listen.Server negotiates h2c, which is meaningless under TLS; ServeTLS
	// needs a protocol set that permits HTTPS/2, so replace it.
	server.Protocols = httpsProtocols()
	server.TLSConfig = &tls.Config{Certificates: []tls.Certificate{certificate}}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = server.Close() })
	t.Cleanup(f.Tunnels.CloseAll)
	go func() { _ = server.ServeTLS(listener, "", "") }()
	return "https://" + listener.Addr().String(), caPath
}

// httpsProtocols enables HTTP/2 over TLS and refuses HTTP/1.1, matching what a
// TLS front for the tunnel-only listener must accept.
func httpsProtocols() *http.Protocols {
	protocols := new(http.Protocols)
	protocols.SetHTTP2(true)
	protocols.SetHTTP1(false)
	return protocols
}

// selfSignedCert mints a certificate valid for 127.0.0.1 and writes the CA
// bundle to a temp file, returning both.
func selfSignedCert(t *testing.T) (tls.Certificate, string) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "127.0.0.1"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
		IPAddresses:           []net.IP{net.ParseIP("127.0.0.1")},
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		IsCA:                  true,
		BasicConstraintsValid: true,
	}
	der, err := x509.CreateCertificate(rand.Reader, &template, &template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	certificate, err := tls.X509KeyPair(certPEM, pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER}))
	if err != nil {
		t.Fatal(err)
	}
	caPath := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(caPath, certPEM, 0o600); err != nil {
		t.Fatal(err)
	}
	return certificate, caPath
}

// RunTunnelConnector connects one connector and serves pushed RPCs until the
// context ends, mirroring the shipped reconnect loop.
func RunTunnelConnector(ctx context.Context, t *testing.T, target string, p config.Peer, origin *http.Transport) {
	t.Helper()
	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Errorf("connector dial failed: %v", err)
		return
	}
	t.Cleanup(func() { _ = conn.Close() })
	if origin == nil {
		origin = &http.Transport{
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "tcp", p.Target)
			},
			DisableCompression: true, ResponseHeaderTimeout: 30 * time.Second,
		}
		t.Cleanup(origin.CloseIdleConnections)
	}
	server := connector.NewServer(origin, authorityOf(t, p.Target))
	tunnelServer := grpctunnel.NewReverseTunnelServer(tunnelpb.NewTunnelServiceClient(conn))
	dispatchpb.RegisterDispatchServiceServer(tunnelServer, server)
	sessionCtx := metadata.AppendToOutgoingContext(ctx,
		"x-source-member", p.WorkerID,
		"x-source-generation", fmt.Sprint(p.Generation),
		"authorization", NotSecret,
	)
	for ctx.Err() == nil {
		_, _ = tunnelServer.Serve(sessionCtx)
		if ctx.Err() != nil {
			return
		}
		timer := time.NewTimer(300 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
		case <-timer.C:
		}
	}
}

// ReadyTunnel reports whether a member is admitted and its catalog verified.
func (f *Fixture) ReadyTunnel(t *testing.T, id string) bool {
	t.Helper()
	session := f.Runtime.Existing(id)
	if session == nil || !session.Ready(SourceID) {
		return false
	}
	f.RefreshPeers()
	peer, ok := f.Control.Peer(id)
	return ok && peer.CatalogFingerprint != nil && *peer.CatalogFingerprint == peer.ExpectedCatalog
}
