// Package target validates and normalizes where a source's origin listens.
//
// A connector runs on the source host and dials the origin itself, so the target
// is a host and a port, never a URL: no scheme, no userinfo, no path, no shell
// expansion, and no DNS name that could be rebound by an attacker. The accepted
// shapes are an IPv4 literal, a properly bracketed IPv6 literal, and a bounded
// Kubernetes Service hostname of the form `svc.namespace.svc`.
//
// Normalization is the contract. The gateway derives a source's identity from a
// normalized target, so the same origin typed two ways must collapse to one
// target, or a repeat registration would create a second source.
package target

import (
	"errors"
	"net"
	"strconv"
	"strings"
)

// Kind is the family of host a target names.
type Kind int

const (
	// IPv4 is a dotted-quad address.
	IPv4 Kind = iota
	// IPv6 is a bracketed literal address.
	IPv6
	// Service is a Kubernetes service hostname.
	Service
)

// Target is a validated origin host and port.
//
// Host is stored unbracketed for an address, and as the lowercased service DNS
// name for a Service. Port is 1-65535.
type Target struct {
	Kind Kind
	Host string
	Port int
}

// String renders the target in the canonical host:port form the gateway and the
// connector both persist, bracketing an IPv6 address.
func (t Target) String() string {
	if t.Kind == IPv6 {
		return "[" + t.Host + "]:" + strconv.Itoa(t.Port)
	}
	return t.Host + ":" + strconv.Itoa(t.Port)
}

// Authority is the value an origin request should carry in its Host header.
//
// For an address this is just the address, so the origin sees the host it is
// bound to. For a Service it is the service DNS name, which is what the origin's
// own router expects. An IPv6 authority is bracketed because an unbracketed one
// is ambiguous with the port separator.
func (t Target) Authority() string {
	if t.Kind == IPv6 {
		return "[" + t.Host + "]"
	}
	return t.Host
}

// Parse validates one host:port and returns its normalized form.
//
// A port is mandatory and must be numeric in range. A host must be an IPv4 or
// bracketed IPv6 literal, or a Kubernetes service hostname. A bare public DNS
// name is rejected here; permitting one is a deployment policy decision made at
// the gateway, not something the client silently allows.
func Parse(value string) (Target, error) {
	text := strings.TrimSpace(value)
	if text == "" {
		return Target{}, errors.New("a target is required")
	}
	if strings.ContainsAny(text, "/\\?#@ \t\r\n") {
		return Target{}, errors.New("target must be a host:port, not a URL or shell input")
	}
	host, portText, err := net.SplitHostPort(text)
	if err != nil {
		return Target{}, errors.New("target must be host:port")
	}
	port, err := strconv.ParseUint(portText, 10, 16)
	if err != nil || port == 0 {
		return Target{}, errors.New("target port must be a number from 1 to 65535")
	}
	normalized, kind, err := normalizeHost(host)
	if err != nil {
		return Target{}, err
	}
	return Target{Kind: kind, Host: normalized, Port: int(port)}, nil
}

func normalizeHost(host string) (string, Kind, error) {
	if host == "" {
		return "", 0, errors.New("target host is empty")
	}
	if address := net.ParseIP(host); address != nil {
		return normalizeAddress(address)
	}
	if net.ParseIP(strings.Trim(host, "[]")) != nil {
		return "", 0, errors.New("an IPv6 target must be bracketed as [address]:port")
	}
	service, err := normalizeService(host)
	if err != nil {
		return "", 0, err
	}
	return service, Service, nil
}

// normalizeAddress canonicalizes an address literal and rejects the addresses a
// source may never expose.
//
// Every mapped IPv4 form collapses to plain IPv4 so `::ffff:127.0.0.1` and
// `127.0.0.1` name one source, matching the gateway's Python normalizer exactly.
// Unspecified (0.0.0.0, ::), multicast, and broadcast/link-local addresses are
// refused: a source origin is a specific host to dial, and an unspecified address
// means "this machine", not a target, while the others are not a service address.
func normalizeAddress(address net.IP) (string, Kind, error) {
	if v4 := address.To4(); v4 != nil {
		if !usableIPv4(v4) {
			return "", 0, errors.New("target address must be a unicast host address")
		}
		return v4.String(), IPv4, nil
	}
	if !usableIPv6(address) {
		return "", 0, errors.New("target address must be a unicast host address")
	}
	return address.String(), IPv6, nil
}

func usableIPv4(v4 net.IP) bool {
	return !v4.IsUnspecified() && !v4.IsMulticast() && !v4.Equal(net.IPv4bcast) && !v4.IsLinkLocalUnicast()
}

func usableIPv6(v6 net.IP) bool {
	return !v6.IsUnspecified() && !v6.IsMulticast() && !v6.IsLinkLocalUnicast()
}

// normalizeService accepts `name.namespace.svc` and the full cluster forms
// `name.namespace.svc.cluster.local` and `name.namespace.svc.local`, returning
// the short form so every spelling identifies one source.
//
// Each label must be a DNS-1123 label. The required `svc` suffix and the cluster
// domain are what constrain this to a service inside the cluster, which is the
// only non-loopback host this client supports; an arbitrary public hostname has
// no such suffix and is refused.
func normalizeService(host string) (string, error) {
	labels := strings.Split(strings.ToLower(strings.TrimSuffix(host, ".")), ".")
	for _, label := range labels {
		if !dnsLabel(label) {
			return "", errors.New("target host must be an IP address or a Kubernetes service name")
		}
	}
	for _, suffix := range [][]string{{"svc", "cluster", "local"}, {"svc", "local"}, {"svc"}} {
		if !hasSuffix(labels, suffix) {
			continue
		}
		service := labels[:len(labels)-len(suffix)]
		if len(service) < 2 {
			return "", errors.New("target host must be a Kubernetes service name like name.namespace.svc")
		}
		return strings.Join(append(service, "svc"), "."), nil
	}
	return "", errors.New("target host must be a Kubernetes service name like name.namespace.svc")
}

func hasSuffix(labels []string, suffix []string) bool {
	if len(labels) < len(suffix) {
		return false
	}
	offset := len(labels) - len(suffix)
	for index, label := range suffix {
		if labels[offset+index] != label {
			return false
		}
	}
	return true
}

func dnsLabel(label string) bool {
	if len(label) == 0 || len(label) > 63 {
		return false
	}
	for index, character := range label {
		switch {
		case character >= 'a' && character <= 'z', character >= '0' && character <= '9':
		case character == '-' && index != 0 && index != len(label)-1:
		default:
			return false
		}
	}
	return true
}
