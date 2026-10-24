# Recovery runbooks

Each runbook assumes you run commands as the Bench owner, from a terminal on the host.

## R1. A restore failed part-way

1. Don't run more restores yet. Read the operation log path printed with the error.
2. `frappe-host history`: find the `site.restore` operation, and the `backup.create` operation just before it (the pre-restore backup).
3. `frappe-host backup verify <PRE_RESTORE_ID>`.
4. `frappe-host restore SITE <PRE_RESTORE_ID>`. This takes another pre-restore backup of the broken state first, which is harmless.
5. `frappe-host site status SITE`, then log in and check the site.

## R2. The site is broken after a migration or update

1. `bench --site SITE set-maintenance-mode on`
2. Find the most recent good backup: `frappe-host backup list SITE`.
3. If the Bench's apps were updated, the backup may be older than the code. Restore it, then run `bench --site SITE migrate`. Restoring a backup from a *newer* major version is refused by design.
4. `bench --site SITE set-maintenance-mode off`

## R3. The disk is full

1. `frappe-host doctor` shows free space per filesystem.
2. Check old backups (`sites/*/private/backups`), `logs/`, and unused Benches first.
3. Copy needed backups to another disk before deleting anything: `backup create --copy-to`, or copy manually.
4. MariaDB may need a restart once space is freed: `sudo systemctl restart mariadb`.

## R4. The registry is lost or corrupted

The registry holds no data you can't rebuild, apart from operation history and backup checksums.

1. `mv ~/.local/share/frappe-host ~/.local/share/frappe-host.broken`
2. `frappe-host bench discover`, then `bench register` each Bench again.
3. Backup files are still on disk. They show as "unmanaged" in `backup list` and can be restored with `bench restore` directly.
4. If a migration failed, `state.db.pre-vN.bak` is the database before that migration.

## R5. The host disk failed

This is only recoverable if you used `--copy-to` on a different disk, or copied backups off the device.

1. Set up a new host and Bench with the same Frappe major version and apps (see `site_config_backup.json` and your app list).
2. `bench new-site SITE`, then `bench --site SITE restore <db.sql.gz> --with-public-files ... --with-private-files ...` from the copies.
3. Restore `encryption_key` from `site_config_backup.json` into the new `site_config.json`. Without it, stored passwords cannot be decrypted.
