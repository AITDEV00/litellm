// Command targetparity prints the canonical form of every `host:port` on stdin,
// one per line, or REJECT when the target grammar refuses it.
//
// It exists for the cross-language contract test: the gateway's Python validator
// (litellm.proxy.mcp_source_broker.models.canonical_worker_host) and this module's
// target.Parse must agree on every spelling, so the Python suite builds this
// command and compares its output case by case. Run it by hand with:
//
//	go run ./cmd/targetparity <<EOF
//	[::1]:8080
//	litellm-dev.adeo-litellm.svc:4000
//	EOF
package main

import (
	"bufio"
	"fmt"
	"os"
	"strings"

	"oicm-litellm-layer/mcp-source-link/internal/target"
)

func main() {
	scanner := bufio.NewScanner(os.Stdin)
	for scanner.Scan() {
		value := strings.TrimSpace(scanner.Text())
		parsed, err := target.Parse(value)
		if err != nil {
			fmt.Println("REJECT")
			continue
		}
		fmt.Println(parsed.String())
	}
}
