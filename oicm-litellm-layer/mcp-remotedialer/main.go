package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"math/rand/v2"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
	"github.com/rancher/remotedialer"
)

func main() {
	remotedialer.PrintTunnelData = false
	if len(os.Args) != 2 || (os.Args[1] != "worker" && os.Args[1] != "connector") {
		log.Fatal("usage: mcp-source-link worker|connector")
	}
	if err := run(os.Args[1]); err != nil {
		log.Fatal(err)
	}
}

func run(mode string) error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if mode == "connector" {
		return runConnector(ctx, environment("MCP_REMOTEDIALER_CONNECTOR_CONFIG"))
	}
	cfg, err := loadConfig()
	if err != nil {
		return err
	}
	client, err := controlClient(cfg)
	if err != nil {
		return err
	}
	defer client.CloseIdleConnections()
	control, err := newControl(cfg, client)
	if err != nil {
		return err
	}
	runtime := newRuntime(control)
	worker := http.NewServeMux()
	worker.HandleFunc("GET /mcp-remotedialer-worker", runtime.worker)
	data := http.NewServeMux()
	data.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	data.HandleFunc("POST /v1/remotedialer/revoke", runtime.revokeHTTP)
	data.HandleFunc("/sources/{source}/{path...}", runtime.forward)
	servers := []*http.Server{
		{Addr: cfg.WorkerListen, Handler: worker, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 90 * time.Second, MaxHeaderBytes: 32 << 10},
		{Addr: cfg.DataListen, Handler: data, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 90 * time.Second, MaxHeaderBytes: 32 << 10},
	}
	for _, server := range servers {
		listener, err := net.Listen("tcp", server.Addr)
		if err != nil {
			return err
		}
		go func(server *http.Server) {
			if err := server.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
				log.Printf("listener stopped: %v", err)
				stop()
			}
		}(server)
	}
	go func() {
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				_ = control.refresh(ctx)
				runtime.reconcile()
			}
		}
	}()
	log.Printf("remotedialer worker listening on %s; private dispatch on %s", cfg.WorkerListen, cfg.DataListen)
	<-ctx.Done()
	shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	runtime.mu.Lock()
	for _, member := range runtime.sessions {
		member.close()
	}
	runtime.mu.Unlock()
	for _, server := range servers {
		_ = server.Shutdown(shutdown)
	}
	return nil
}

func runConnector(ctx context.Context, path string) error {
	remotedialer.PrintTunnelData = false
	cfg, err := readConnector(path)
	if err != nil {
		return err
	}
	gateway := environment("MCP_REMOTEDIALER_PUBLIC_URL")
	ca := environment("MCP_REMOTEDIALER_CA_FILE")
	if !strings.HasPrefix(gateway, "https://") {
		return errors.New("worker URL must use approved HTTPS")
	}
	wsGateway := "wss://" + strings.TrimPrefix(gateway, "https://")
	tlsConfig, err := httpTLS(ca)
	if err != nil {
		return err
	}
	dialer := &websocket.Dialer{Proxy: http.ProxyFromEnvironment, TLSClientConfig: tlsConfig, HandshakeTimeout: 8 * time.Second}
	headers := http.Header{
		"X-Source-Member":     {cfg.WorkerID},
		"X-Source-Generation": {fmt.Sprint(cfg.Generation)},
		"Authorization":       {"Bearer " + cfg.Credential},
	}
	local := &net.Dialer{Timeout: 4 * time.Second, KeepAlive: 30 * time.Second}
	allowed := func(network, address string) bool { return network == "tcp" && address == slot(cfg.SourceID) }
	dial := func(ctx context.Context, network, address string) (net.Conn, error) {
		if !allowed(network, address) {
			return nil, errors.New("target denied by connector")
		}
		return local.DialContext(ctx, "tcp", cfg.Target)
	}
	delay := 300 * time.Millisecond
	for ctx.Err() == nil {
		started := time.Now()
		err := remotedialer.ConnectToProxyWithDialer(ctx, wsGateway, headers, allowed, dialer, dial, nil)
		if ctx.Err() != nil {
			return nil
		}
		if time.Since(started) > 30*time.Second {
			delay = 300 * time.Millisecond
		}
		log.Printf("connector %s disconnected: %v", cfg.WorkerID, err)
		timer := time.NewTimer(delay + time.Duration(rand.Int64N(int64(delay/2))))
		select {
		case <-ctx.Done():
			timer.Stop()
		case <-timer.C:
		}
		delay = min(15*time.Second, delay*2)
	}
	return nil
}
