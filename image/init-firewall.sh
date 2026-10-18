#!/bin/bash
set -euo pipefail  # Exit on error, undefined vars, and pipeline failures
IFS=$'\n\t'       # Stricter word splitting

# 1. Extract Docker DNS info BEFORE any flushing
DOCKER_DNS_RULES=$(iptables-save -t nat | grep "127\.0\.0\.11" || true)

# The container's original resolvers become dnsmasq's upstreams (IPv4 only, at most 3). Read once:
# the file is rewritten below and a restart must not pick up 127.0.0.1.
DNS_STATE=/run/hands-dns
if [ -f "$DNS_STATE/upstream" ]; then
    UPSTREAMS=$(cat "$DNS_STATE/upstream")
else
    UPSTREAMS=$(awk '$1 == "nameserver" && $2 ~ /^[0-9.]+$/ {print $2}' /etc/resolv.conf | head -3)
fi
if [ -z "$UPSTREAMS" ]; then
    echo "ERROR: no IPv4 nameserver in /etc/resolv.conf"
    exit 1
fi

# Flush existing rules and delete existing ipsets
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

# DNS: everything goes through a root-owned dnsmasq on 127.0.0.1:53 that forwards only allowlisted
# names. Only root (dnsmasq, and this script) may reach the upstream resolvers; any other port-53
# egress is dropped. These rules come before the loopback and host-network accepts below.
pkill -x dnsmasq 2>/dev/null || true
for proto in udp tcp; do
    iptables -A OUTPUT -o lo -d 127.0.0.1 -p "$proto" --dport 53 -j ACCEPT
done
while read -r up; do
    if [ "$up" = 127.0.0.11 ]; then
        # Docker's embedded resolver: its nat rules DNAT port 53 to a random port, so match the address
        iptables -A OUTPUT -d "$up" -m owner --uid-owner 0 -j ACCEPT
        iptables -A OUTPUT -d "$up" -j DROP
    else
        for proto in udp tcp; do
            iptables -A OUTPUT -d "$up" -p "$proto" --dport 53 -m owner --uid-owner 0 -j ACCEPT
        done
    fi
done <<< "$UPSTREAMS"
for proto in udp tcp; do
    iptables -A OUTPUT -p "$proto" --dport 53 -j DROP
done
# Allow localhost
iptables -A INPUT -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT

# Create ipset with CIDR support (dnsmasq adds the addresses of allowlisted names at query time)
ipset create allowed-domains hash:net

# Allowlisted names: what Claude Code and the toolchain need, plus the spec repo's allowed-domains.
# No telemetry hosts (sentry.io, statsig.com): the runner sets CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1,
# and with suffix matching their ingest subdomains would be a free exfiltration route.
ALLOWED_NAMES=$(printf '%s\n' \
    "api.anthropic.com" \
    "registry.npmjs.org"
    cat /etc/hands/allowed-domains 2>/dev/null || true)

mkdir -p "$DNS_STATE"
chown root:root "$DNS_STATE"
chmod 755 "$DNS_STATE"
printf '%s\n' "$UPSTREAMS" > "$DNS_STATE/upstream"
# shellcheck disable=SC2086  # one argument per upstream (IFS is newline)
if ! printf '%s\n' "$ALLOWED_NAMES" | /usr/local/bin/hands-dns-conf.sh allowed-domains $UPSTREAMS \
        > "$DNS_STATE/dnsmasq.conf"; then
    echo "ERROR: invalid DNS allowlist (see above)"
    exit 1
fi
chmod 644 "$DNS_STATE/upstream" "$DNS_STATE/dnsmasq.conf"
dnsmasq --test --conf-file="$DNS_STATE/dnsmasq.conf"
dnsmasq --conf-file="$DNS_STATE/dnsmasq.conf"
for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -s "$DNS_STATE/dnsmasq.pid" ] && break
    sleep 0.2
done
if [ ! -s "$DNS_STATE/dnsmasq.pid" ]; then
    echo "ERROR: dnsmasq did not start"
    exit 1
fi
# /etc/resolv.conf is a Docker bind mount: rewrite in place (cannot rename over it)
printf 'nameserver 127.0.0.1\n' > /etc/resolv.conf
chown root:root /etc/resolv.conf
chmod 644 /etc/resolv.conf
echo "DNS filter: dnsmasq on 127.0.0.1:53, upstream $(echo $UPSTREAMS)"

# Resolve the allowed names once through dnsmasq: fails early on a typo, and pre-fills the ipset
for domain in $(printf '%s\n' "$ALLOWED_NAMES" | grep -v '^[[:space:]]*\(#\|$\)' | tr -d ' \r'); do
    echo "Resolving $domain..."
    ips=$(dig @127.0.0.1 +noall +answer A "$domain" | awk '$4 == "A" {print $5}')
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
        ipset add -exist allowed-domains "$ip"
    done < <(echo "$ips")
done

# No host-network allow: nothing in the container talks to the host or its LAN (the runner only
# mounts files and reads exit codes), so the Docker gateway and the host /24 are blocked like everything else.

# Set default policies to DROP first
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP

# First allow established connections for already approved traffic
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# Then allow only specific outbound traffic to allowed domains
iptables -A OUTPUT -m set --match-set allowed-domains dst -j ACCEPT

# Explicitly REJECT all other outbound traffic for immediate feedback
iptables -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited

echo "Firewall configuration complete"
echo "Verifying firewall rules..."
if curl --connect-timeout 5 https://example.com >/dev/null 2>&1; then
    echo "ERROR: Firewall verification failed - was able to reach https://example.com"
    exit 1
else
    echo "Firewall verification passed - unable to reach https://example.com as expected"
fi

# Verify the DNS filter: a name that exists but is not allowlisted must not resolve
if dig @127.0.0.1 +short +time=3 +tries=1 A example.com | grep -q .; then
    echo "ERROR: DNS filter verification failed - example.com resolved"
    exit 1
else
    echo "DNS filter verification passed - example.com does not resolve"
fi

# Verify an allowlisted site is reachable (DNS through dnsmasq, IP added to the set at query time)
if ! curl -s -o /dev/null --connect-timeout 5 https://api.anthropic.com/ 2>/dev/null; then
    echo "ERROR: Firewall verification failed - unable to reach https://api.anthropic.com"
    exit 1
else
    echo "Firewall verification passed - able to reach https://api.anthropic.com as expected"
fi
