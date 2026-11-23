#!/usr/bin/env bash
# Owner-matched OUTPUT rules for the bot user. Safe to re-run.
set -euo pipefail
UID_BOT=$(id -u mediabot)
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
           100.64.0.0/10 0.0.0.0/8 224.0.0.0/4; do
    iptables -A MEDIABOT_EGRESS -d "$net" -j REJECT
done
# IPv6: loopback, ULA (incl. the tailnet fd7a::/48), link-local
for net in ::1/128 fc00::/7 fe80::/10; do
    ip6tables -A MEDIABOT_EGRESS -d "$net" -j REJECT
done
