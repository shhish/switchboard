"""Explicit IPv4 allowlists shared by configuration validators."""

from ipaddress import IPv4Network

# RFC 1918 private-use networks. Do not substitute the broader is_private property.
RFC1918_NETWORKS = (
    IPv4Network((10 << 24, 8)),
    IPv4Network(((172 << 24) | (16 << 16), 12)),
    IPv4Network(((192 << 24) | (168 << 16), 16)),
)
# RFC 6598 shared address space is allowed only for remote hosts, not LAN probes.
REMOTE_NETWORKS = (*RFC1918_NETWORKS, IPv4Network(((100 << 24) | (64 << 16), 10)))
