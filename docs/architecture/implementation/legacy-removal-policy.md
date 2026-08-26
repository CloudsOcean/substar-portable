# Legacy removal policy

The refactored application is a deliberate breaking version. It supports only
projects, settings, credentials, routes and artifacts produced by the current
canonical contracts.

## Production rule

- Dead historical implementations are deleted; Git is their archive.
- Historical code is never moved under an importable `archive` package.
- Offline research scripts may remain under `scripts/` only when no production
  module imports them.
- Current-schema upgrades may migrate their own SQLite schema, but the product
  does not discover or convert pre-refactor project layouts.
- Unsupported folders are left untouched on disk and excluded from discovery.

## Removed in the breaking-version cleanup

- the manual relay runtime;
- experiment-era production profiles;
- old project-directory and display-name migrations;
- legacy project-ID directory scanning;
- individual credential-file and credential-role migration;
- the former media creation write route;
- the `/relay` page alias.

## Release guard

Release acceptance searches the production import graph and public route table
for removed modules, routes and experiment labels. Current-schema projects must
still pass creation, editing, AI-task, export and restart smoke tests.
