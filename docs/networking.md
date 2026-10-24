# Networking: local, LAN and public access

## Three reachability levels

| Level | Meaning | What frappe-host checks today |
|---|---|---|
| **Localhost** | The site answers on this computer | `site status` sends `GET /api/method/ping` to `127.0.0.1:<webserver_port>` with the site's `Host` header and requires Frappe's `pong` |
| **LAN** | Other devices on your network can reach it | `doctor` and `site status` report whether the port listens beyond loopback. **This is not tested from another device.** A host or router firewall can still block access, so the result is reported as `unknown`. |
| **Public internet** | Anyone on the internet can reach it | `domain check` collects evidence (DNS, NAT/CGNAT, ports, TLS) but always reports public reachability as *not verified*. Only `frappe-host probe URL`, run from **another network** (for example a phone hotspot), tests it. |

A pass at one level never implies the next.

## Exposure checks (implemented)

`doctor` reads `/proc/net/tcp` and `/proc/net/tcp6` and reports:

- **FAIL**: MariaDB (3306), PostgreSQL (5432), system Redis (6379), or the Bench's own Redis ports (from `common_site_config.json`) listening on a non-loopback address. These must never be reachable from outside the host.
- **WARN**: the Bench web port or Socket.IO port listening on all interfaces. That is expected in development mode, when you want LAN access, but you should know about it.
- The UFW state is read from `/etc/ufw/ufw.conf`. Live rules need root and are not read.

frappe-host never changes bind addresses or firewall rules.

## Domain diagnostics (implemented)

`frappe-host domain check DOMAIN [--site SITE] [--detect-public-ip]` reports:

- **DNS:** A/AAAA records, and whether they only point to private (LAN-only) addresses.
- **Network position:** a public address directly on this computer, a CGNAT address (100.64.0.0/10) on an interface, or a private address behind a router (port forwarding needed).
- **Public IP** (only with `--detect-public-ip`, which sends one request to api.ipify.org): CGNAT range detection, and a comparison with the DNS A record. A mismatch can mean a stale record, a second NAT, or a VPN.
- **Ports 80/443:** listeners and their binding. UFW state is read from its config file.
- With `--site`: whether the domain is bound to the site and present in the Bench's NGINX config.
- **TLS:** a fully verified handshake to 127.0.0.1:443 with the domain as SNI, plus days until expiry and whether `certbot.timer` is active.

Double NAT can't be detected reliably from inside the network. Treat a public-IP mismatch as a hint, not proof.

## Publishing (implemented)

`frappe-host publish SITE --domain DOMAIN [--email EMAIL]` is described in the README (§8) and [ADR 0004](adr/0004-production-and-publishing.md).

## Public hosting prerequisites

Public access requires **all** of the following, and several are outside this tool's control:

1. A domain or other public endpoint, with DNS pointing at a reachable address.
2. A reachable public IPv4/IPv6 path. **CGNAT** (a WAN address in 100.64.0.0/10, or an ISP that shares addresses) usually prevents inbound IPv4 entirely. You would then need IPv6, a public IP from your ISP, or an optional relay/tunnel, which is an external dependency.
3. Router port forwarding of 80/443 only to this host. frappe-host will not try to configure arbitrary routers.
4. Host firewall rules allowing only 80/443 publicly.
5. A reverse proxy (NGINX) with valid HTTPS certificates and working renewal.
6. A host that stays powered on and connected. Residential upload bandwidth and uptime carry no guarantees.

"No cloud server required" is not the same as "no external service of any kind". A domain registrar, a DNS provider and your ISP are still involved for public access. Local and LAN hosting need none of them.

## Manual steps you may need

- Router port forwarding: check your router's admin page or manual.
- CGNAT: ask your ISP for a public IPv4 address, or use IPv6.
- DNS records: set them at your DNS provider.
