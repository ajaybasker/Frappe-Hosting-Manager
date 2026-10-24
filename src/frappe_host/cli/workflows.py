"""CLI commands for setup, lifecycle, production, publishing, scheduling and tooling."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from frappe_host.application import backups, diagnostics, domains, lifecycle, production, schedule, setup, sites
from frappe_host.application.state import Store
from frappe_host.cli import output as out
from frappe_host.cli.common import (
    EXIT_FAILED, EXIT_INVALID, EXIT_NOT_FOUND, EXIT_OK, resolve_bench, run_plan,
)
from frappe_host.domain.models import Status, overall
from frappe_host.domain.validation import ValidationError, existing_directory
from frappe_host.policies.confirm import require_typed_confirmation


def _checks_out(args, checks, title: str | None = None) -> None:
    if args.json:
        out.print_json({"overall": overall(checks), "checks": [c.to_dict() for c in checks]})
    else:
        if title:
            print(title)
        out.print_checks(checks)
        print(f"\nOverall: {overall(checks)}")


# ---------------------------------------------------------------- setup

def cmd_init(args, store: Store) -> int:
    plan = setup.plan_init(args.branch, args.production, args.secure_mariadb)
    rc = run_plan(args, store, plan, "host.init", args.branch)
    if rc == EXIT_OK and not args.dry_run and [s for s in plan.steps if not s.manual]:
        out.info("Next: open a new terminal (so ~/.local/bin is on PATH), then:")
        out.info(f"  frappe-host bench create frappe-bench --frappe-branch {args.branch}")
    return rc


def cmd_bench_create(args, store: Store) -> int:
    parent = existing_directory(args.dir or str(Path.home()))
    plan, target = setup.plan_create_bench(store, args.name, args.frappe_branch, parent, args.python)
    rc = run_plan(args, store, plan, "bench.create", str(target))
    if rc == EXIT_OK and not args.dry_run:
        out.info("Next: create a site with: frappe-host site create mysite.local --bench " + str(target))
    return rc


def cmd_bench_update(args, store: Store) -> int:
    bench = resolve_bench(store, args.path)
    plan = setup.plan_update_bench(store, bench, not args.no_backup)
    return run_plan(args, store, plan, "bench.update", str(bench))


# ---------------------------------------------------------------- site lifecycle

def cmd_site_delete(args, store: Store) -> int:
    bench, site = sites.resolve_site(store, args.site, args.bench)
    plan = lifecycle.plan_delete(store, bench, site, not args.no_backup)
    return run_plan(args, store, plan, "site.delete", f"{bench}:{site}", confirm_name=site, confirm_what="deletion of the site")


def cmd_site_migrate(args, store: Store) -> int:
    bench, site = sites.resolve_site(store, args.site, args.bench)
    return run_plan(args, store, lifecycle.plan_migrate(store, bench, site, not args.no_backup), "site.migrate", f"{bench}:{site}")


def cmd_site_maintenance(args, store: Store) -> int:
    bench, site = sites.resolve_site(store, args.site, args.bench)
    return run_plan(args, store, lifecycle.plan_maintenance(bench, site, args.state), "site.maintenance", f"{site}:{args.state}")


def cmd_site_hosts(args, store: Store) -> int:
    site = lifecycle.validate_site(args.site)
    return run_plan(args, store, lifecycle.plan_hosts(site), "site.hosts", site)


def cmd_site_apps(args, store: Store) -> int:
    bench, site = sites.resolve_site(store, args.site, args.bench)
    ok, text = lifecycle.list_site_apps(bench, site)
    if args.json:
        out.print_json({"site": site, "ok": ok, "output": text})
    else:
        print(text)
    return EXIT_OK if ok else EXIT_FAILED


# ---------------------------------------------------------------- apps

def cmd_app_get(args, store: Store) -> int:
    bench = resolve_bench(store, args.bench)
    return run_plan(args, store, lifecycle.plan_get_app(store, bench, args.source, args.branch), "app.get", f"{bench}:{args.source}")


def cmd_app_install(args, store: Store) -> int:
    bench, site = sites.resolve_site(store, args.site, args.bench)
    plan = lifecycle.plan_install_app(store, bench, site, args.app, not args.no_backup)
    return run_plan(args, store, plan, "app.install", f"{site}:{args.app}")


# ---------------------------------------------------------------- production

def cmd_production(args, store: Store) -> int:
    bench = resolve_bench(store, args.bench)
    if args.production_cmd == "status":
        checks = production.status(bench)
        if args.json:
            out.print_json({"checks": [c.to_dict() for c in checks]})
        else:
            out.print_checks(checks)
            enabled = any(c.name == "mode" and c.status == Status.PASS for c in checks)
            problems = [c for c in checks if c.status in (Status.FAIL, Status.WARN)]
            print(f"\nProduction mode: {'enabled' if enabled else 'not enabled'}"
                  + (f"; {len(problems)} problem(s)" if problems else ""))
        return EXIT_OK
    if args.production_cmd == "enable":
        rc = run_plan(args, store, production.plan_enable(store, bench), "production.enable", str(bench))
        if rc == EXIT_OK and not args.dry_run:
            out.info("Status now:")
            out.print_checks(production.status(bench))
        return rc
    return run_plan(args, store, production.plan_disable(store, bench), "production.disable", str(bench))


# ---------------------------------------------------------------- domains / publishing

def cmd_domain_check(args, store: Store) -> int:
    domain = domains.domain_name(args.domain)
    bench = site = None
    if args.site:
        bench, site = sites.resolve_site(store, args.site, args.bench)
    _checks_out(args, domains.check_domain(domain, bench=bench, site=site, detect_public_ip=args.detect_public_ip))
    return EXIT_OK


def cmd_probe(args, store: Store) -> int:
    checks = domains.probe(args.url)
    _checks_out(args, checks, f"Probing {args.url} from THIS machine's network")
    return EXIT_OK if all(c.status != Status.FAIL for c in checks) else EXIT_FAILED


def cmd_publish(args, store: Store) -> int:
    bench, site = sites.resolve_site(store, args.site, args.bench)
    domain = domains.domain_name(args.domain)
    email = domains.email(args.email) if args.email else None
    plan, checks = domains.plan_publish(store, bench, site, domain, not args.no_tls, email)
    out.info("Security checklist:")
    for c in checks:
        out.info(f"  {out.tag(c.status, False)} {c.name}: {c.summary}")
    out.info("")
    return run_plan(args, store, plan, "site.publish", f"{site}:{domain}", confirm_name=domain,
                    confirm_what="exposing this site on the internet")


def cmd_unpublish(args, store: Store) -> int:
    bench, site = sites.resolve_site(store, args.site, args.bench)
    domain = domains.domain_name(args.domain)
    return run_plan(args, store, domains.plan_unpublish(store, bench, site, domain), "site.unpublish", f"{site}:{domain}")


# ---------------------------------------------------------------- backups: retention, import, schedule

def cmd_backup_prune(args, store: Store) -> int:
    bench, site = sites.resolve_site(store, args.site, args.bench)
    victims = backups.plan_prune(store, site, bench, args.keep)
    if not victims:
        out.info(f"Nothing to prune: {site} has {args.keep} or fewer frappe-host backups.")
        return EXIT_OK
    out.info(f"Plan: keep the newest {args.keep} frappe-host backups of {site}; DELETE:")
    for r in victims:
        out.info(f"  {r['id']}  {r['created_at']}  {out.human_size(r['total_size'])}"
                 + ("  (+ copies)" if any(f.get("copy") for f in r["files"]) else ""))
    out.info("  Backup files not recorded by frappe-host are never touched.")
    if args.dry_run:
        out.info("Dry run: nothing was changed.")
        return EXIT_OK
    require_typed_confirmation(site, "deleting these backups", args.confirm)
    op, freed = backups.prune(store, site, victims)
    out.info(f"Deleted {len(victims)} backup(s), freed {out.human_size(freed)} (operation {op}).")
    return EXIT_OK


def cmd_backup_import(args, store: Store) -> int:
    bench, site = sites.resolve_site(store, args.site, args.bench)
    known = {f["name"] for r in backups.list_backups(store, site) for f in r["files"]}
    pending = backups.unmanaged_backups(bench, site, known)
    if not pending:
        out.info("No unrecorded backup files found.")
        return EXIT_OK
    out.info(f"Plan: record {len(pending)} existing backup file(s) of {site} with SHA-256 checksums (files are not changed)")
    if args.dry_run:
        out.info("Dry run: nothing was changed.")
        return EXIT_OK
    from frappe_host.cli.common import confirm_proceed

    confirm_proceed(args)
    imported = backups.import_unmanaged(store, bench, site)
    if args.json:
        out.print_json(imported)
    else:
        for i in imported:
            print(f"  {i['id']}  set {i['prefix']}  ({i['files']} files)")
        print("Checksums prove integrity from now on, not since the files were created. Verify with `backup verify ID`.")
    return EXIT_OK


def cmd_backup_schedule(args, store: Store) -> int:
    bench, site = sites.resolve_site(store, args.site, args.bench)
    copy_to = None
    if args.copy_to:
        copy_to = Path(args.copy_to).expanduser().resolve()
        backups.validate_copy_to(bench, copy_to)
    entry = schedule.render_entry(site, bench, args.at, copy_to, args.keep)
    out.info(f"Plan: add to your crontab (daily at {args.at}):\n  {entry[1]}")
    if args.dry_run:
        out.info("Dry run: nothing was changed.")
        return EXIT_OK
    from frappe_host.cli.common import confirm_proceed

    confirm_proceed(args)
    _, op = schedule.set_schedule(store, site, bench, args.at, copy_to, args.keep)
    out.info(f"Scheduled (operation {op}). Output is appended to {schedule.state_home() / 'scheduled-backups.log'}")
    return EXIT_OK


def cmd_backup_schedules(args, store: Store) -> int:
    entries = schedule.list_entries()
    if args.json:
        out.print_json([e.__dict__ for e in entries])
    elif not entries:
        print("No scheduled backups.")
    else:
        out.print_table([[e.site, e.bench, e.at, e.copy_to or "-", e.keep or "-"] for e in entries],
                        ["SITE", "BENCH", "DAILY AT", "COPY TO", "KEEP"])
    return EXIT_OK


def cmd_backup_unschedule(args, store: Store) -> int:
    bench, site = sites.resolve_site(store, args.site, args.bench)
    op = schedule.remove_schedule(store, site, bench)
    print(f"Removed the scheduled backup of {site} (operation {op})." if op else f"{site} had no scheduled backup.")
    return EXIT_OK


# ---------------------------------------------------------------- tooling

def cmd_diagnostics(args, store: Store) -> int:
    files = diagnostics.collect(store)
    out.info("The bundle will contain (all text redacted):")
    for name, text in sorted(files.items()):
        out.info(f"  {name:<52} {out.human_size(len(text.encode()))}")
    if args.preview:
        for name, text in sorted(files.items()):
            print(f"\n===== {name} =====\n{text}")
        return EXIT_OK
    target = Path(args.output or f"frappe-host-diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz").expanduser().resolve()
    if target.exists():
        raise ValidationError(f"{target} already exists")
    diagnostics.write_bundle(files, target)
    out.info(f"Wrote {target} (mode 0600). Nothing was uploaded; review it before sharing.")
    return EXIT_OK


def cmd_check(args, store: Store) -> int:
    checks = diagnostics.check(store, args.max_backup_age)
    failing = [c for c in checks if c.status == Status.FAIL]
    if args.json:
        out.print_json({"ok": not failing, "checks": [c.to_dict() for c in checks]})
    elif failing:
        for c in failing:
            print(f"FAIL {c.name}: {c.summary}")
    elif not args.quiet:
        print(f"OK ({len(checks)} checks)")
    return EXIT_FAILED if failing else EXIT_OK


def cmd_dashboard(args, store: Store) -> int:
    from frappe_host.cli import dashboard

    srv, token = dashboard.serve(args.port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/?token={token}"
    print(f"Dashboard (read-only, this computer only): {url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return EXIT_OK


# ---------------------------------------------------------------- parser

def register(sub, add, mutating, bench_sub, site_sub, backup_sub) -> None:
    sp = add(sub, "init", cmd_init, "prepare this computer: system packages, MariaDB, Redis, Python, Node.js, yarn, bench")
    sp.add_argument("--branch", default="version-16", choices=sorted(setup.PYTHON_FOR_BRANCH))
    sp.add_argument("--production", action="store_true", help="also install NGINX, Supervisor and certbot")
    sp.add_argument("--secure-mariadb", action="store_true", help="run mariadb-secure-installation (set the root password)")
    mutating(sp)

    sp = add(bench_sub, "create", cmd_bench_create, "create a new Bench with bench init and register it")
    sp.add_argument("name")
    sp.add_argument("--frappe-branch", default="version-16", choices=sorted(setup.PYTHON_FOR_BRANCH))
    sp.add_argument("--dir", help="parent directory (default: your home)")
    sp.add_argument("--python", help="Python version, e.g. 3.14 (default: the branch's)")
    mutating(sp)
    sp = add(bench_sub, "update", cmd_bench_update, "back up every site, then bench update")
    sp.add_argument("path", nargs="?")
    sp.add_argument("--no-backup", action="store_true")
    mutating(sp)

    sp = add(site_sub, "delete", cmd_site_delete, "back up, then drop a site (destructive)")
    sp.add_argument("site")
    sp.add_argument("--bench")
    sp.add_argument("--no-backup", action="store_true")
    sp.add_argument("--confirm", metavar="SITE")
    sp.add_argument("--dry-run", action="store_true")
    sp = add(site_sub, "migrate", cmd_site_migrate, "back up, then bench migrate")
    sp.add_argument("site")
    sp.add_argument("--bench")
    sp.add_argument("--no-backup", action="store_true")
    mutating(sp)
    sp = add(site_sub, "maintenance", cmd_site_maintenance, "turn maintenance mode on or off")
    sp.add_argument("site")
    sp.add_argument("state", choices=["on", "off"])
    sp.add_argument("--bench")
    mutating(sp)
    sp = add(site_sub, "hosts", cmd_site_hosts, "add 127.0.0.1 SITE to /etc/hosts (idempotent)")
    sp.add_argument("site")
    mutating(sp)
    sp = add(site_sub, "apps", cmd_site_apps, "apps installed on a site (asks Frappe)")
    sp.add_argument("site")
    sp.add_argument("--bench")

    app = sub.add_parser("app", help="get and install Frappe apps").add_subparsers(dest="app_cmd", metavar="ACTION", required=True)
    sp = add(app, "get", cmd_app_get, "download an app into a Bench (bench get-app)")
    sp.add_argument("source", help="app name (e.g. erpnext) or https:// git URL")
    sp.add_argument("--branch")
    sp.add_argument("--bench")
    mutating(sp)
    sp = add(app, "install", cmd_app_install, "install an app on a site (backs up first)")
    sp.add_argument("site")
    sp.add_argument("app")
    sp.add_argument("--bench")
    sp.add_argument("--no-backup", action="store_true")
    mutating(sp)

    prod = sub.add_parser("production", help="Supervisor + NGINX production mode").add_subparsers(dest="production_cmd", metavar="ACTION", required=True)
    for name, helptext in (("status", "production health"), ("enable", "enable production mode"), ("disable", "return to development mode")):
        sp = add(prod, name, cmd_production, helptext)
        sp.add_argument("bench", nargs="?", help="Bench path (default: the only registered Bench)")
        if name != "status":
            mutating(sp)

    dom = sub.add_parser("domain", help="domain, DNS, TLS and network diagnostics").add_subparsers(dest="domain_cmd", metavar="ACTION", required=True)
    sp = add(dom, "check", cmd_domain_check, "diagnose whether a domain can reach a site on this computer")
    sp.add_argument("domain")
    sp.add_argument("--site")
    sp.add_argument("--bench")
    sp.add_argument("--detect-public-ip", action="store_true", help=f"ask {domains.network.DEFAULT_PUBLIC_IP_SERVICE} for your public IP")

    sp = add(sub, "probe", cmd_probe, "test a URL from THIS machine's network (run it from outside your LAN)")
    sp.add_argument("url")

    sp = add(sub, "publish", cmd_publish, "serve a site at a public domain with HTTPS (guarded)")
    sp.add_argument("site")
    sp.add_argument("--domain", required=True)
    sp.add_argument("--email", help="Let's Encrypt account email (expiry notices)")
    sp.add_argument("--no-tls", action="store_true", help="HTTP only (testing)")
    sp.add_argument("--bench")
    sp.add_argument("--confirm", metavar="DOMAIN")
    sp.add_argument("--dry-run", action="store_true")
    sp = add(sub, "unpublish", cmd_unpublish, "stop serving a site at a domain")
    sp.add_argument("site")
    sp.add_argument("--domain", required=True)
    sp.add_argument("--bench")
    mutating(sp)

    sp = add(backup_sub, "prune", cmd_backup_prune, "delete old frappe-host backups, keeping the newest N")
    sp.add_argument("site")
    sp.add_argument("--keep", type=int, required=True)
    sp.add_argument("--bench")
    sp.add_argument("--confirm", metavar="SITE")
    sp.add_argument("--dry-run", action="store_true")
    sp = add(backup_sub, "import", cmd_backup_import, "record existing (e.g. Frappe scheduled) backups with checksums")
    sp.add_argument("site")
    sp.add_argument("--bench")
    mutating(sp)
    sp = add(backup_sub, "schedule", cmd_backup_schedule, "daily backup via your crontab")
    sp.add_argument("site")
    sp.add_argument("--at", required=True, metavar="HH:MM")
    sp.add_argument("--copy-to", metavar="DIR")
    sp.add_argument("--keep", type=int, help="also prune to the newest N")
    sp.add_argument("--bench")
    mutating(sp)
    add(backup_sub, "schedules", cmd_backup_schedules, "list scheduled backups")
    sp = add(backup_sub, "unschedule", cmd_backup_unschedule, "remove a scheduled backup")
    sp.add_argument("site")
    sp.add_argument("--bench")

    sp = add(sub, "diagnostics", cmd_diagnostics, "write a redacted diagnostics bundle (never uploaded)")
    sp.add_argument("--output", metavar="FILE")
    sp.add_argument("--preview", action="store_true", help="print the bundle contents instead of writing it")
    sp = add(sub, "check", cmd_check, "fast health check for cron/monitoring (exit 1 on problems)")
    sp.add_argument("--max-backup-age", type=int, default=26, metavar="HOURS")
    sp.add_argument("--quiet", action="store_true")
    sp = add(sub, "dashboard", cmd_dashboard, "read-only web dashboard on 127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
