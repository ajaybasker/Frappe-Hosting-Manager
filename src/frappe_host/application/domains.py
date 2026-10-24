"""Domain, DNS, TLS and public-access diagnostics, and the publish workflow.

Honesty rules: a check made from this computer proves nothing about access
from the internet. Public reachability is only ever reported as verified by
`frappe-host probe`, run from a device on a different network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from frappe_host.adapters import bench_fs, network, process
from frappe_host.application import doctor, production, registry
from frappe_host.application.operations import Operation, OperationFailed
from frappe_host.application.state import Store
from frappe_host.application.steps import Plan, Step
from frappe_host.domain.models import Check, Status
from frappe_host.domain.validation import ValidationError
from frappe_host.platform.linux import host, paths

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def domain_name(value: str) -> str:
    v = (value or "").strip().lower().rstrip(".")
    if not _DOMAIN_RE.match(v):
        raise ValidationError(f"invalid domain {value!r}: use a fully-qualified name such as erp.example.com")
    return v


def email(value: str) -> str:
    if not _EMAIL_RE.match(value or ""):
        raise ValidationError(f"invalid email {value!r}")
    return value


def default_gateway() -> str | None:
    r = process.run(["ip", "-j", "route", "show", "default"], timeout=10)
    try:
        routes = json.loads(r.stdout) if r.ok else []
    except ValueError:
        return None
    return routes[0].get("gateway") if routes else None


def check_domain(domain: str, *, bench: Path | None = None, site: str | None = None,
                 detect_public_ip: bool = False) -> list[Check]:
    sec = f"domain {domain}"
    out: list[Check] = []

    # 1. DNS
    addrs = network.resolve(domain)
    kinds = {a: host.classify_address(a) for a in addrs}
    if not addrs:
        out.append(Check(sec, "dns", Status.FAIL, "does not resolve", {}, f"Create an A (and/or AAAA) record for {domain} at your DNS provider"))
    elif all(k in ("private", "loopback", "link-local") for k in kinds.values()):
        out.append(Check(sec, "dns", Status.WARN, f"resolves only to private addresses {', '.join(addrs)} (LAN-only)",
                         {"addresses": addrs}, "For public access the record must point to your public IP"))
    else:
        out.append(Check(sec, "dns", Status.PASS, f"resolves to {', '.join(addrs)}", {"addresses": addrs}))

    # 2. Local network position
    ifaces = [i for i in host.interfaces() if host.classify_address(i.address) not in ("loopback", "link-local")]
    iface_kinds = {i.address: host.classify_address(i.address) for i in ifaces}
    gw = default_gateway()
    pub_iface = [a for a, k in iface_kinds.items() if k == "public"]
    if any(k == "cgnat" for k in iface_kinds.values()):
        out.append(Check(sec, "cgnat", Status.WARN, "this computer has a 100.64.0.0/10 address: likely carrier-grade NAT",
                         {"interfaces": iface_kinds}, "Inbound IPv4 is usually impossible behind CGNAT; ask your ISP for a public IP, use IPv6, or a relay"))
    if pub_iface:
        out.append(Check(sec, "network", Status.INFO, f"a public address is configured directly on this computer: {', '.join(pub_iface)}"))
    else:
        out.append(Check(sec, "network", Status.INFO,
                         f"this computer is behind a router (private address, gateway {gw or 'unknown'}); "
                         "inbound ports 80/443 must be forwarded to it"))

    # 3. Public IP (opt-in external request)
    public = None
    if detect_public_ip:
        public = network.public_ip()
        if public is None:
            out.append(Check(sec, "public-ip", Status.UNKNOWN, f"could not reach {network.DEFAULT_PUBLIC_IP_SERVICE}"))
        else:
            k = host.classify_address(public)
            if k == "cgnat":
                out.append(Check(sec, "public-ip", Status.FAIL, f"your network's address {public} is in the CGNAT range",
                                 {}, "Direct inbound IPv4 hosting is not possible on this connection"))
            else:
                v4 = [a for a in addrs if ":" not in a]
                if v4 and public not in v4:
                    out.append(Check(sec, "public-ip", Status.WARN, f"DNS points to {', '.join(v4)} but this network's public IP is {public}",
                                     {}, "Update the A record, or you may be behind a second NAT / VPN"))
                elif v4:
                    out.append(Check(sec, "public-ip", Status.PASS, f"DNS A record matches this network's public IP {public}"))
                else:
                    out.append(Check(sec, "public-ip", Status.INFO, f"this network's public IPv4 is {public}"))
    else:
        out.append(Check(sec, "public-ip", Status.UNKNOWN, "public IP not checked (pass --detect-public-ip to ask "
                         f"{network.DEFAULT_PUBLIC_IP_SERVICE}; this sends one request to that service)"))

    # 4. Listeners and firewall
    ls = host.tcp_listeners()
    for port in (80, 443):
        on = [l for l in ls if l.port == port]
        if not on:
            out.append(Check(sec, f"port {port}", Status.WARN if port == 80 else Status.INFO, f"nothing listening on {port}"))
        elif all(l.scope == "loopback" for l in on):
            out.append(Check(sec, f"port {port}", Status.WARN, f"port {port} only listens on loopback"))
        else:
            out.append(Check(sec, f"port {port}", Status.PASS, f"listening on {', '.join(sorted({l.address for l in on}))}"))
    out.append(Check(sec, "firewall", Status.INFO, f"UFW {host.ufw_state()} (config file; live rules need sudo). "
                     "Allow only 80/443 from the internet; never forward database/Redis ports."))

    # 5. Site binding and TLS from this host
    if bench is not None and site is not None:
        domains = [d["domain"] if isinstance(d, dict) else d for d in bench_fs.site_config(bench, site).get("domains", [])]
        bound = domain in domains or domain == site
        out.append(Check(sec, "site", Status.PASS if bound else Status.WARN,
                         f"{'bound to' if bound else 'not yet bound to'} site {site}", {},
                         None if bound else f"frappe-host publish {site} --domain {domain}"))
        out.append(Check(sec, "nginx", Status.PASS if production.nginx_conf_has_domain(bench, domain) else Status.WARN,
                         "server_name present in the Bench NGINX config" if production.nginx_conf_has_domain(bench, domain)
                         else "domain not in the Bench NGINX config"))
    if any(l.port == 443 for l in ls):
        t = network.tls_check("127.0.0.1", 443, domain)
        out.append(Check(sec, "tls", Status.PASS if t.ok and (t.days_left or 0) > 14 else Status.WARN if t.ok else Status.FAIL,
                         t.detail + (f" (issuer {t.issuer})" if t.issuer else ""), {"not_after": t.not_after}))
    out.append(Check(sec, "renewal", Status.PASS if host.systemd_state("certbot.timer") == "active" else Status.INFO,
                     f"certbot.timer: {host.systemd_state('certbot.timer')}"))
    out.append(Check(sec, "external", Status.UNKNOWN, "public reachability NOT verified from this computer",
                     {}, f"From a phone hotspot or another network run: frappe-host probe https://{domain}"))
    return out


def probe(url: str) -> list[Check]:
    """Reachability test from wherever this runs. Meant to be run OUTSIDE the host's network."""
    m = re.match(r"^(https?)://([^/:]+)(?::(\d+))?/?$", url.strip())
    if not m:
        raise ValidationError("give a base URL such as https://erp.example.com")
    scheme, hostname, port = m.group(1), m.group(2).lower(), m.group(3)
    sec = f"probe {url}"
    out = []
    addrs = network.resolve(hostname)
    out.append(Check(sec, "dns", Status.PASS if addrs else Status.FAIL, f"{hostname} -> {', '.join(addrs) or 'no records'}"))
    if not addrs:
        return out
    base = f"{scheme}://{hostname}" + (f":{port}" if port else "")
    r = network.http_get(f"{base}/api/method/ping")
    out.append(Check(sec, "frappe", Status.PASS if r.ok else Status.FAIL,
                     f"GET {base}/api/method/ping -> {r.detail}" + (" pong" if r.frappe_pong else "")))
    if scheme == "https":
        t = network.tls_check(hostname, int(port or 443), hostname)
        out.append(Check(sec, "tls", Status.PASS if t.ok else Status.FAIL, t.detail))
    local = [a for a in addrs if host.classify_address(a) in ("private", "loopback")]
    if r.ok and not local:
        out.append(Check(sec, "verdict", Status.PASS, "reachable from the network this probe ran on. If that network is "
                         "outside the host's LAN (e.g. mobile data), public access is verified."))
    elif r.ok:
        out.append(Check(sec, "verdict", Status.WARN, "reachable, but the name resolves to a private address: this only proves LAN access"))
    else:
        out.append(Check(sec, "verdict", Status.FAIL, "not reachable from this network", {},
                         "Check DNS, router port forwarding (80/443), CGNAT, firewall; then `frappe-host domain check` on the host"))
    return out


def _dpkg_installed(pkg: str) -> bool:
    from frappe_host.application.setup import dpkg_installed

    return dpkg_installed(pkg)


def site_domains(bench: Path, site: str) -> list:
    return list(bench_fs.site_config(bench, site).get("domains", []))


def publish_checklist(store: Store, bench: Path, site: str, domain: str, tls: bool) -> tuple[list[Check], list[str]]:
    """Security gate. Returns (checks, blockers)."""
    sec = "publish checklist"
    checks: list[Check] = []
    blockers: list[str] = []
    mode = bench_fs.detect_process_mode(bench, paths.supervisor_conf_d())
    ok = mode == "supervisor"
    checks.append(Check(sec, "production", Status.PASS if ok else Status.FAIL, f"process mode: {mode}"))
    if not ok:
        blockers.append(f"production mode is not enabled: run `frappe-host production enable {bench}`")
    cfg = bench_fs.common_config(bench)
    if not cfg.get("dns_multitenant"):
        blockers.append("dns_multitenant is off (production enable turns it on)")
    info = bench_fs.inspect_bench(bench)
    exposure = doctor.check_exposure([info])
    exposed = [c for c in exposure if c.status == Status.FAIL]
    checks.append(Check(sec, "internal-ports", Status.FAIL if exposed else Status.PASS,
                        "; ".join(c.summary for c in exposed) or "database and Redis ports bound to loopback"))
    if exposed:
        blockers.append("internal services are listening beyond loopback")
    s = bench_fs.inspect_site(bench, site)
    checks.append(Check(sec, "developer-mode", Status.FAIL if s.developer_mode else Status.PASS,
                        "developer_mode is ON" if s.developer_mode else "developer_mode off"))
    if s.developer_mode:
        blockers.append(f"developer_mode is on for {site}: run `bench --site {site} set-config developer_mode 0`")
    row = store.db.execute("SELECT created_at FROM backups WHERE site=? AND bench_path=? AND verify_result='ok' "
                           "ORDER BY created_at DESC LIMIT 1", (site, str(bench))).fetchone() if store.exists() else None
    checks.append(Check(sec, "backup", Status.PASS if row else Status.WARN,
                        f"latest verified backup {row[0]}" if row else "no verified frappe-host backup",
                        {}, None if row else f"frappe-host backup {site} && frappe-host backup verify ID"))
    addrs = network.resolve(domain)
    checks.append(Check(sec, "dns", Status.PASS if addrs else Status.FAIL, f"{domain} -> {', '.join(addrs) or 'no records'}"))
    if tls and not addrs:
        blockers.append(f"{domain} does not resolve; certificates cannot be issued")
    if tls and not process.which("certbot"):
        blockers.append("certbot is not installed: run `frappe-host init --production`")
    elif tls and process.which("dpkg-query") and not _dpkg_installed("python3-certbot-nginx"):
        blockers.append("certbot's NGINX plugin is missing: run `frappe-host init --production` (python3-certbot-nginx)")
    fw = host.ufw_state()
    checks.append(Check(sec, "firewall", Status.PASS if fw == "enabled" else Status.WARN,
                        f"UFW {fw}" + ("" if fw == "enabled" else ": consider `sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp && sudo ufw enable`")))
    checks.append(Check(sec, "manual", Status.INFO, "You must verify yourself: router forwards ONLY 80 and 443 to this "
                        "computer; Administrator password is strong and unique; this computer stays on and updated"))
    return checks, blockers


def plan_publish(store: Store, bench: Path, site: str, domain: str, tls: bool, email_addr: str | None) -> tuple[Plan, list[Check]]:
    plan = Plan(f"publish {site} at {'https' if tls else 'http'}://{domain}")
    checks, blockers = publish_checklist(store, bench, site, domain, tls)
    plan.blockers += blockers
    exe = process.which("bench") or "bench"
    existing = [d["domain"] if isinstance(d, dict) else d for d in site_domains(bench, site)]
    if domain not in existing and domain != site:
        def undo(op: Operation) -> None:
            # Remove the domain AND regenerate nginx.conf, so the next reload doesn't pick it up.
            for argv in ([exe, "setup", "remove-domain", domain, "--site", site], [exe, "setup", "nginx", "--yes"]):
                op.log(f"RUN {process.display(argv)}")
                if process.run_interactive(argv, cwd=bench) != 0:
                    raise OperationFailed(f"rollback command failed: {process.display(argv)}")

        plan.steps.append(Step(f"add domain {domain} to {site}", [exe, "setup", "add-domain", domain, "--site", site], cwd=bench,
                               undo=Step(f"remove domain {domain} and regenerate nginx.conf", func=undo)))
    else:
        plan.satisfied.append(f"{domain} already configured for {site}")
    plan.steps += [Step("regenerate config/nginx.conf", [exe, "setup", "nginx", "--yes"], cwd=bench),
                   Step("validate NGINX configuration", ["nginx", "-t"], sudo=True, why="nginx -t reads root-only files"),
                   Step("reload NGINX", ["systemctl", "reload", "nginx"], sudo=True, why="reloading a system service")]
    plan.steps.append(Step(f"set host_name to {'https' if tls else 'http'}://{domain} (links in emails)",
                           [exe, "--site", site, "set-config", "host_name", f"{'https' if tls else 'http'}://{domain}"], cwd=bench))
    if tls:
        live = paths.letsencrypt_live() / domain
        cert = ["certbot", "certonly", "--nginx", "-d", domain, "--keep-until-expiring",
                "--deploy-hook", "systemctl reload nginx"]
        cert += ["-m", email_addr, "--agree-tos", "--no-eff-email"] if email_addr else []
        plan.steps.append(Step(f"obtain a Let's Encrypt certificate for {domain} (Let's Encrypt must reach "
                               f"http://{domain} from the internet)", cert, sudo=True, why="certbot writes /etc/letsencrypt"))
        plan.steps.append(Step("re-register the domain with its certificate",
                               [exe, "setup", "remove-domain", domain, "--site", site], cwd=bench))
        plan.steps.append(Step("attach certificate paths", [exe, "setup", "add-domain", domain, "--site", site,
                               "--ssl-certificate", str(live / "fullchain.pem"),
                               "--ssl-certificate-key", str(live / "privkey.pem")], cwd=bench))
        plan.steps += [Step("regenerate config/nginx.conf with HTTPS", [exe, "setup", "nginx", "--yes"], cwd=bench),
                       Step("validate NGINX configuration", ["nginx", "-t"], sudo=True, why="nginx -t reads root-only files"),
                       Step("reload NGINX", ["systemctl", "reload", "nginx"], sudo=True, why="reloading a system service")]
        plan.warnings.append("renewal: the Ubuntu certbot package renews automatically via certbot.timer")
    else:
        plan.warnings.append("without TLS, passwords travel unencrypted; use --no-tls only for testing on a LAN")
    if not any(c.name == "backup" and c.status == Status.PASS for c in checks):
        plan.warnings.append("no verified backup exists; take one before exposing the site")
    plan.steps.append(Step("refresh the registry snapshot", func=lambda op: registry.refresh(store, str(bench))))
    plan.warnings.append(f"public access is NOT verified by this command; afterwards run `frappe-host probe "
                         f"{'https' if tls else 'http'}://{domain}` from another network")
    return plan, checks


def plan_unpublish(store: Store, bench: Path, site: str, domain: str) -> Plan:
    plan = Plan(f"stop serving {site} at {domain}")
    exe = process.which("bench") or "bench"
    existing = [d["domain"] if isinstance(d, dict) else d for d in site_domains(bench, site)]
    if domain not in existing:
        plan.satisfied.append(f"{domain} is not configured for {site}")
        return plan
    plan.steps += [Step(f"remove domain {domain}", [exe, "setup", "remove-domain", domain, "--site", site], cwd=bench),
                   Step("regenerate config/nginx.conf", [exe, "setup", "nginx", "--yes"], cwd=bench),
                   Step("validate NGINX configuration", ["nginx", "-t"], sudo=True, why="nginx -t reads root-only files"),
                   Step("reload NGINX", ["systemctl", "reload", "nginx"], sudo=True, why="reloading a system service"),
                   Step("refresh the registry snapshot", func=lambda op: registry.refresh(store, str(bench)))]
    plan.warnings.append(f"the certificate is kept; delete it with `sudo certbot delete --cert-name {domain}` if unused. "
                         "Remove the router port forward if no other site needs it.")
    return plan
