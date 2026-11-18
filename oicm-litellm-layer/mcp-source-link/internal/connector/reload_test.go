package connector

import (
	"reflect"
	"strings"
	"testing"

	"oicm-litellm-layer/mcp-source-link/internal/state"
)

// src builds a registered source with a full worker identity, so the reconcile
// logic treats it as servable. `generation` is the gateway's change marker.
func src(name, worker string, generation uint64) state.Source {
	return state.Source{
		Name:       name,
		Account:    "alpha",
		Target:     "127.0.0.1:18201",
		WorkerID:   worker,
		SourceID:   "sid-" + worker,
		Generation: generation,
		Credential: strings.Repeat("a", 64),
		Enabled:    true,
	}
}

// A reload must not restart a source that nobody touched, or every add would drop
// and re-dial every healthy tunnel. An identical record plans neither a stop nor
// a start.
func TestPlanChangesLeavesUnchangedSourceAlone(t *testing.T) {
	a := src("alpha.127.0.0.1.18201", "w1", 1)
	stop, start := planChanges(
		map[string]state.Source{a.Name: a},
		map[string]state.Source{a.Name: a},
	)
	if len(stop) != 0 || len(start) != 0 {
		t.Fatalf("unchanged source planned stop=%v start=%v, want neither", stop, start)
	}
}

// Adding a source starts only the new one and touches nothing already running.
func TestPlanChangesStartsOnlyNewSource(t *testing.T) {
	a := src("alpha.127.0.0.1.18201", "w1", 1)
	b := src("beta.127.0.0.1.18202", "w2", 1)

	stop, start := planChanges(
		map[string]state.Source{a.Name: a},
		map[string]state.Source{a.Name: a, b.Name: b},
	)
	if len(stop) != 0 {
		t.Fatalf("starting a new source stopped %v, want none", stop)
	}
	if len(start) != 1 || start[0].Name != b.Name {
		t.Fatalf("start = %v, want only %s", start, b.Name)
	}
}

// Removing a source stops it and leaves the survivors running.
func TestPlanChangesStopsOnlyRemovedSource(t *testing.T) {
	a := src("alpha.127.0.0.1.18201", "w1", 1)
	b := src("alpha.127.0.0.1.18203", "w2", 1)

	stop, start := planChanges(
		map[string]state.Source{a.Name: a, b.Name: b},
		map[string]state.Source{a.Name: a},
	)
	if !reflect.DeepEqual(stop, []string{b.Name}) {
		t.Fatalf("stop = %v, want [%s]", stop, b.Name)
	}
	if len(start) != 0 {
		t.Fatalf("removing a source started %v, want none", start)
	}
}

// A source whose gateway identity changed (a re-enrollment bumped the generation)
// must be restarted, because the old tunnel authenticates as a stale worker.
func TestPlanChangesRestartsSourceWhoseIdentityChanged(t *testing.T) {
	before := src("alpha.127.0.0.1.18201", "w1", 1)
	after := src("alpha.127.0.0.1.18201", "w1", 2)

	stop, start := planChanges(
		map[string]state.Source{before.Name: before},
		map[string]state.Source{after.Name: after},
	)
	if !reflect.DeepEqual(stop, []string{after.Name}) {
		t.Fatalf("stop = %v, want the changed source stopped", stop)
	}
	if len(start) != 1 || start[0].Name != after.Name {
		t.Fatalf("start = %v, want the changed source restarted", start)
	}
}
