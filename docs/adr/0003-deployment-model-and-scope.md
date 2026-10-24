# ADR 0003: MVP manages existing Bench installs; Supervisor + NGINX is the Phase 5 production target

Status: accepted (2026-10-24). The design document leaves this open (§20.2, §20.3, §20.7).

## Decision

1. **The MVP manages existing Benches and does not install OS packages.** `init` (dependency installation) is deferred. When it is added, it must start as plan-only output, and branch-specific requirements come from `domain/support.py`.
2. **Production service model:** the target is the path Bench itself provides, `bench setup production`, which generates Supervisor and NGINX configuration. It is the documented non-containerised path, and Bench already maintains its configuration templates. Systemd and container adapters are out of MVP scope.
3. **Container caveat:** Frappe's current production guide favours container images. This project targets Bench installs on purpose, for users who want Bench on their own machine. It must not describe bare-metal as Frappe's recommended production path.
4. **Public publishing is not in the MVP.** It comes in Phase 6, after the security foundations.
5. **Secrets:** commands that need database root or Administrator passwords run Bench with the terminal attached, so Bench prompts directly. frappe-host never receives them.

## Consequences

- `doctor` reports development-mode Benches as `INFO`, not as errors.
- Process-mode detection already distinguishes development, Supervisor and systemd, so Phase 5 can refuse unsupported modes explicitly.
