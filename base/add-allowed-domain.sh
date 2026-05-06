#!/bin/bash
# Idempotently add a domain to this container's live firewall allowlist.
#
# Invoked from the Harbor by `drydock daemon` when a NETWORK_REACH capability is
# granted (see docs/design/network-reach.md). Can also be run manually
# for one-off opens during interactive debugging.
#
# Usage:
#     add-allowed-domain.sh <domain> [<port>]
#
# Behavior:
#   1. Validates domain shape (cheap defense vs. injection from a
#      compromised RPC path).
#   2. Appends to /tmp/firewall-domains.txt if not already present —
#      the periodic refresher (refresh-firewall-allowlist.sh) picks
#      it up on the next cycle so CDN-rotated IPs stay current.
#   3. Resolves A records and adds each to the `allowed-domains` ipset
#      via `ipset add -exist`. Synchronous — caller learns whether the
#      domain is reachable now.
#   4. For ports other than 80/443, adds an iptables OUTPUT rule for
#      tcp/<port> matched against the ipset. (80/443 are pre-opened
#      by init-firewall.sh.)
#
# Exit codes:
#   0  domain reachable; >=1 IP added or already present
#   2  bad usage / invalid domain
#   3  DNS resolution returned no A records
#   4  ipset/iptables operation failed
#
# Output: JSON to stdout describing what happened. Designed to be
# parsed by the calling daemon and surfaced in the capability lease.

set -u

DOMAINS_FILE=/tmp/firewall-domains.txt
LOG=/tmp/firewall-add.log

usage() {
    echo "usage: add-allowed-domain.sh <domain> [<port>]" >&2
    exit 2
}

[ $# -ge 1 ] && [ $# -le 2 ] || usage

DOMAIN="$1"
PORT="${2:-443}"

# Domain shape: lowercase letters, digits, dot, hyphen. 1..253 chars.
# Reject anything that looks like shell metachars or path traversal.
if ! [[ "$DOMAIN" =~ ^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)+$ ]]; then
    echo '{"ok":false,"error":"invalid_domain","detail":"domain failed shape validation"}'
    exit 2
fi

# Port: integer 1..65535
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo '{"ok":false,"error":"invalid_port","detail":"port out of range"}'
    exit 2
fi

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "$(ts) $*" >> "$LOG"; }

# Append to the durable list so the periodic refresher will keep
# resolving this domain even after IPs rotate. Idempotent.
touch "$DOMAINS_FILE"
if ! grep -Fxq "$DOMAIN" "$DOMAINS_FILE"; then
    echo "$DOMAIN" >> "$DOMAINS_FILE"
    log "appended $DOMAIN to $DOMAINS_FILE"
fi

# Resolve. Short timeout — we hold the RPC caller's connection.
IPS=$(dig +noall +answer +time=3 +tries=1 A "$DOMAIN" 2>/dev/null | awk '$4 == "A" {print $5}')
if [ -z "$IPS" ]; then
    echo "{\"ok\":false,\"error\":\"dns_resolution_failed\",\"domain\":\"$DOMAIN\"}"
    log "no A records for $DOMAIN"
    exit 3
fi

added=()
existing=()
fail=""
while IFS= read -r ip; do
    [[ "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || continue
    if sudo ipset test allowed-domains "$ip" 2>/dev/null; then
        existing+=("$ip")
    elif sudo ipset add allowed-domains "$ip" -exist 2>/dev/null; then
        added+=("$ip")
        log "+$ip for $DOMAIN"
    else
        fail="$ip"
        break
    fi
done <<< "$IPS"

if [ -n "$fail" ]; then
    echo "{\"ok\":false,\"error\":\"ipset_add_failed\",\"ip\":\"$fail\",\"domain\":\"$DOMAIN\"}"
    exit 4
fi

# Non-default port: install an OUTPUT rule for it (80/443 already open).
if [ "$PORT" != "443" ] && [ "$PORT" != "80" ]; then
    if ! sudo iptables -C OUTPUT -p tcp --dport "$PORT" -m set --match-set allowed-domains dst -j ACCEPT 2>/dev/null; then
        if ! sudo iptables -I OUTPUT -p tcp --dport "$PORT" -m set --match-set allowed-domains dst -j ACCEPT 2>/dev/null; then
            echo "{\"ok\":false,\"error\":\"iptables_rule_failed\",\"port\":$PORT}"
            exit 4
        fi
        log "opened tcp/$PORT for allowed-domains"
    fi
fi

# Compose JSON arrays without depending on jq.
join_json() {
    local first=1 out="["
    for x in "$@"; do
        [ $first -eq 1 ] && first=0 || out+=","
        out+="\"$x\""
    done
    out+="]"
    echo "$out"
}

added_json=$(join_json "${added[@]}")
existing_json=$(join_json "${existing[@]}")

cat <<EOF
{"ok":true,"domain":"$DOMAIN","port":$PORT,"added":$added_json,"already_present":$existing_json}
EOF
exit 0
