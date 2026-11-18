package setup

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
)

// pidFileName is the file the running connector writes its PID to, beside the
// account and source store. `adma source add` reads it to make a running agent
// re-read the store, so a new source is served without a restart.
const pidFileName = "connector.pid"

// PidFilePath returns where the connector records its PID under a state dir.
func PidFilePath(stateDir string) string {
	return filepath.Join(stateDir, pidFileName)
}

// WritePid records this process as the running connector. A stale file from a
// crashed run is overwritten; the reader validates liveness before signalling.
func WritePid(stateDir string) error {
	path := PidFilePath(stateDir)
	if err := os.WriteFile(path, []byte(strconv.Itoa(os.Getpid())), 0o600); err != nil {
		return fmt.Errorf("write connector pid file %q: %w", path, err)
	}
	return nil
}

// RemovePid deletes the pid file on a clean exit. A failure is not worth
// reporting: the next start overwrites it and the reader checks liveness.
func RemovePid(stateDir string) {
	_ = os.Remove(PidFilePath(stateDir))
}

// SignalConnector asks a running connector, if any, to reload its source set.
//
// It sends SIGHUP to the recorded PID. No connector running is not an error:
// adding a source with no agent up just means it serves the source on next start.
// A notice is returned so the caller can tell the user whether a reload was sent,
// and the reload is best-effort because a race with the connector exiting is
// harmless.
func SignalConnector(stateDir string) (reloaded bool, notice string) {
	path := PidFilePath(stateDir)
	raw, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return false, "no running agent found; it will serve this source when it starts"
		}
		return false, "could not read the agent pid file; start or restart the agent to serve this source"
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
	if err != nil || pid <= 0 {
		return false, "the agent pid file is unreadable; start or restart the agent to serve this source"
	}
	process, err := os.FindProcess(pid)
	if err != nil {
		return false, "the agent is not running; it will serve this source when it starts"
	}
	if err := process.Signal(syscall.SIGHUP); err != nil {
		if errors.Is(err, os.ErrProcessDone) || errors.Is(err, syscall.ESRCH) {
			return false, "the agent is not running; it will serve this source when it starts"
		}
		return false, "could not signal the agent; restart it to serve this source"
	}
	return true, "told the running agent to reload; it will serve this source shortly"
}
