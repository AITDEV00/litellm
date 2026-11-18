// Package wire owns the dispatch envelope types and the protocol and size
// limits both transports share. It imports nothing from the other slices so
// either plane can depend on it without coupling to the other.
package wire

import (
	"bytes"
	"errors"
	"io"
	"net/http"
	"strconv"
)

const (
	// StreamWindow and StreamLimit size one multiplexed HTTP/2 stream.
	StreamWindow = 64 << 10
	StreamLimit  = 1 << 17

	PingTimeout   = 15 // seconds
	SendPingAfter = 30 // seconds

	ConnectionBuffer = 1 << 20

	// ResponseDeadline bounds how long a dispatch may wait for the connector.
	ResponseDeadlineSeconds = 90

	// MaxBody bounds one serialized dispatch envelope on the wire.
	MaxBody = 1 << 20

	// MaxOriginBody bounds the request or origin-response payload carried
	// inside an envelope. The envelope base64-inflates the payload by 4/3
	// and adds the id, status, and header fields, so this cap keeps the
	// whole envelope under MaxBody.
	MaxOriginBody = 760 << 10
)

// StripFramingHeaders are response headers that describe the original
// connection and must not be replayed onto a new one.
var StripFramingHeaders = [...]string{"Content-Length", "Transfer-Encoding", "Connection", "Keep-Alive", "Trailer"}

// DispatchRequest is one forwarded HTTP request, serialized by the relay onto
// a pending connector dispatch stream.
type DispatchRequest struct {
	ID      string              `json:"id"`
	Method  string              `json:"method"`
	Path    string              `json:"path"`
	Query   string              `json:"query,omitempty"`
	Headers map[string][]string `json:"headers,omitempty"`
	Body    []byte              `json:"body,omitempty"`
}

// DispatchResponse is one origin HTTP response, posted back by the connector
// on a new stream and correlated by ID.
type DispatchResponse struct {
	ID      string              `json:"id"`
	Status  int                 `json:"status"`
	Headers map[string][]string `json:"headers,omitempty"`
	Body    []byte              `json:"body,omitempty"`
	Error   string              `json:"error,omitempty"`
}

// HTTPResponse converts the envelope back into an HTTP response.
func (r DispatchResponse) HTTPResponse() (*http.Response, error) {
	if r.Error != "" {
		return nil, errors.New(r.Error)
	}
	header := make(http.Header, len(r.Headers))
	for name, values := range r.Headers {
		header[name] = values
	}
	for _, name := range StripFramingHeaders {
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
