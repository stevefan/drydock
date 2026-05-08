// dockwarden — drydock's egress proxy.
//
// Replaces smokescreen for the drydock use case. SNI-aware HTTPS
// gating via HTTP CONNECT, allowlist-driven ACL with file-watch
// live-reload, JSON structured logging, SSRF protections.
//
// Why not smokescreen: smokescreen's ACL semantics around
// allow_missing_role are footgunny when not running with mTLS, and
// the version pinning + ACL field naming has been a moving target.
// Dockwarden owns the format the drydock daemon writes — single
// source of truth, no impedance mismatch.
//
// Threat model:
//   - Trust boundary: the container itself. Dockwarden is loopback-only
//     (127.0.0.1:4750); only processes inside the container can reach it.
//   - The kernel firewall (iptables OUTPUT chain) is the hard floor —
//     deny-all except loopback to dockwarden's port. If dockwarden is
//     compromised, the worst it can do is what its ACL already permits.
//   - SSRF: dockwarden refuses connections to RFC1918, link-local, and
//     loopback addresses regardless of ACL. Catches misconfig where a
//     domain resolves to a private IP.
package main

import (
	"encoding/json"
	"flag"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"gopkg.in/yaml.v3"
)

// Allowlist is the parsed view of the ACL. allowed_hosts is exact
// match; allowed_domains matches the domain itself plus any subdomain
// (so "github.com" matches "github.com" and "api.github.com").
type Allowlist struct {
	AllowedHosts   []string `yaml:"allowed_hosts"`
	AllowedDomains []string `yaml:"allowed_domains"`

	// Derived at load time. Maps + suffix slice for fast lookup.
	hostSet      map[string]struct{}
	domainSuffix []string // each entry pre-fixed with "." for HasSuffix match
}

// ACL holds the current allowlist and reloads it from disk on signal
// or file-modtime change.
type ACL struct {
	Path string

	mu        sync.RWMutex
	current   *Allowlist
	loadedAt  time.Time
}

// rootDoc mirrors the YAML the daemon writes:
//
//	version: v1
//	default:
//	  allowed_hosts: [...]
//	  allowed_domains: [...]
//
// We intentionally ignore unknown top-level keys so the format can
// evolve forward without breaking us.
type rootDoc struct {
	Default Allowlist `yaml:"default"`
}

func (a *ACL) Load() error {
	data, err := os.ReadFile(a.Path)
	if err != nil {
		return err
	}
	var root rootDoc
	if err := yaml.Unmarshal(data, &root); err != nil {
		return err
	}
	list := root.Default
	list.hostSet = make(map[string]struct{}, len(list.AllowedHosts))
	for _, h := range list.AllowedHosts {
		list.hostSet[strings.ToLower(strings.TrimSpace(h))] = struct{}{}
	}
	list.domainSuffix = make([]string, 0, len(list.AllowedDomains))
	for _, d := range list.AllowedDomains {
		d = strings.ToLower(strings.TrimSpace(d))
		d = strings.TrimPrefix(d, "*.")
		if d == "" {
			continue
		}
		list.domainSuffix = append(list.domainSuffix, "."+d)
		// Also exact-match the bare domain
		list.hostSet[d] = struct{}{}
	}
	a.mu.Lock()
	a.current = &list
	a.loadedAt = time.Now()
	a.mu.Unlock()
	return nil
}

// Allowed reports whether the given hostname is in the current
// allowlist. Case-insensitive.
func (a *ACL) Allowed(host string) bool {
	a.mu.RLock()
	defer a.mu.RUnlock()
	if a.current == nil {
		return false
	}
	h := strings.ToLower(host)
	if _, ok := a.current.hostSet[h]; ok {
		return true
	}
	for _, suffix := range a.current.domainSuffix {
		if strings.HasSuffix(h, suffix) {
			return true
		}
	}
	return false
}

// Stats for /healthz endpoint.
func (a *ACL) Stats() (int, int, time.Time) {
	a.mu.RLock()
	defer a.mu.RUnlock()
	if a.current == nil {
		return 0, 0, time.Time{}
	}
	return len(a.current.AllowedHosts), len(a.current.AllowedDomains), a.loadedAt
}

// isPrivate returns true for IPs that no legitimate egress should
// hit: RFC1918, link-local, loopback, multicast, IPv6 ULA. Refused
// regardless of ACL — SSRF defense in depth.
func isPrivate(ip net.IP) bool {
	if ip.IsLoopback() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() ||
		ip.IsMulticast() || ip.IsUnspecified() {
		return true
	}
	for _, cidr := range privateCIDRs {
		if cidr.Contains(ip) {
			return true
		}
	}
	return false
}

var privateCIDRs []*net.IPNet

func init() {
	for _, s := range []string{
		"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
		"169.254.0.0/16",
		"fc00::/7", "fe80::/10",
		// AWS metadata IP — common SSRF target
		"169.254.169.254/32",
	} {
		_, n, _ := net.ParseCIDR(s)
		if n != nil {
			privateCIDRs = append(privateCIDRs, n)
		}
	}
}

// logEvent emits one JSON line to stdout (caller redirects to log file).
func logEvent(decision, host, port, reason, project, clientAddr string, durationMs int64) {
	rec := map[string]any{
		"ts":          time.Now().UTC().Format(time.RFC3339Nano),
		"decision":    decision, // "allow" | "deny"
		"host":        host,
		"port":        port,
		"reason":      reason, // "" for allow; e.g. "not-in-allowlist", "ssrf", "dial-failed"
		"project":     project,
		"client_addr": clientAddr,
		"duration_ms": durationMs,
	}
	b, _ := json.Marshal(rec)
	log.Println(string(b))
}

func handleConnect(w http.ResponseWriter, r *http.Request, acl *ACL, project string) {
	start := time.Now()
	host, port, err := net.SplitHostPort(r.Host)
	if err != nil {
		host = r.Host
		port = "443"
	}
	host = strings.ToLower(host)

	if !acl.Allowed(host) {
		http.Error(w, "Forbidden by drydock egress policy", http.StatusForbidden)
		logEvent("deny", host, port, "not-in-allowlist", project, r.RemoteAddr, time.Since(start).Milliseconds())
		return
	}

	// Resolve + SSRF check before connecting.
	ips, err := net.LookupIP(host)
	if err != nil {
		http.Error(w, "DNS resolution failed", http.StatusBadGateway)
		logEvent("deny", host, port, "dns-failed:"+err.Error(), project, r.RemoteAddr, time.Since(start).Milliseconds())
		return
	}
	for _, ip := range ips {
		if isPrivate(ip) {
			http.Error(w, "Forbidden: resolves to private/SSRF address", http.StatusForbidden)
			logEvent("deny", host, port, "ssrf-private:"+ip.String(), project, r.RemoteAddr, time.Since(start).Milliseconds())
			return
		}
	}

	upstream, err := net.DialTimeout("tcp", net.JoinHostPort(host, port), 10*time.Second)
	if err != nil {
		http.Error(w, "Bad Gateway", http.StatusBadGateway)
		logEvent("deny", host, port, "dial-failed:"+err.Error(), project, r.RemoteAddr, time.Since(start).Milliseconds())
		return
	}
	defer upstream.Close()

	hijacker, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "Internal Error", http.StatusInternalServerError)
		return
	}
	clientConn, _, err := hijacker.Hijack()
	if err != nil {
		http.Error(w, "Hijack failed", http.StatusInternalServerError)
		return
	}
	defer clientConn.Close()

	// Tell the client the tunnel is open. Per RFC 7231 §4.3.6.
	if _, err := clientConn.Write([]byte("HTTP/1.1 200 Connection Established\r\n\r\n")); err != nil {
		logEvent("deny", host, port, "client-write-failed:"+err.Error(), project, r.RemoteAddr, time.Since(start).Milliseconds())
		return
	}
	logEvent("allow", host, port, "", project, r.RemoteAddr, time.Since(start).Milliseconds())

	// Bidirectional copy until either side closes.
	done := make(chan struct{}, 2)
	go func() { _, _ = io.Copy(upstream, clientConn); done <- struct{}{} }()
	go func() { _, _ = io.Copy(clientConn, upstream); done <- struct{}{} }()
	<-done
}

// healthHandler — exposed on the same port. The Auditor probes this
// to detect "proxy is up but allowlist is empty" failure modes.
func healthHandler(acl *ACL) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		hosts, domains, loadedAt := acl.Stats()
		resp := map[string]any{
			"ok":              true,
			"allowed_hosts":   hosts,
			"allowed_domains": domains,
			"loaded_at":       loadedAt.UTC().Format(time.RFC3339),
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(resp)
	}
}

// watchACL polls the ACL file every interval and reloads on modtime change.
// This is the always-on belt; SIGHUP is the suspenders for daemon-driven reload.
func watchACL(acl *ACL, interval time.Duration) {
	var last time.Time
	if info, err := os.Stat(acl.Path); err == nil {
		last = info.ModTime()
	}
	for {
		time.Sleep(interval)
		info, err := os.Stat(acl.Path)
		if err != nil {
			continue
		}
		if info.ModTime().After(last) {
			last = info.ModTime()
			if err := acl.Load(); err != nil {
				log.Printf("dockwarden: poll-reload failed: %v", err)
			} else {
				log.Printf("dockwarden: poll-reload ok (mtime=%s)", info.ModTime().UTC().Format(time.RFC3339))
			}
		}
	}
}

func main() {
	listenAddr := flag.String("listen", "127.0.0.1:4750", "listen address")
	aclPath := flag.String("acl", "/run/drydock/proxy/allowlist.yaml", "path to ACL YAML")
	project := flag.String("project", "drydock", "project tag for log events")
	flag.Parse()

	acl := &ACL{Path: *aclPath}
	if err := acl.Load(); err != nil {
		log.Fatalf("dockwarden: initial ACL load failed: %v", err)
	}

	// SIGHUP triggers immediate reload (daemon-driven path).
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGHUP)
	go func() {
		for range sigCh {
			if err := acl.Load(); err != nil {
				log.Printf("dockwarden: SIGHUP reload failed: %v", err)
			} else {
				hosts, domains, _ := acl.Stats()
				log.Printf("dockwarden: SIGHUP reload ok (hosts=%d domains=%d)", hosts, domains)
			}
		}
	}()

	// Stat-poll every 5s so the daemon doesn't HAVE to SIGHUP us.
	go watchACL(acl, 5*time.Second)

	// CONNECT requests have r.URL.Path = "" and don't route through
	// http.ServeMux as you'd expect — handle method before any pattern
	// matching. Healthz is on a fixed path and only via GET.
	healthFn := healthHandler(acl)
	rootHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodConnect {
			handleConnect(w, r, acl, *project)
			return
		}
		if r.Method == http.MethodGet && r.URL.Path == "/healthz" {
			healthFn(w, r)
			return
		}
		http.Error(w, "dockwarden only supports CONNECT (and GET /healthz)", http.StatusMethodNotAllowed)
	})

	server := &http.Server{
		Addr:              *listenAddr,
		Handler:           rootHandler,
		ReadHeaderTimeout: 5 * time.Second,
	}
	hosts, domains, _ := acl.Stats()
	log.Printf("dockwarden: listening on %s, acl=%s, hosts=%d, domains=%d",
		*listenAddr, *aclPath, hosts, domains)
	log.Fatal(server.ListenAndServe())
}
