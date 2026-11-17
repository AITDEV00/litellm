package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

var errReverseClosed = errors.New("reverse session closed")

const (
	reverseStreamWindow     = 64 << 10
	reverseStreamLimit      = 1 << 17
	reversePingTimeout      = 15 * time.Second
	reverseSendPingAfter    = 30 * time.Second
	reverseProbeInterval    = 15 * time.Second
	reverseResponseDeadline = 90 * time.Second
	reverseMaxBody          = 1 << 20
	// reverseMaxOriginBody bounds the request and origin-response payload
	// carried inside a dispatch envelope. The envelope base64-inflates the
	// payload by 4/3 and adds the id, status, and header fields, so the
	// payload cap must keep the whole envelope under reverseMaxBody.
	reverseMaxOriginBody    = 760 << 10
	reverseConnectionBuffer = 1 << 20
)

func reverseHTTP2Config() *http.HTTP2Config {
	return &http.HTTP2Config{
		MaxConcurrentStreams:          reverseStreamLimit,
		MaxReceiveBufferPerStream:     reverseStreamWindow,
		MaxReceiveBufferPerConnection: reverseConnectionBuffer,
		SendPingTimeout:               reverseSendPingAfter,
		PingTimeout:                   reversePingTimeout,
	}
}

func reverseServer() *http.Server {
	return &http.Server{
		ReadHeaderTimeout: 5 * time.Second,
		MaxHeaderBytes:    32 << 10,
		HTTP2:             reverseHTTP2Config(),
	}
}

// dispatchRequest is one forwarded HTTP request, serialized by the relay
// onto a pending connector dispatch stream.
type dispatchRequest struct {
	ID      string              `json:"id"`
	Method  string              `json:"method"`
	Path    string              `json:"path"`
	Query   string              `json:"query,omitempty"`
	Headers map[string][]string `json:"headers,omitempty"`
	Body    []byte              `json:"body,omitempty"`
}

// dispatchResponse is one origin HTTP response, posted back by the
// connector on a new stream and correlated by ID.
type dispatchResponse struct {
	ID      string              `json:"id"`
	Status  int                 `json:"status"`
	Headers map[string][]string `json:"headers,omitempty"`
	Body    []byte              `json:"body,omitempty"`
	Error   string              `json:"error,omitempty"`
}

func (r dispatchResponse) httpResponse() (*http.Response, error) {
	if r.Error != "" {
		return nil, errors.New(r.Error)
	}
	header := make(http.Header)
	for name, values := range r.Headers {
		header[name] = values
	}
	for _, name := range []string{"Content-Length", "Transfer-Encoding", "Connection", "Keep-Alive", "Trailer"} {
		header.Del(name)
	}
	return &http.Response{
		Status:        strconv.Itoa(r.Status),
		StatusCode:    r.Status,
		Header:        header,
		Body:          io.NopCloser(bytes.NewReader(r.Body)),
		ContentLength: int64(len(r.Body)),
	}, nil
}

type reverseSession struct {
	workerID   string
	sourceID   string
	generation uint64
	deadline   time.Duration
	closeOnce  sync.Once
	closed     chan struct{}
	onClose    func()
	mu         sync.Mutex
	pending    chan dispatchRequest
	inflight   map[string]chan dispatchResponse
	lastProbe  time.Time
}

var reverseSerial atomic.Uint64

func (s *reverseSession) close() {
	s.closeOnce.Do(func() {
		close(s.closed)
		s.mu.Lock()
		inflight := s.inflight
		s.inflight = nil
		s.mu.Unlock()
		for _, waiter := range inflight {
			select {
			case waiter <- dispatchResponse{Error: "reverse session closed"}:
			default:
			}
		}
		s.onClose()
	})
}

func (s *reverseSession) ready(sourceID string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return !s.lastProbe.IsZero() && time.Since(s.lastProbe) < 2*reverseProbeInterval && s.sourceID == sourceID
}

// roundTrip serializes one request, queues it for the next connector
// dispatch, and blocks until the connector posts the origin response.
func (s *reverseSession) roundTrip(req *http.Request) (*http.Response, error) {
	var body []byte
	if req.Body != nil {
		read, err := io.ReadAll(io.LimitReader(req.Body, reverseMaxOriginBody+1))
		if err != nil {
			return nil, err
		}
		if len(read) > reverseMaxOriginBody {
			return nil, errors.New("reverse dispatch request exceeds the body limit")
		}
		body = read
		_ = req.Body.Close()
	}
	headers := make(map[string][]string, len(req.Header))
	for name, values := range req.Header {
		headers[name] = values
	}
	serial := reverseSerial.Add(1)
	item := dispatchRequest{
		ID:      fmt.Sprintf("%d-%d", time.Now().UnixNano(), serial),
		Method:  req.Method,
		Path:    req.URL.Path,
		Query:   req.URL.RawQuery,
		Headers: headers,
		Body:    body,
	}
	waiter := make(chan dispatchResponse, 1)
	s.mu.Lock()
	select {
	case <-s.closed:
		s.mu.Unlock()
		return nil, errReverseClosed
	default:
	}
	s.inflight[item.ID] = waiter
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		delete(s.inflight, item.ID)
		s.mu.Unlock()
	}()
	select {
	case <-s.closed:
		return nil, errReverseClosed
	case <-req.Context().Done():
		return nil, req.Context().Err()
	case s.pending <- item:
	default:
		return nil, errors.New("reverse dispatch queue saturated")
	}
	select {
	case <-s.closed:
		return nil, errReverseClosed
	case <-req.Context().Done():
		return nil, req.Context().Err()
	case resp := <-waiter:
		return resp.httpResponse()
	case <-time.After(s.deadline):
		return nil, errors.New("reverse dispatch exceeded its deadline")
	}
}

// take hands the next queued request to a connector dispatch stream.
func (s *reverseSession) take(ctx context.Context) (dispatchRequest, error) {
	select {
	case <-ctx.Done():
		return dispatchRequest{}, ctx.Err()
	case <-s.closed:
		return dispatchRequest{}, errReverseClosed
	case item := <-s.pending:
		return item, nil
	}
}

// deliver correlates a connector response with its waiting request.
func (s *reverseSession) deliver(resp dispatchResponse) bool {
	s.mu.Lock()
	waiter := s.inflight[resp.ID]
	if waiter != nil {
		delete(s.inflight, resp.ID)
	}
	s.mu.Unlock()
	if waiter == nil {
		return false
	}
	waiter <- resp
	return true
}
