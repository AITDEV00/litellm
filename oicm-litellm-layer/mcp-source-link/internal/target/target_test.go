package target

import "testing"

// A target is what a source's identity is derived from, so these cases pin the
// two properties the gateway relies on: the accepted shapes, and that equivalent
// spellings normalize to one value. A change that makes two spellings of one
// origin distinct would silently create duplicate sources.
func TestParseAcceptsAndNormalizes(t *testing.T) {
	for _, testCase := range []struct {
		name string
		in   string
		want string
		kind Kind
	}{
		{"ipv4", "127.0.0.1:8080", "127.0.0.1:8080", IPv4},
		{"ipv4 private", "10.34.104.99:4000", "10.34.104.99:4000", IPv4},
		{"ipv4 with padding", "  10.0.0.1:80  ", "10.0.0.1:80", IPv4},
		{"ipv6 bracketed", "[::1]:8080", "[::1]:8080", IPv6},
		{"ipv6 compressed", "[fd00::1]:4000", "[fd00::1]:4000", IPv6},
		{"ipv6 normalized", "[0:0:0:0:0:0:0:1]:80", "[::1]:80", IPv6},
		{"service short", "litellm-dev.adeo-litellm.svc:4000", "litellm-dev.adeo-litellm.svc:4000", Service},
		{"service full cluster", "litellm-dev.adeo-litellm.svc.cluster.local:4000", "litellm-dev.adeo-litellm.svc:4000", Service},
		{"service uppercase", "LiteLLM-DEV.ADEO-LITELLM.SVC:4000", "litellm-dev.adeo-litellm.svc:4000", Service},
		{"service trailing dot", "litellm-dev.adeo-litellm.svc.:4000", "litellm-dev.adeo-litellm.svc:4000", Service},
		{"port range low", "127.0.0.1:1", "127.0.0.1:1", IPv4},
		{"port range high", "127.0.0.1:65535", "127.0.0.1:65535", IPv4},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			got, err := Parse(testCase.in)
			if err != nil {
				t.Fatalf("Parse(%q) rejected a valid target: %v", testCase.in, err)
			}
			if got.String() != testCase.want {
				t.Fatalf("Parse(%q) = %q, want %q", testCase.in, got.String(), testCase.want)
			}
			if got.Kind != testCase.kind {
				t.Fatalf("Parse(%q) kind = %v, want %v", testCase.in, got.Kind, testCase.kind)
			}
		})
	}
}

// Two spellings of one origin must normalize identically, or a repeat
// registration against the other spelling would create a second source.
func TestEquivalentSpellingsCollapse(t *testing.T) {
	groups := [][]string{
		{"litellm-dev.adeo-litellm.svc:4000", "LiteLLM-DEV.adeo-litellm.svc.local.:4000",
			"litellm-dev.adeo-litellm.svc.cluster.local:4000"},
		{"[::1]:8080", "[0:0:0:0:0:0:0:1]:8080"},
	}
	for _, group := range groups {
		first := group[0]
		parsed, err := Parse(first)
		if err != nil {
			t.Fatalf("Parse(%q) failed: %v", first, err)
		}
		for _, spelling := range group[1:] {
			other, err := Parse(spelling)
			if err != nil {
				t.Fatalf("Parse(%q) failed: %v", spelling, err)
			}
			if other != parsed {
				t.Fatalf("%q normalized to %v, but %q normalized to %v", first, parsed, spelling, other)
			}
		}
	}
}

func TestParseRejects(t *testing.T) {
	for _, testCase := range []struct {
		name string
		in   string
	}{
		{"empty", ""},
		{"no port", "127.0.0.1"},
		{"zero port", "127.0.0.1:0"},
		{"port too high", "127.0.0.1:65536"},
		{"non-numeric port", "127.0.0.1:http"},
		{"url scheme", "http://127.0.0.1:8080"},
		{"userinfo", "user:pass@127.0.0.1:8080"},
		{"path", "127.0.0.1:8080/admin"},
		{"query", "127.0.0.1:8080?x=1"},
		{"shell expansion", "$(whoami):80"},
		{"space", "127.0.0.1 :8080"},
		{"unbracketed ipv6", "fd00::1:4000"},
		{"public hostname", "example.com:443"},
		{"public hostname with svc", "www.svc.example.com:80"},
		{"single label service", "litellm:4000"},
		{"two label", "litellm-dev.adeo-litellm:4000"},
		{"empty host", ":8080"},
		{"wildcard", "*:8080"},
		{"bad service label", "litellm_dev.adeo.svc:4000"},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			if got, err := Parse(testCase.in); err == nil {
				t.Fatalf("Parse(%q) accepted an invalid target as %v", testCase.in, got)
			}
		})
	}
}

func TestAuthorityBracketsIPv6(t *testing.T) {
	for _, testCase := range []struct {
		in   string
		want string
	}{
		{"127.0.0.1:8080", "127.0.0.1"},
		{"[fd00::1]:4000", "[fd00::1]"},
		{"litellm-dev.adeo-litellm.svc:4000", "litellm-dev.adeo-litellm.svc"},
	} {
		parsed, err := Parse(testCase.in)
		if err != nil {
			t.Fatalf("Parse(%q) failed: %v", testCase.in, err)
		}
		if got := parsed.Authority(); got != testCase.want {
			t.Fatalf("Authority(%q) = %q, want %q", testCase.in, got, testCase.want)
		}
	}
}

// A target must be rebuilt from the components a config file round-trips,
// otherwise a restart would dial a different origin than the one registered.
func TestTargetRoundTrips(t *testing.T) {
	for _, value := range []string{"127.0.0.1:8080", "[fd00::1]:4000", "litellm-dev.adeo-litellm.svc:4000"} {
		parsed, err := Parse(value)
		if err != nil {
			t.Fatalf("Parse(%q) failed: %v", value, err)
		}
		again, err := Parse(parsed.String())
		if err != nil {
			t.Fatalf("re-Parse(%q) failed: %v", parsed.String(), err)
		}
		if again != parsed {
			t.Fatalf("round trip changed %v into %v", parsed, again)
		}
	}
}

// An IPv4-mapped IPv6 literal names the same source as its plain IPv4 form. The
// gateway's Python normalizer must agree, so `[::ffff:127.0.0.1]:8080` and
// `127.0.0.1:8080` collapse to one target rather than creating two sources with
// different aliases.
func TestMappedIPv4CollapsesToIPv4(t *testing.T) {
	for _, testCase := range []struct {
		in   string
		want string
	}{
		{"[::ffff:127.0.0.1]:8080", "127.0.0.1:8080"},
		{"[::ffff:10.34.104.99]:4000", "10.34.104.99:4000"},
		{"[::ffff:7f00:1]:8080", "127.0.0.1:8080"},
	} {
		got, err := Parse(testCase.in)
		if err != nil {
			t.Fatalf("Parse(%q) rejected a mapped IPv4: %v", testCase.in, err)
		}
		if got.Kind != IPv4 {
			t.Fatalf("Parse(%q) kind = %v, want IPv4", testCase.in, got.Kind)
		}
		if got.String() != testCase.want {
			t.Fatalf("Parse(%q) = %q, want %q", testCase.in, got.String(), testCase.want)
		}
	}
}

// A source origin is a specific unicast host, so an unspecified, multicast,
// broadcast, or link-local address must be refused rather than dialed. This is the
// immutability policy the gateway mirrors in canonical_worker_host.
func TestRejectsUnroutableOrNonUnicastAddresses(t *testing.T) {
	for _, in := range []string{
		"0.0.0.0:8080",
		"[::]:8080",
		"255.255.255.255:8080",
		"224.0.0.1:8080",
		"[ff02::1]:8080",
		"[fe80::1]:8080",
		"169.254.1.1:8080",
	} {
		if _, err := Parse(in); err == nil {
			t.Fatalf("Parse(%q) accepted a non-unicast or unroutable address", in)
		}
	}
}
