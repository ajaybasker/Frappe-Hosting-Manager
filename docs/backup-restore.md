# Backup and restore

## What a backup contains

`frappe-host backup SITE` runs Frappe's own mechanism, `bench --site SITE backup --with-files`, which produces:

| File | Kind |
|---|---|
| `<ts>-<site>-database.sql.gz` | database dump |
| `<ts>-<site>-files.tar` | public files |
| `<ts>-<site>-private-files.tar` | private files |
| `<ts>-<site>-site_config_backup.json` | site config, **including db password and encryption_key** |

frappe-host then:

1. Identifies the files that this run created (a before/after comparison of `sites/SITE/private/backups/`).
2. Fails the operation if no database dump was produced. It warns if a files archive is missing.
3. Sets each new file to mode 0600.
4. Records size and SHA-256 for each file, plus the Frappe version, in the registry.
5. With `--copy-to DIR`, copies each file atomically and re-checks its SHA-256 after copying. It refuses to overwrite existing files.

## Verification

`frappe-host backup verify ID` checks each file and each copy:

- that the file exists and its size and SHA-256 match the record;
- the `.sql.gz` decompresses completely (CRC checked) and looks like an SQL dump;
- each tar archive is readable end to end, with no absolute paths, `..` members, or links pointing outside the archive;
- the site config is valid JSON.

**A verified archive is not proof that recovery will work. Only a restore test is.**

## Restore

```bash
frappe-host restore SITE BACKUP_ID --dry-run      # show the plan
frappe-host restore SITE BACKUP_ID                # asks you to type SITE
```

Safeguards, in order:

1. The target site must exist in a registered Bench. If the site name exists in several Benches, `--bench` is required.
2. Restoring a backup from a **newer** Frappe major version is refused. An older version produces a warning to run `bench --site SITE migrate`.
3. Restoring into a different site than the source produces an `encryption_key` warning.
4. You must type the exact site name, or pass `--confirm SITE` for automation.
5. The backup is re-verified. If an original file is damaged, a verified `--copy-to` copy is used instead.
6. A **pre-restore backup** of the target is taken. Skipping it needs `--no-pre-restore-backup`, and the plan then warns that current data cannot be recovered.
7. `bench restore` runs with your terminal attached and prompts for the database root password.
8. If the restore fails, the error names the pre-restore backup to roll back to.

## Restore drill (recommended monthly)

```bash
bench new-site restore-test.local          # a throwaway target
frappe-host bench refresh ~/frappe-bench
frappe-host restore restore-test.local BACKUP_ID
frappe-host site status restore-test.local
```

Then log in, open core pages and attachments, and confirm background jobs run. Record how long the drill took as your recovery-time estimate. Drop the test site afterwards with `bench drop-site restore-test.local`.

## Same-disk risk

Backups in `sites/SITE/private/backups/` share a disk with the live site, so a disk failure, theft or accidental `rm` loses both. Every backup plan warns about this unless `--copy-to` points at a different filesystem. Use a separate disk, or move copies off the device.

## Frappe's scheduled backups

If Frappe's own scheduler creates backups, `backup list SITE` shows those files as **unmanaged**. `frappe-host backup import SITE` records each complete set (sets without a database dump are skipped) with checksums taken at import time, after which they can be verified and restored like any other backup.

## Retention

`frappe-host backup prune SITE --keep N` deletes all but the newest N frappe-host backups of the site, including their `--copy-to` copies. It shows the list first and needs the site name typed (or `--confirm SITE`). Files that frappe-host didn't record are never deleted.

## Scheduled backups

`frappe-host backup schedule SITE --at HH:MM [--copy-to DIR] [--keep N]` adds a daily entry to **your** crontab, inside a marked block. Other lines are preserved. The entry runs `backup create` and then, if `--keep` is given, `backup prune --keep N --confirm SITE`. Output is appended to `~/.local/share/frappe-host/scheduled-backups.log`. Use `backup schedules` to list entries and `backup unschedule SITE` to remove one. `frappe-host check` fails when a site's latest frappe-host backup is older than 26 hours.

## Not implemented

Encryption, cloud destinations, and automated restore drills.
