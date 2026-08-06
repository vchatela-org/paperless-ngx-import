# Security Policy

## Supported versions

Only the latest `main` and the most recent tagged release receive fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately via GitHub's [private vulnerability reporting](https://github.com/vchatela-org/paperless-ngx-import/security/advisories/new).

Please include the affected version or commit, reproduction steps, and the impact you believe it has. Expect an acknowledgement within 7 days and an assessment within 30 days.

## Scope

This repository contains the import job only. Vulnerabilities in
[Paperless-NGX](https://github.com/paperless-ngx/paperless-ngx) itself should be
reported to that project.

## Handling of secrets

`PAPERLESS_API_TOKEN` is read from the environment and is never written to disk
by this project. It is redacted from log output, and the job refuses to send it
over a plaintext `http://` connection unless `PAPERLESS_ALLOW_INSECURE_HTTP` is
explicitly set.

If you believe a secret has been committed here, report it privately using the
link above rather than opening an issue.

## Automated security tooling

- **CodeQL** code scanning (`python` and `actions`, `security-extended` queries) on every push and PR, plus a weekly scan
- **Secret scanning** with **push protection**
- **Dependabot** alerts, security updates, and weekly version updates for pip, Docker and GitHub Actions
- Dependencies are hash-pinned (`pip --require-hashes`); the base image is pinned by digest
