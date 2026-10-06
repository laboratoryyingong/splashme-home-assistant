# Recommended GitHub Labels

These labels are recommended for the SplashMe Home Assistant integration repository.

| Label | Suggested colour | Purpose |
|---|---|---|
| `bug` | `d73a4a` | Confirmed or suspected software defect |
| `enhancement` | `a2eeef` | New feature or improvement request |
| `ux` | `f9d0c4` | Dashboard, naming, entity organisation, or usability issue |
| `compatibility` | `fbca04` | Home Assistant, firmware, device, or platform compatibility issue |
| `documentation` | `0075ca` | Documentation or setup guide issue |
| `needs-info` | `d4c5f9` | More information is required from the reporter |
| `cannot-reproduce` | `cfd3d7` | Issue cannot currently be reproduced |
| `duplicate` | `cfd3d7` | Duplicate of an existing issue |
| `beta` | `bfdadc` | Report relates to beta testing |
| `priority-high` | `b60205` | High-priority issue affecting major functionality |
| `priority-medium` | `fbca04` | Normal-priority issue |
| `priority-low` | `0e8a16` | Minor issue or low-impact improvement |

## Suggested triage rules

- Use `bug` for broken behaviour.
- Use `enhancement` for new functionality.
- Add `ux` when the issue mainly concerns naming, dashboards, layout, entity organisation, or usability.
- Add `compatibility` when a problem depends on a Home Assistant version, SplashMe firmware version, installation type, or controller platform.
- Add `needs-info` when logs, diagnostics, firmware version, or reproduction steps are missing.
- Add `beta` to reports received during the beta-testing period.

## Recommended reporting rule

Use **one issue per independent problem**.

For example, if a tester finds:
1. an entity naming problem,
2. a pump command failure, and
3. a dashboard usability suggestion,

these should normally be submitted as three separate GitHub issues.

## Issue template notes

- `blank_issues_enabled: false` forces users to choose one of the provided templates.
- The Support link in `config.yml` currently points to `https://www.splashmepool.com.au/`.
- You can replace that URL with a dedicated SplashMe support page or email/contact page later.
- GitHub labels referenced by the templates (`bug` and `enhancement`) should exist in the repository.
