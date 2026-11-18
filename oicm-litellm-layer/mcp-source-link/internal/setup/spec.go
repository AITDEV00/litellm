package setup

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"oicm-litellm-layer/mcp-source-link/internal/target"
)

// maxOpenAPIDocument bounds the fetched document so a hostile or misconfigured
// origin cannot stream unbounded bytes into the connector, which then go to the
// gateway. An OpenAPI document for a real service is far under this.
const maxOpenAPIDocument = 8 << 20

// openAPIPath is where an origin publishes its own document, per the OpenAPI
// convention. It is the path a human would otherwise save to a file by hand.
const openAPIPath = "/openapi.json"

// SpecFetcher returns the OpenAPI document bytes for an origin. It is a seam so a
// test drives the registration with a fixed document instead of standing up an
// origin, and so the real fetch stays one small function.
type SpecFetcher func(ctx context.Context, origin target.Target) ([]byte, error)

// HTTPOpenAPIFetcher fetches `<origin>/openapi.json` over plain HTTP.
//
// The origin is local to the connector, which is the only party that can reach
// it, and it is dialed directly over HTTP; the tunnel is for the gateway, not for
// this fetch. The Host header carries the target's authority so an origin bound
// to a Kubernetes service name still routes the request to itself. A non-2xx
// status is a hard failure: a source with no document cannot be described as an
// openapi source, so it is better to stop than to register an empty catalog.
func HTTPOpenAPIFetcher(client *http.Client) SpecFetcher {
	return func(ctx context.Context, origin target.Target) ([]byte, error) {
		url := openAPIURL(origin)
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			return nil, fmt.Errorf("build OpenAPI request for %s: %w", url, err)
		}
		request.Host = origin.Authority()
		response, err := client.Do(request)
		if err != nil {
			return nil, fmt.Errorf("fetch %s: %w", url, err)
		}
		defer func() { _ = response.Body.Close() }()
		if response.StatusCode < 200 || response.StatusCode > 299 {
			return nil, fmt.Errorf("fetch %s: origin returned HTTP %d", url, response.StatusCode)
		}
		body, err := io.ReadAll(io.LimitReader(response.Body, maxOpenAPIDocument+1))
		if err != nil {
			return nil, fmt.Errorf("read %s: %w", url, err)
		}
		if len(body) > maxOpenAPIDocument {
			return nil, fmt.Errorf("openapi document at %s exceeds %d bytes", url, maxOpenAPIDocument)
		}
		if len(strings.TrimSpace(string(body))) == 0 {
			return nil, fmt.Errorf("openapi document at %s is empty", url)
		}
		return body, nil
	}
}

// defaultSpecFetcher is the real fetch, dialing the origin over a short timeout so
// an unreachable origin surfaces as a clear error instead of a stall.
func defaultSpecFetcher() SpecFetcher {
	return HTTPOpenAPIFetcher(&http.Client{Timeout: 10 * time.Second})
}

// openAPIURL is the URL a fetch reads, recorded on the source so a later run can
// say where the document came from without re-deriving it.
func openAPIURL(origin target.Target) string {
	return "http://" + origin.String() + openAPIPath
}

// resolveOpenAPIDocument returns the document bytes and what to record as the
// source's spec path: the given file when specPath is set, else the origin's own
// /openapi.json through fetch. A file wins so an operator can still pin an exact
// reviewed document.
func resolveOpenAPIDocument(
	ctx context.Context,
	origin target.Target,
	specPath string,
	fetch SpecFetcher,
) (specBytes []byte, resolvedSource string, err error) {
	if trimmed := strings.TrimSpace(specPath); trimmed != "" {
		document, readErr := os.ReadFile(trimmed)
		if readErr != nil {
			return nil, "", fmt.Errorf("read OpenAPI document %q: %w", trimmed, readErr)
		}
		return document, trimmed, nil
	}
	if fetch == nil {
		return nil, "", errors.New("no OpenAPI document given and no fetcher configured")
	}
	document, fetchErr := fetch(ctx, origin)
	if fetchErr != nil {
		return nil, "", fmt.Errorf("no --spec given and %s did not serve %s: %w",
			origin.String(), openAPIPath, fetchErr)
	}
	return document, openAPIURL(origin), nil
}
