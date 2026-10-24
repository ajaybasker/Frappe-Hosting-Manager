"""frappe-host command-line entry point.

Exit codes:
  0  success (doctor: READY or NEEDS ATTENTION)
  1  operation failed
  2  invalid input / usage error
  3  doctor found blockers
  4  refused: confirmation missing or did not match
  5  resource not found
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from frappe_host import __version__
from frappe_host.adapters import bench_fs, process
from frappe_host.application import backups, doctor, operations, registry, sites
from frappe_host.application.operations import OperationFailed
from frappe_host.application.state import StateError, Store
from frappe_host.cli import output as out
from frappe_host.domain.models import overall
from frappe_host.domain.validation import ValidationError, existing_directory
from frappe_host.policies.confirm import ConfirmationRefused, require_typed_confirmation

EXIT_OK, EXIT_FAILED, EXIT_INVALID, EXIT_BLOCKED, EXIT_REFUSED, EXIT_NOT_FOUND = 0, 1, 2, 3, 4, 5


class Refused(Exception):
    pass


def confirm_proceed(args: argparse.Namespace, question: str = "Proceed?") -> None:
    """Consent for non-destructive changes: --yes, or an interactive y/N."""
    if args.yes:
        return
    if not sys.stdin.isatty():
        raise Refused("not running interactively; review the plan with --dry-run and re-run with --yes")
    if input(f"{question} [y/N] ").strip().lower() not in ("y", "yes"):
        raise Refused("cancelled; nothing was changed")


# ---------------------------------------------------------------- commands

def cmd_doctor(args, store: Store) -> int:
    paths = [Path(b["path"]) for b in registry.list_benches(store) if Path(b["path"]).is_dir()]
    source = "registered"
    if not paths:
        source = "discovered (read-only scan of your home directory; nothing registered yet)"
        paths = [Path(d.path) for d in registry.discover(None, [Path.home()])]
    infos = [bench_fs.inspect_bench(p) for p in paths]
    checks = doctor.run(infos)
    verdict = overall(checks)
    if args.json:
        out.print_json({"overall": verdict, "benches_source": source, "checks": [c.to_dict() for c in checks]})
    else:
        print("FRAPPE HOSTING MANAGER - HOST DOCTOR (read-only)")
        print(f"Benches: {len(infos)} {source}")
        out.print_checks(checks)
        print(f"\nOverall: {verdict}")
        print("Doctor made no changes to this computer.")
    return EXIT_BLOCKED if verdict == "BLOCKED" else EXIT_OK


def cmd_bench_discover(args, store: Store) -> int:
    roots = [existing_directory(r) for r in (args.root or [str(Path.home())])]
    found = registry.discover(store, roots, args.max_depth)
    if args.json:
        out.print_json({"roots": [str(r) for r in roots], "benches": [asdict(f) for f in found]})
        return EXIT_OK
    if not found:
        print(f"No Bench directories found under {', '.join(map(str, roots))} (max depth {args.max_depth}).")
        return EXIT_OK
    out.print_table(
        [[f.path, f.frappe_version or "?", f.frappe_branch or "?", f.site_count, f.process_mode, "yes" if f.registered else "no"]
         for f in found],
        ["PATH", "FRAPPE", "BRANCH", "SITES", "MODE", "REGISTERED"],
    )
    print("\nDiscovery is read-only. Register a Bench explicitly: frappe-host bench register PATH")
    return EXIT_OK


def cmd_bench_register(args, store: Store) -> int:
    path = registry.resolve_bench_path(args.path)
    if registry.get_bench(store, path):
        if args.json:
            out.print_json({"result": "already-registered", "path": str(path)})
        else:
            print(f"{path} is already registered; nothing to do.")
        return EXIT_OK
    _, plan, warnings = registry.plan_register(path)
    out.print_plan(f"register {path}", plan, warnings)
    if args.dry_run:
        out.info("Dry run: nothing was changed.")
        return EXIT_OK
    confirm_proceed(args)
    result, op_id = registry.register(store, path)
    if args.json:
        out.print_json({"result": result, "path": str(path), "operation_id": op_id, "warnings": warnings})
    else:
        print(f"Registered {path} (operation {op_id}).")
    return EXIT_OK


def cmd_bench_unregister(args, store: Store) -> int:
    path = str(Path(args.path).expanduser().resolve())
    if registry.get_bench(store, Path(path)) is None:
        print(f"{path} is not registered.", file=sys.stderr)
        return EXIT_NOT_FOUND
    plan = [f"remove {path} from the frappe-host registry", "the Bench directory and its sites are not touched"]
    if args.dry_run:
        out.print_plan(f"unregister {path}", plan)
        out.info("Dry run: nothing was changed.")
        return EXIT_OK
    out.print_plan(f"unregister {path}", plan)
    confirm_proceed(args)
    _, op_id = registry.unregister(store, path)
    print(f"Unregistered {path} (operation {op_id}). No files were changed.")
    return EXIT_OK


def cmd_bench_refresh(args, store: Store) -> int:
    path = str(Path(args.path).expanduser().resolve())
    if registry.get_bench(store, Path(path)) is None:
        print(f"{path} is not registered.", file=sys.stderr)
        return EXIT_NOT_FOUND
    registry.refresh(store, path)
    print(f"Snapshot for {path} updated to the Bench's current state.")
    return EXIT_OK


def cmd_bench_list(args, store: Store) -> int:
    benches = registry.list_benches(store)
    drifts = {b["path"]: registry.drift(b) for b in benches}
    if args.json:
        out.print_json([{**b, "drift": asdict(drifts[b["path"]])} for b in benches])
        return EXIT_OK
    if not benches:
        print("No Benches registered. Try: frappe-host bench discover")
        return EXIT_OK
    out.print_table(
        [[b["path"], b["frappe_version"] or "?", b["process_mode"], len((b["snapshot"] or {}).get("sites", [])),
          "DRIFT" if drifts[b["path"]].drifted else "in sync"] for b in benches],
        ["PATH", "FRAPPE", "MODE", "SITES", "STATE"],
    )
    for p, d in drifts.items():
        if d.drifted:
            print(f"\nDrift in {p}:")
            for c in d.changes:
                print(f"  - {c}")
            print(f"  Accept the current state with: frappe-host bench refresh {p}")
    return EXIT_OK


def cmd_site_list(args, store: Store) -> int:
    rows = sites.list_sites(store)
    if args.json:
        out.print_json([asdict(s) for s in rows])
        return EXIT_OK
    if not rows:
        print("No sites in registered Benches.")
        return EXIT_OK
    out.print_table(
        [[s.name, s.bench_path, s.db_type or "?", "yes" if s.maintenance_mode else "no",
          "paused" if s.scheduler_paused else "on", "yes" if s.developer_mode else "no"] for s in rows],
        ["SITE", "BENCH", "DB", "MAINT", "SCHEDULER", "DEV MODE"],
    )
    return EXIT_OK


def cmd_site_status(args, store: Store) -> int:
    bench, site = sites.resolve_site(store, args.site, args.bench)
    checks = sites.status(store, bench, site, probe=not args.no_probe)
    if args.json:
        out.print_json({"site": site, "bench": str(bench), "overall": overall(checks), "checks": [c.to_dict() for c in checks]})
    else:
        out.print_checks(checks)
        print(f"\nOverall: {overall(checks)}")
    return EXIT_OK


def cmd_site_create(args, store: Store) -> int:
    if args.bench:
        bench = Path(args.bench).expanduser().resolve()
    else:
        benches = sites.registered_bench_paths(store)
        if len(benches) != 1:
            raise ValidationError("pass --bench PATH (zero or several Benches are registered)")
        bench = benches[0]
    argv, plan = sites.plan_create(store, bench, args.site, args.install_app or [], args.db_type)
    out.print_plan(f"create site {args.site}", plan)
    if args.dry_run:
        out.info("Dry run: nothing was changed.")
        return EXIT_OK
    confirm_proceed(args)
    op_id = sites.create(store, bench, args.site.lower(), argv, plan)
    print(f"Site {args.site} created (operation {op_id}). Check it with: frappe-host site status {args.site}")
    return EXIT_OK


def cmd_logs(args, store: Store) -> int:
    bench, site = sites.resolve_site(store, args.site, args.bench)
    if args.list or not args.file:
        files = sites.log_files(bench, site)
        if args.json:
            out.print_json([str(f) for f in files])
        else:
            print(f"Log files for {site}:")
            for f in files:
                print(f"  {f.name:<32} {out.human_size(f.stat().st_size):>10}  {f.parent}")
            print("\nShow one with: frappe-host logs SITE --file NAME [--lines N]")
        return EXIT_OK
    lines = sites.tail(bench, site, args.file, args.lines)
    if args.json:
        out.print_json(lines)
    else:
        print("\n".join(lines))
    return EXIT_OK


def _resolve_copy_to(value: str | None) -> Path | None:
    if not value:
        return None
    p = Path(value).expanduser().resolve()
    if p.exists() and not p.is_dir():
        raise ValidationError(f"{p} exists and is not a directory")
    if not p.parent.is_dir():
        raise ValidationError(f"parent directory {p.parent} does not exist")
    return p


def cmd_backup_create(args, store: Store) -> int:
    bench, site = sites.resolve_site(store, args.site, args.bench)
    copy_to = _resolve_copy_to(args.copy_to)
    plan = backups.plan_create(bench, site, copy_to)
    out.print_plan(f"back up {site}", plan.plan, plan.warnings)
    if args.dry_run:
        out.info("Dry run: nothing was changed.")
        return EXIT_OK
    confirm_proceed(args)
    rec = backups.create(store, bench, site, plan, copy_to)
    if args.json:
        out.print_json(rec)
    else:
        print(f"\nBackup {rec['id']} recorded ({out.human_size(rec['total_size'])}, {len(rec['files'])} files).")
        for w in rec["warnings"]:
            print(f"  WARNING: {w}")
        print(f"Verify it with: frappe-host backup verify {rec['id']}")
        print("Remember: only a restore test proves a backup is usable (docs/backup-restore.md).")
    return EXIT_OK


def cmd_backup_list(args, store: Store) -> int:
    site = None
    if args.site:
        bench, site = sites.resolve_site(store, args.site, args.bench)
    recs = backups.list_backups(store, site)
    unmanaged = []
    if site:
        known = {f["name"] for r in recs for f in r["files"]}
        unmanaged = backups.unmanaged_backups(bench, site, known)
    if args.json:
        out.print_json({"backups": recs, "unmanaged_files": unmanaged})
        return EXIT_OK
    if recs:
        out.print_table(
            [[r["id"], r["site"], r["created_at"], out.human_size(r["total_size"]), len(r["files"]),
              "yes" if any(f.get("copy") for f in r["files"]) else "no", r["verify_result"] or "never"] for r in recs],
            ["ID", "SITE", "CREATED", "SIZE", "FILES", "COPIED", "VERIFIED"],
        )
    else:
        print("No backups recorded by frappe-host.")
    if unmanaged:
        print(f"\n{len(unmanaged)} backup file(s) on disk not recorded by frappe-host (e.g. Frappe scheduled backups);"
              " they have no recorded checksum:")
        for n in unmanaged[-10:]:
            print(f"  {n}")
    return EXIT_OK


def cmd_backup_verify(args, store: Store) -> int:
    ok, notes = backups.verify(store, args.backup_id)
    if args.json:
        out.print_json({"id": args.backup_id, "ok": ok, "details": notes})
    else:
        for n in notes:
            print(f"  {n}")
        print(f"\nBackup {args.backup_id}: {'archives verified' if ok else 'VERIFICATION FAILED'}")
        if ok:
            print("Archive integrity is not proof of recoverability; run a restore test into a test site.")
    return EXIT_OK if ok else EXIT_FAILED


def cmd_restore(args, store: Store) -> int:
    bench, site = sites.resolve_site(store, args.site, args.bench)
    rec = backups.get(store, args.backup_id)
    rplan = backups.plan_restore(store, bench, site, rec)
    pre = None if args.no_pre_restore_backup else backups.plan_create(bench, site, None)
    warnings = rplan.warnings + ([] if pre else ["pre-restore backup SKIPPED at your request; the current data cannot be recovered"])
    out.print_plan(f"restore {rec['id']} into {site}", rplan.plan, warnings)
    if args.dry_run:
        out.info("Dry run: nothing was changed.")
        return EXIT_OK
    require_typed_confirmation(site, "restore (this replaces the site's data)", args.confirm)
    result = backups.restore(store, bench, site, rec, rplan, pre)
    print(f"\nRestore finished (operation {result['operation_id']}).")
    if result["pre_restore_backup"]:
        print(f"Pre-restore backup: {result['pre_restore_backup']}")
    print(f"Check the site now: frappe-host site status {site}")
    return EXIT_OK


def cmd_status(args, store: Store) -> int:
    benches = registry.list_benches(store)
    data = []
    for b in benches:
        d = registry.drift(b)
        p = Path(b["path"])
        site_rows = []
        if d.exists:
            for s in bench_fs.list_sites(p):
                last = store.db.execute("SELECT created_at, verify_result FROM backups WHERE site=? AND bench_path=? "
                                        "ORDER BY created_at DESC LIMIT 1", (s, b["path"])).fetchone()
                site_rows.append({"site": s, "last_managed_backup": last[0] if last else None,
                                  "verified": last[1] if last else None})
        data.append({"bench": b["path"], "frappe": b["frappe_version"], "mode": b["process_mode"],
                     "drift": d.changes, "sites": site_rows})
    recent = operations.history(store, 5)
    if args.json:
        out.print_json({"benches": data, "recent_operations": recent})
        return EXIT_OK
    if not data:
        print("Nothing registered yet. Start with: frappe-host doctor, then frappe-host bench discover")
    for b in data:
        print(f"{b['bench']}  Frappe {b['frappe'] or '?'}  mode={b['mode']}  {'DRIFT' if b['drift'] else 'in sync'}")
        no_backup = [s["site"] for s in b["sites"] if not s["last_managed_backup"]]
        print(f"  {len(b['sites'])} sites; {len(b['sites']) - len(no_backup)} with a frappe-host backup")
    if recent:
        print("\nRecent operations:")
        for o in recent:
            print(f"  {o['started_at']}  {o['status']:<11} {o['kind']:<18} {o['target'] or ''}")
    print("\nFor per-site health: frappe-host site status SITE. Public reachability: not verified.")
    return EXIT_OK


def cmd_history(args, store: Store) -> int:
    rows = operations.history(store, args.limit)
    if args.json:
        out.print_json(rows)
        return EXIT_OK
    out.print_table([[r["id"], r["started_at"], r["status"], r["kind"], r["target"] or "", r["error"] or ""] for r in rows],
                    ["OPERATION", "STARTED", "STATUS", "KIND", "TARGET", "ERROR"])
    if not rows:
        print("No operations recorded.")
    return EXIT_OK


def cmd_state(args, store: Store) -> int:
    if args.state_cmd == "path":
        print(store.home)
    else:
        out.print_json(store.export())
    return EXIT_OK


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    # Subcommands use SUPPRESS so `frappe-host --json bench list` is not reset by the subparser default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")

    p = argparse.ArgumentParser(prog="frappe-host", description="Manage Frappe Benches and sites on your own Linux computer.")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--version", action="version", version=f"frappe-host {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def add(parent, name: str, func: Callable, help: str, **kw):
        sp = parent.add_parser(name, help=help, parents=[common], **kw)
        sp.set_defaults(func=func)
        return sp

    def mutating(sp):
        sp.add_argument("--dry-run", action="store_true", help="show the plan without changing anything")
        sp.add_argument("--yes", "-y", action="store_true", help="approve the plan without prompting (non-destructive operations only)")

    add(sub, "doctor", cmd_doctor, "read-only host readiness report")
    add(sub, "status", cmd_status, "summary of managed Benches, sites and recent operations")
    sp = add(sub, "history", cmd_history, "operation audit log")
    sp.add_argument("--limit", type=int, default=20)

    bench = sub.add_parser("bench", help="discover and register Bench environments").add_subparsers(dest="bench_cmd", metavar="ACTION", required=True)
    sp = add(bench, "discover", cmd_bench_discover, "read-only scan for Bench directories")
    sp.add_argument("--root", action="append", help="directory to scan (repeatable; default: home)")
    sp.add_argument("--max-depth", type=int, default=registry.DEFAULT_MAX_DEPTH)
    add(bench, "list", cmd_bench_list, "list registered Benches and report drift")
    sp = add(bench, "register", cmd_bench_register, "register an existing Bench (metadata only)")
    sp.add_argument("path")
    mutating(sp)
    sp = add(bench, "unregister", cmd_bench_unregister, "forget a Bench (files untouched)")
    sp.add_argument("path")
    mutating(sp)
    sp = add(bench, "refresh", cmd_bench_refresh, "accept a Bench's current state as its registry snapshot")
    sp.add_argument("path")

    site = sub.add_parser("site", help="site inventory and lifecycle").add_subparsers(dest="site_cmd", metavar="ACTION", required=True)
    add(site, "list", cmd_site_list, "list sites across registered Benches")
    sp = add(site, "status", cmd_site_status, "health of one site")
    sp.add_argument("site")
    sp.add_argument("--bench")
    sp.add_argument("--no-probe", action="store_true", help="skip the local HTTP probe")
    sp = add(site, "create", cmd_site_create, "create a site through bench new-site")
    sp.add_argument("site")
    sp.add_argument("--bench")
    sp.add_argument("--install-app", action="append", metavar="APP")
    sp.add_argument("--db-type", default="mariadb", choices=["mariadb", "postgres"])
    mutating(sp)

    sp = add(sub, "logs", cmd_logs, "list or show a site's logs (redacted)")
    sp.add_argument("site")
    sp.add_argument("--bench")
    sp.add_argument("--list", action="store_true")
    sp.add_argument("--file", help="log file name, e.g. frappe.log")
    sp.add_argument("--lines", type=int, default=100)

    bk = sub.add_parser("backup", help="create, list and verify backups (shorthand: backup SITE)").add_subparsers(dest="backup_cmd", metavar="ACTION", required=True)
    sp = add(bk, "create", cmd_backup_create, "back up a site's database and files via bench")
    sp.add_argument("site")
    sp.add_argument("--bench")
    sp.add_argument("--copy-to", metavar="DIR", help="also copy the backup to this directory (ideally another disk)")
    mutating(sp)
    sp = add(bk, "list", cmd_backup_list, "backup inventory")
    sp.add_argument("site", nargs="?")
    sp.add_argument("--bench")
    sp = add(bk, "verify", cmd_backup_verify, "check checksums and archive integrity")
    sp.add_argument("backup_id")

    sp = add(sub, "restore", cmd_restore, "restore a recorded backup into a site (destructive)")
    sp.add_argument("site")
    sp.add_argument("backup_id")
    sp.add_argument("--bench")
    sp.add_argument("--confirm", metavar="SITE", help="non-interactive confirmation: must equal the site name")
    sp.add_argument("--no-pre-restore-backup", action="store_true", help="skip the safety backup (not recommended)")
    sp.add_argument("--dry-run", action="store_true")

    st = sub.add_parser("state", help="inspect the local registry").add_subparsers(dest="state_cmd", metavar="ACTION", required=True)
    add(st, "path", cmd_state, "print the state directory")
    add(st, "export", cmd_state, "export registry contents as JSON")
    return p


def _expand_shorthand(argv: list[str]) -> list[str]:
    """`frappe-host backup SITE` == `frappe-host backup create SITE`."""
    for i, a in enumerate(argv):
        if a == "backup":
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if nxt and not nxt.startswith("-") and nxt not in ("create", "list", "verify"):
                return argv[: i + 1] + ["create"] + argv[i + 1 :]
            break
        if not a.startswith("-"):
            break
    return argv


def main(argv: list[str] | None = None) -> int:
    argv = _expand_shorthand(list(sys.argv[1:] if argv is None else argv))
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_INVALID
    out.info_stream = sys.stderr if args.json else None
    process.interactive_stdout = 2 if args.json else None  # fd 2: child output to stderr
    store = Store()
    try:
        return args.func(args, store)
    except ValidationError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_INVALID
    except sites.SiteNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except (Refused, ConfirmationRefused) as e:
        print(f"refused: {e}", file=sys.stderr)
        return EXIT_REFUSED
    except OperationFailed as e:
        print(f"failed: {e}", file=sys.stderr)
        if e.op_id:
            print(f"Operation log: {store.ops_dir / (e.op_id + '.log')}", file=sys.stderr)
        return EXIT_FAILED
    except StateError as e:
        print(f"state error: {e}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        store.close()
