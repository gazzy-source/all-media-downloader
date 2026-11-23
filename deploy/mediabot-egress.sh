#!/usr/bin/env bash
# Owner-matched OUTPUT rules for the bot user. Safe to re-run.
set -euo pipefail
UID_BOT=$(id -u mediabot)
# Fail closed: if any rule below fails, the bot reaches nothing rather than
# running with a half-built chain.
trap 'iptables -I MEDIABOT_EGRESS 1 -j REJECT 2>/dev/null; ip6tables -I MEDIABOT_EGRESS 1 -j REJECT 2>/dev/null' ERR
for ipt in iptables ip6tables; do
    $ipt -N MEDIABOT_EGRESS 2>/dev/null || $ipt -F MEDIABOT_EGRESS
    $ipt -C OUTPUT -m owner --uid-owner "$UID_BOT" -j MEDIABOT_EGRESS 2>/dev/null \
        || $ipt -I OUTPUT 1 -m owner --uid-owner "$UID_BOT" -j MEDIABOT_EGRESS
done
# IPv4: allow exactly what the bot needs locally, refuse every other inward address
iptables -A MEDIABOT_EGRESS -d 127.0.0.53,127.0.0.54 -p udp --dport 53 -j RETURN
iptables -A MEDIABOT_EGRESS -d 127.0.0.53,127.0.0.54 -p tcp --dport 53 -j RETURN
iptables -A MEDIABOT_EGRESS -d 127.0.0.1 -p tcp --dport 40000 -j RETURN   # WARP SOCKS
iptables -A MEDIABOT_EGRESS -d 127.0.0.1 -p tcp --dport 4416 -j RETURN    # PO-token provider
for net in 127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 \
           100.64.0.0/10 0.0.0.0/8 224.0.0.0/4 255.255.255.255/32; do
    iptables -A MEDIABOT_EGRESS -d "$net" -j REJECT
done
# Any address of this host itself (e.g. its own interface IPs).
iptables -A MEDIABOT_EGRESS -m addrtype --dst-type LOCAL -j REJECT
# IPv6: loopback, ULA (incl. the tailnet fd7a::/48), link-local,
# plus multicast, IPv4-mapped and NAT64 forms of the IPv4 ranges above.
for net in ::1/128 fc00::/7 fe80::/10 ff00::/8 ::ffff:0:0/96 64:ff9b::/96; do
    ip6tables -A MEDIABOT_EGRESS -d "$net" -j REJECT
done
ip6tables -A MEDIABOT_EGRESS -m addrtype --dst-type LOCAL -j REJECT
