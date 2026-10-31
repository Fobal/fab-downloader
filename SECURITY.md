# Security

Fab Downloader stores an Epic Games session on your computer (see
[Usage](README.md#usage)), so security problems matter even in a small tool.

## Reporting a vulnerability

Report it privately through
[GitHub's private vulnerability reporting](https://github.com/Fobal/fab-downloader/security/advisories/new),
not in a public issue. Include the Fab Downloader version, the steps to reproduce it and what an
attacker could do with it. You should get a reply within a week.

Only the latest release gets fixes.

## Keeping your account safe

- Never share your `authorizationCode`, the copied login page or `session.bin` (in
  `%APPDATA%\fab-downloader` on Windows, `~/Library/Application Support/fab-downloader` on
  macOS, `~/.config/fab-downloader` on Linux), including in issues. On macOS and Linux the file
  is not encrypted, so keep it out of backups others can read.
- Download the executables only from this repository's Releases page. Releases after 1.2.0 can
  be verified: `gh attestation verify <file> -R Fobal/fab-downloader` (with the
  [GitHub CLI](https://cli.github.com/)) checks that the file was built by this repository's
  release workflow, and `SHA256SUMS` lists the expected SHA-256 of each executable.
- If you think your session leaked, run `fabdl logout` and change your Epic password, which
  ends your other sessions.
