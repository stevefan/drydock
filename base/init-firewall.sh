#!/bin/bash
set -euo pipefail  # Exit on error, undefined vars, and pipeline failures
IFS=$'\n\t'       # Stricter word splitting

LOG="/tmp/firewall.log"
exec > >(tee -a "$LOG") 2>&1
echo "=== Firewall init started at $(date) ==="

# Project-specific domains to whitelist (space-separated, passed via env)
FIREWALL_EXTRA_DOMAINS="${FIREWALL_EXTRA_DOMAINS:-}"

# 1. Extract Docker DNS info BEFORE any flushing
DOCKER_DNS_RULES=$(iptables-save -t nat | grep "127\.0\.0\.11" || true)

# Flush existing rules, reset policies to ACCEPT (so we're not locked out
# by a previous failed run's DROP policies), and delete existing ipsets
iptables -P INPUT ACCEPT
iptables -P FORWARD ACCEPT
iptables -P OUTPUT ACCEPT
iptables -F
iptables -X
iptables -t nat -F
iptables -t nat -X
iptables -t mangle -F
iptables -t mangle -X
ipset destroy allowed-domains 2>/dev/null || true

# 2. Selectively restore ONLY internal Docker DNS resolution
if [ -n "$DOCKER_DNS_RULES" ]; then
    echo "Restoring Docker DNS rules..."
    iptables -t nat -N DOCKER_OUTPUT 2>/dev/null || true
    iptables -t nat -N DOCKER_POSTROUTING 2>/dev/null || true
    echo "$DOCKER_DNS_RULES" | xargs -L 1 iptables -t nat
else
    echo "No Docker DNS rules to restore"
fi

# 3. Allow essential traffic BEFORE dropping everything
# Allow outbound DNS
iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
# Allow inbound DNS responses
iptables -A INPUT -p udp --sport 53 -j ACCEPT
# Allow outbound SSH
iptables -A OUTPUT -p tcp --dport 22 -j ACCEPT
# Allow inbound SSH responses
iptables -A INPUT -p tcp --sport 22 -m state --state ESTABLISHED -j ACCEPT
# Allow localhost
iptables -A INPUT -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
# Allow Tailscale tunnel traffic
iptables -A INPUT -i tailscale0 -j ACCEPT
iptables -A OUTPUT -o tailscale0 -j ACCEPT
# Allow Tailscale UDP (WireGuard) on port 41641
iptables -A OUTPUT -p udp --dport 41641 -j ACCEPT
iptables -A INPUT -p udp --sport 41641 -m state --state ESTABLISHED -j ACCEPT
# Allow established connections
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# Get host IP from default route and allow host network
HOST_IP=$(ip route | grep default | cut -d" " -f3)
if [ -z "$HOST_IP" ]; then
    echo "ERROR: Failed to detect host IP"
    exit 1
fi
HOST_NETWORK=$(echo "$HOST_IP" | sed "s/\.[0-9]*$/.0\/24/")
echo "Host network detected as: $HOST_NETWORK"
iptables -A INPUT -s "$HOST_NETWORK" -j ACCEPT
iptables -A OUTPUT -d "$HOST_NETWORK" -j ACCEPT

# 4. Build the whitelist BEFORE dropping (network is still open, but we need
#    outbound access to curl GitHub /meta and dig domain IPs)
ipset create allowed-domains hash:net

# Fetch GitHub meta information and aggregate + add their IP ranges
echo "Fetching GitHub IP ranges..."
gh_ranges=$(curl -s --connect-timeout 10 https://api.github.com/meta)
if [ -z "$gh_ranges" ]; then
    echo "ERROR: Failed to fetch GitHub IP ranges"
    exit 1
fi

if ! echo "$gh_ranges" | jq -e '.web and .api and .git' >/dev/null; then
    echo "ERROR: GitHub API response missing required fields"
    exit 1
fi

echo "Processing GitHub IPs..."
while read -r cidr; do
    if [[ ! "$cidr" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}/[0-9]{1,2}$ ]]; then
        echo "ERROR: Invalid CIDR range from GitHub meta: $cidr"
        exit 1
    fi
    echo "Adding GitHub range $cidr"
    ipset add allowed-domains "$cidr" -exist
done < <(echo "$gh_ranges" | jq -r '(.web + .api + .git)[]' | aggregate -q)

# Base domains (infra: npm, Anthropic, VS Code, Tailscale)
BASE_DOMAINS=(
    "registry.npmjs.org"
    "api.anthropic.com"
    "sentry.io"
    "statsig.anthropic.com"
    "statsig.com"
    "marketplace.visualstudio.com"
    "vscode.blob.core.windows.net"
    "update.code.visualstudio.com"
    "controlplane.tailscale.com"
    "login.tailscale.com"
    "log.tailscale.io"
    "derp1.tailscale.com"
    "derp2.tailscale.com"
    "derp3.tailscale.com"
    "derp4.tailscale.com"
    "derp5.tailscale.com"
    "derp6.tailscale.com"
    "derp7.tailscale.com"
    "derp8.tailscale.com"
    "derp9.tailscale.com"
    "derp10.tailscale.com"
)

# Merge base + project-specific domains
ALL_DOMAINS=("${BASE_DOMAINS[@]}")
if [ -n "$FIREWALL_EXTRA_DOMAINS" ]; then
    # FIREWALL_EXTRA_DOMAINS is space-separated; the script's strict IFS
    # (newline+tab) would otherwise treat the whole value as one "domain".
    IFS=' ' read -ra EXTRA <<< "$FIREWALL_EXTRA_DOMAINS"
    ALL_DOMAINS+=("${EXTRA[@]}")
    ( IFS=' '; echo "Extra project domains: ${EXTRA[*]}" )
fi

# Resolve and add all allowed domains
for domain in "${ALL_DOMAINS[@]}"; do
    echo "Resolving $domain..."
    ips=$(dig +noall +answer A "$domain" | awk '$4 == "A" {print $5}')
    if [ -z "$ips" ]; then
        echo "ERROR: Failed to resolve $domain"
        exit 1
    fi

    while read -r ip; do
        if [[ ! "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
            echo "ERROR: Invalid IP from DNS for $domain: $ip"
            exit 1
        fi
        echo "Adding $ip for $domain"
        ipset add allowed-domains "$ip" -exist
    done < <(echo "$ips")
done

# 5. Whitelist built — NOW lock down
iptables -A OUTPUT -m set --match-set allowed-domains dst -j ACCEPT
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP
echo "Default policies set to DROP"

# Explicitly REJECT all other outbound traffic for immediate feedback
iptables -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited

# 6. IPv6: deny by default, allow specific IPv6 hosts if configured
ip6tables -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true
ip6tables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
ip6tables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true

# Allow IPv6 hosts from env (space-separated: "host:port host2:port2")
FIREWALL_IPV6_HOSTS="${FIREWALL_IPV6_HOSTS:-}"
if [ -n "$FIREWALL_IPV6_HOSTS" ]; then
    echo "Configuring IPv6 allowlist..."
    # Space-separated; override strict IFS for this split.
    IFS=' ' read -ra IPV6_ENTRIES <<< "$FIREWALL_IPV6_HOSTS"
    for entry in "${IPV6_ENTRIES[@]}"; do
        host="${entry%%:*}"
        port="${entry##*:}"
        ipv6=$(dig +noall +answer AAAA "$host" | awk '$4 == "AAAA" {print $5}')
        if [ -n "$ipv6" ]; then
            ip6tables -A OUTPUT -d "$ipv6" -p tcp --dport "$port" -j ACCEPT 2>/dev/null || true
            echo "Added IPv6 rule for $host: $ipv6 port $port"
        else
            echo "WARNING: Could not resolve IPv6 for $host"
        fi
    done
fi

ip6tables -P INPUT DROP 2>/dev/null || true
ip6tables -P OUTPUT DROP 2>/dev/null || true

# 7. Verification
echo "Firewall configuration complete"
echo "Verifying firewall rules..."
if curl --connect-timeout 5 https://example.com >/dev/null 2>&1; then
    echo "ERROR: Firewall verification failed - was able to reach https://example.com"
    exit 1
else
    echo "Firewall verification passed - unable to reach https://example.com as expected"
fi

if ! curl --connect-timeout 5 https://api.github.com/zen >/dev/null 2>&1; then
    echo "ERROR: Firewall verification failed - unable to reach https://api.github.com"
    exit 1
else
    echo "Firewall verification passed - able to reach https://api.github.com as expected"
fi

# Verify project-specific domains if configured
if [ -n "$FIREWALL_EXTRA_DOMAINS" ]; then
    first_extra="${EXTRA[0]}"
    if ! curl -s --connect-timeout 5 -o /dev/null -w '%{http_code}' "https://$first_extra" | grep -qE '^[2-4][0-9][0-9]$'; then
        echo "ERROR: Firewall verification failed - unable to reach $first_extra"
        exit 1
    else
        echo "Firewall verification passed - able to reach $first_extra as expected"
    fi
fi

echo "=== Firewall init completed successfully at $(date) ==="
