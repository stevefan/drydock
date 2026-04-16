#!/bin/bash
# Periodic re-resolution of whitelisted domains against CDN IP rotation.
#
# Problem this solves: init-firewall.sh resolves domains ONCE at container
# startup. CDN-fronted domains (Akamai, Cloudflare, CloudFront) rotate IPs
# on short TTLs for load balancing / geo-routing. After hours-to-days, the
# resolved IPs in the ipset no longer match current DNS; new connections
# get REJECTed. Silent rot. Surfaced 2026-04-15 via govdeals.com — the
# dumb-operator crashed because the Akamai-fronted www.govdeals.com had
# rotated to a new /16 subnet that wasn't whitelisted.
#
# This script runs as a background supervisor, started at the tail of
# init-firewall.sh. Every N minutes it re-resolves every whitelisted domain
# and adds any new IPs to the allowed-domains ipset.
#
# Design choices:
# - Additive only. Never remove. Stale IPs in the set don't cause false
#   ACCEPTs (the CDN no longer routes to them anyway), but protect against
#   momentary DNS hiccups.
# - Silent on DNS failure. Just skip and try next iteration.
# - Idempotent via `ipset -exist`.
# - Logs only when something was actually added — quiet most of the time.
# - Reads the effective domain list from /tmp/firewall-domains.txt, which
#   init-firewall.sh writes after it computes BASE_DOMAINS + project extras.

set -u

LOG=/tmp/firewall-refresh.log
DOMAINS_FILE=/tmp/firewall-domains.txt
INTERVAL=${FIREWALL_REFRESH_INTERVAL:-900}  # 15 min default; override via env

if [ ! -f "$DOMAINS_FILE" ]; then
    echo "$(date): $DOMAINS_FILE not found; refresh loop will not start" | tee -a "$LOG"
    exit 1
fi

echo "$(date): refresh loop starting (interval=${INTERVAL}s, domains=$(wc -l <"$DOMAINS_FILE"))" | tee -a "$LOG"

while true; do
    sleep "$INTERVAL"
    added=0
    checked=0
    while IFS= read -r domain; do
        [ -z "$domain" ] && continue
        checked=$((checked + 1))
        ips=$(dig +noall +answer +time=3 +tries=1 A "$domain" 2>/dev/null | awk '$4 == "A" {print $5}' || true)
        [ -z "$ips" ] && continue
        while IFS= read -r ip; do
            if [[ "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
                if ! sudo ipset test allowed-domains "$ip" 2>/dev/null; then
                    if sudo ipset add allowed-domains "$ip" -exist 2>/dev/null; then
                        added=$((added + 1))
                        echo "$(date): +$ip for $domain" >> "$LOG"
                    fi
                fi
            fi
        done <<< "$ips"
    done < "$DOMAINS_FILE"
    # Quiet log when nothing changes; only mark cycle boundaries if work happened
    if [ "$added" -gt 0 ]; then
        echo "$(date): cycle added $added new IPs across $checked domains" >> "$LOG"
    fi
done
