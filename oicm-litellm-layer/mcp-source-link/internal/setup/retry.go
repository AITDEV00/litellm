package setup

import (
	"context"
	"errors"
	"fmt"
	"time"

	"oicm-litellm-layer/mcp-source-link/internal/bootstrap"
	"oicm-litellm-layer/mcp-source-link/internal/state"
)

// RetryPolicy is the bounded backoff applied to transient registration failures.
// Exposed as a value so a test can drive the loop without sleeping.
type RetryPolicy struct {
	Attempts int
	Backoff  func(attempt int) time.Duration
	Sleep    func(d time.Duration)
}

// DefaultRetryPolicy retries a handful of times with a short doubling delay. It is
// bounded on purpose: a gateway that is restarting recovers within a second or
// two, and anything longer is operator action, not something a daemon should
// wait out silently.
func DefaultRetryPolicy() RetryPolicy {
	return RetryPolicy{
		Attempts: 5,
		Backoff: func(attempt int) time.Duration {
			return time.Duration(250<<uint(attempt)) * time.Millisecond
		},
		Sleep: time.Sleep,
	}
}

// registerWithRetry runs registration, retrying only transient failures.
//
// The classification is the point: ErrNotYetAvailable (unreachable gateway, 401,
// 429) is transient and worth retrying, while a terminal refusal will never
// succeed on its own and must surface at once as operator action instead of a
// long retry that looks like a network outage.
func registerWithRetry(
	ctx context.Context,
	policy RetryPolicy,
	attempt func() (state.ConnectorConfig, error),
) (state.ConnectorConfig, error) {
	if policy.Attempts < 1 {
		policy.Attempts = 1
	}
	var last error
	for index := 0; index < policy.Attempts; index++ {
		identity, err := attempt()
		if err == nil {
			return identity, nil
		}
		if !errors.Is(err, bootstrap.ErrNotYetAvailable) {
			return state.ConnectorConfig{}, err
		}
		last = err
		if index == policy.Attempts-1 {
			break
		}
		if policy.Backoff != nil && policy.Sleep != nil {
			policy.Sleep(policy.Backoff(index))
		}
		if ctx.Err() != nil {
			return state.ConnectorConfig{}, ctx.Err()
		}
	}
	return state.ConnectorConfig{}, fmt.Errorf(
		"gateway did not accept registration after %d attempts: %w", policy.Attempts, last,
	)
}
