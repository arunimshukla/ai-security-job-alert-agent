# Security Policy

## Reporting a vulnerability

Please report security problems privately through this repository's **Security → Report a vulnerability** form when private vulnerability reporting is enabled.

Do not open a public issue containing tokens, passwords, email credentials, private application material or other sensitive information. If a credential is exposed, revoke or rotate it immediately before reporting the incident.

## Supported version

Security fixes are applied to the latest version on the default branch.

## Credential model

This project expects runtime credentials to be stored in GitHub Actions Secrets and Netlify environment variables. Secret values must never be committed to the repository.
