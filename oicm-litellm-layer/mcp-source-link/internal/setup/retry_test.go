package setup

import (
	"context"
	"errors"
	"testing"
	"time"

	"oicm-litellm-layer/mcp-source-link/internal/bootstrap"
	"oicm-litellm-layer/mcp-source-link/internal/config"
)

func policy(attempts int) (RetryPolicy, *int) {
	slept := 0
	return RetryPolicy{
		Attempts: attempts,
		Backoff:  func(int) time.Duration { return time.Millisecond },
		Sleep:    func(time.Duration) { slept++ },
	}, &slept
}

// TestRegisterWithRetryRetriesTransientOnly is the R7 regression: a transient
// gateways-offline error must be retried, which the previous single-attempt setup
// never did, while a permanent refusal must stop immediately instead of looking
// like an outage the daemon waits out.
func TestRegisterWithRetryRetriesTransientOnly(t *testing.T) {
	p, slept := policy(4)
	calls := 0
	identity, err := registerWithRetry(context.Background(), p, func() (config.ConnectorConfig, error) {
		calls++
		if calls < 3 {
			return config.ConnectorConfig{}, bootstrap.ErrNotYetAvailable
		}
		return config.ConnectorConfig{WorkerID: "w1", Generation: 1}, nil
	})
	if err != nil {
		t.Fatalf("a transient failure must eventually succeed: %v", err)
	}
	if identity.WorkerID != "w1" {
		t.Fatalf("got %+v, want the issued identity", identity)
	}
	if calls != 3 {
		t.Fatalf("expected 3 attempts, got %d", calls)
	}
	if *slept != 2 {
		t.Fatalf("expected 2 backoff sleeps, got %d", *slept)
	}
}

func TestRegisterWithRetryStopsOnTerminalRefusal(t *testing.T) {
	p, slept := policy(5)
	calls := 0
	_, err := registerWithRetry(context.Background(), p, func() (config.ConnectorConfig, error) {
		calls++
		return config.ConnectorConfig{}, bootstrap.ErrTerminalRegistration
	})
	if !errors.Is(err, bootstrap.ErrTerminalRegistration) {
		t.Fatalf("a terminal refusal must surface unchanged, got %v", err)
	}
	if calls != 1 {
		t.Fatalf("a terminal refusal must not be retried, got %d calls", calls)
	}
	if *slept != 0 {
		t.Fatalf("a terminal refusal must not sleep, got %d", *slept)
	}
}

func TestRegisterWithRetryIsBounded(t *testing.T) {
	p, _ := policy(3)
	calls := 0
	_, err := registerWithRetry(context.Background(), p, func() (config.ConnectorConfig, error) {
		calls++
		return config.ConnectorConfig{}, bootstrap.ErrNotYetAvailable
	})
	if err == nil {
		t.Fatal("an unreachable gateway must eventually give up")
	}
	if calls != 3 {
		t.Fatalf("retry must be bounded to 3 attempts, got %d", calls)
	}
	if !errors.Is(err, bootstrap.ErrNotYetAvailable) {
		t.Fatalf("the final error must name the transient cause, got %v", err)
	}
}

func TestRegisterWithRetryHonoursCancellation(t *testing.T) {
	p, _ := policy(5)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	calls := 0
	_, err := registerWithRetry(ctx, p, func() (config.ConnectorConfig, error) {
		calls++
		return config.ConnectorConfig{}, bootstrap.ErrNotYetAvailable
	})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("a cancelled context must stop the loop, got %v", err)
	}
	if calls != 1 {
		t.Fatalf("cancellation must stop after the first attempt, got %d", calls)
	}
}
