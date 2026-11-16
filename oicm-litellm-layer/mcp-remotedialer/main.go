package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math/rand/v2"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"sync"
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
	transport := environment("MCP_REMOTEDIALER_TRANSPORT")
	if transport == "" {
		transport = transportRemotedialer
	}
	if transport != transportRemotedialer && transport != transportReverseH2 {
		return fmt.Errorf("unsupported transport %q", transport)
	}
	if mode == "connector" {
		return runConnector(ctx, environment("MCP_REMOTEDIALER_CONNECTOR_CONFIG"), transport)
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
	control, err := newControl(ctx, cfg, client)
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
	if cfg.Transport == transportReverseH2 {
		reverse := reverseServer()
		reverse.Addr = cfg.ReverseListen
		reverseMux := http.NewServeMux()
		reverseMux.HandleFunc("POST /mcp-remotedialer-reverse", runtime.reverseRegister)
		reverseMux.HandleFunc("POST /mcp-remotedialer-hold", runtime.reverseHold)
		reverseMux.HandleFunc("POST /mcp-remotedialer-dispatch", runtime.reverseDispatch)
		reverseMux.HandleFunc("POST /mcp-remotedialer-respond", runtime.reverseRespond)
		reverse.Handler = reverseMux
		protocols := new(http.Protocols)
		protocols.SetUnencryptedHTTP2(true)
		protocols.SetHTTP1(false)
		reverse.Protocols = protocols
		servers = append(servers, reverse)
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
	if cfg.Transport == transportReverseH2 {
		log.Printf("reverse-h2 worker listening on %s; private dispatch on %s; reverse ingress on %s", cfg.WorkerListen, cfg.DataListen, cfg.ReverseListen)
	} else {
		log.Printf("remotedialer worker listening on %s; private dispatch on %s", cfg.WorkerListen, cfg.DataListen)
	}
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

func runConnector(ctx context.Context, path string, transport string) error {
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
	tlsConfig, err := httpTLS(ca)
	if err != nil {
		return err
	}
	headers := http.Header{
		"X-Source-Member":     {cfg.WorkerID},
		"X-Source-Generation": {fmt.Sprint(cfg.Generation)},
		"Authorization":       {"Bearer " + cfg.Credential},
	}
	if transport == transportReverseH2 {
		return runReverseConnector(ctx, cfg, gateway, tlsConfig, headers)
	}
	wsGateway := "wss://" + strings.TrimPrefix(gateway, "https://")
	dialer := &websocket.Dialer{Proxy: http.ProxyFromEnvironment, TLSClientConfig: tlsConfig, HandshakeTimeout: 8 * time.Second}
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

func runReverseConnector(ctx context.Context, cfg connectorConfig, gateway string, tlsConfig *tls.Config, headers http.Header) error {
	base := strings.TrimSuffix(gateway, "/")
	client := &http.Client{
		Transport: &http.Transport{
			TLSClientConfig:     tlsConfig,
			ForceAttemptHTTP2:   true,
			TLSHandshakeTimeout: 8 * time.Second,
			IdleConnTimeout:     90 * time.Second,
			MaxIdleConns:        4,
			DisableCompression:  true,
		},
	}
	local := &net.Dialer{Timeout: 4 * time.Second, KeepAlive: 30 * time.Second}

	origin := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return local.DialContext(ctx, "tcp", cfg.Target)
		},
		IdleConnTimeout: 90 * time.Second, DisableCompression: true, ResponseHeaderTimeout: 30 * time.Second,
	}
	defer origin.CloseIdleConnections()
	delay := 300 * time.Millisecond
	for ctx.Err() == nil {
		started := time.Now()
		err := reverseTunnelSession(ctx, client, base, headers, origin)
		if ctx.Err() != nil {
			return nil
		}
		if time.Since(started) > 30*time.Second {
			delay = 300 * time.Millisecond
		}
		log.Printf("reverse connector %s disconnected: %v", cfg.WorkerID, err)
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

func reversePost(ctx context.Context, client *http.Client, url string, headers http.Header, body []byte) (*http.Response, error) {
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, reader)
	if err != nil {
		return nil, err
	}
	req.Header = headers.Clone()
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	return client.Do(req)
}

func reverseTunnelSession(ctx context.Context, client *http.Client, base string, headers http.Header, origin *http.Transport) error {
	response, err := reversePost(ctx, client, base+"/mcp-remotedialer-reverse", headers, nil)
	if err != nil {
		return err
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4096))
	_ = response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("reverse registration returned HTTP %d", response.StatusCode)
	}
	holdCtx, holdCancel := context.WithCancel(ctx)
	defer holdCancel()
	go func() {
		resp, err := reversePost(holdCtx, client, base+"/mcp-remotedialer-hold", headers, nil)
		if err == nil {
			_ = resp.Body.Close()
		}
		holdCancel()
	}()
	const dispatchers = 8
	var workers sync.WaitGroup
	lost := make(chan struct{}, 1)
	for range dispatchers {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for {
				select {
				case <-holdCtx.Done():
					return
				case <-lost:
					return
				default:
				}
				resp, err := reversePost(holdCtx, client, base+"/mcp-remotedialer-dispatch", headers, nil)
				if err != nil || resp.StatusCode != http.StatusOK {
					if resp != nil {
						_ = resp.Body.Close()
					}
					select {
					case lost <- struct{}{}:
					default:
					}
					return
				}
				var item dispatchRequest
				if err := json.NewDecoder(resp.Body).Decode(&item); err != nil {
					_ = resp.Body.Close()
					continue
				}
				_ = resp.Body.Close()
				payload, err := executeDispatch(holdCtx, origin, item)
				if err != nil {
					payload = dispatchResponse{ID: item.ID, Error: err.Error()}
				}
				encoded, err := json.Marshal(payload)
				if err != nil {
					continue
				}
				done, err := reversePost(holdCtx, client, base+"/mcp-remotedialer-respond", headers, encoded)
				if err != nil {
					select {
					case lost <- struct{}{}:
					default:
					}
					_ = done
					return
				}
				_, _ = io.Copy(io.Discard, io.LimitReader(done.Body, 4096))
				_ = done.Body.Close()
			}
		}()
	}
	workers.Wait()
	return nil
}

func executeDispatch(ctx context.Context, origin *http.Transport, item dispatchRequest) (dispatchResponse, error) {
	target := url.URL{Scheme: "http", Host: "target.source.invalid", Path: item.Path, RawQuery: item.Query}
	req, err := http.NewRequestWithContext(ctx, item.Method, target.String(), bytes.NewReader(item.Body))
	if err != nil {
		return dispatchResponse{}, err
	}
	for name, values := range item.Headers {
		for _, value := range values {
			req.Header.Add(name, value)
		}
	}
	resp, err := origin.RoundTrip(req)
	if err != nil {
		return dispatchResponse{}, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, reverseMaxBody))
	if err != nil {
		return dispatchResponse{}, err
	}
	headers := make(map[string][]string, len(resp.Header))
	for name, values := range resp.Header {
		headers[name] = values
	}
	return dispatchResponse{ID: item.ID, Status: resp.StatusCode, Headers: headers, Body: body}, nil
}
