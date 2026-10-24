# Security policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Email the maintainers at the address listed in the repository profile (to be finalised before the first public release), and include the affected version, steps to reproduce, and the impact. Expect an acknowledgement within 7 days.

## Scope

In scope: command injection, path traversal, secret leakage (logs, state, JSON output, argv), privilege boundary violations, unsafe file permissions, false "verified" or "public" reports, and any way frappe-host could modify a Bench or system configuration without showing a plan and getting consent.

Out of scope: vulnerabilities in Frappe, Bench, MariaDB, Redis or NGINX themselves. Report those upstream.

## Supported versions

Only the latest release receives security fixes during the 0.x series.

## Release signing

Signed releases are planned before the first public release (design document §20.9).

See [docs/security.md](docs/security.md) for the security model and controls.
