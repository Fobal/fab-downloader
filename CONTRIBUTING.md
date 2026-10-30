# Contributing

Bug reports, fixes and ideas are welcome.

## Issues

- Search the existing issues first.
- For a bug, use the bug report form: the Fab Downloader version, how you run it, and the error.
- Never post your `authorizationCode`, the copied login page or `session.bin`.
- Report security problems privately, as described in [SECURITY.md](SECURITY.md).

## Pull requests

1. For anything larger than a small fix, open an issue first so we can agree on the approach.
2. Keep Fab Downloader dependency-free: only the Python standard library, Python 3.11+.
3. Add or update tests in `tests/`. They use fakes and must not reach Epic or Fab.
4. Run the checks before you push:

   ```
   py -3 -m pip install pyflakes
   py -3 -m pyflakes fabdl tests packaging fab-downloader.pyw
   py -3 -m unittest discover -s tests -t .
   ```

5. Update the README if the behaviour or the commands change.

By contributing, you agree that your contribution is licensed under the [GNU General Public License v3.0](LICENSE).
