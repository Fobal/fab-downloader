# Fab Downloader

Get the Unreal Engine items in your Fab library without the Epic Games Launcher: add them to a
project, install code plugins into an engine (including source-built engines, which the
launcher cannot install into), or download a build for any engine version. Runs on Windows and
Linux, with a window and a command line.

![The Fab Downloader window](https://raw.githubusercontent.com/Fobal/fab-downloader/main/docs/screenshot.png)

> Fab Downloader is not affiliated with, endorsed by or supported by Epic Games. See
> [Disclaimer](#disclaimer).

## Install

- **Executable** (window only, no Python needed): download it from
  [Releases](https://github.com/Fobal/fab-downloader/releases).
  - Windows: `fab-downloader.exe`. It is not code-signed, so SmartScreen or antivirus software
    may warn about it; if so, use pipx or the source.
  - Linux (x86-64, glibc 2.35 or newer, such as Ubuntu 22.04 or Debian 12):
    `fab-downloader-linux-x86_64`. Make it executable with `chmod +x` first.
- **pipx** (Python 3.11+): `pipx install fab-downloader`, then run `fab-downloader` for the
  window or `fabdl` for the command line. On Linux the window needs Tkinter, which some
  distributions package separately (`python3-tk` on Debian and Ubuntu).
- **Source** (Python 3.11+, standard library only): clone the repo and run
  `fab-downloader.pyw`, or `py -3 -m fabdl` (`python3 -m fabdl` on Linux) for the command line.

## Usage

1. Click **Open Epic login** and sign in. The page then shows JSON text: copy the whole page
   (Ctrl+A, Ctrl+C) and the window signs in, or paste just the `authorizationCode` value.
2. Search your library, select an item and pick an engine version.
3. Under **Save to**, choose a destination:
   - **Folder** (recommended): **Download** saves the build to `<folder>\<title> <engine>`.
   - **Project:** pick a `.uproject`. **Add to project** puts a code plugin in `Plugins` or
     merges an asset pack into `Content`.
   - **Engine:** pick a detected engine, or browse to one. **Install to engine** puts a code
     plugin in `Engine\Plugins\Marketplace`.

Anything that gets replaced is backed up to the project's or engine's `Saved` folder first, and
nothing changes if the Unreal Editor has the files open. The `.uproject` is never edited; if a
plugin needs enabling, you are told to do it in **Edit > Plugins**.

For a project or engine, the **Installed** column lists the plugins Fab Downloader put there.
**Check for updates** looks for newer builds; install an item again to update it.

The window and the command line share one session. On Windows it is saved in
`%APPDATA%\fab-downloader\session.bin` and encrypted for your Windows user with DPAPI. On Linux
it is saved in `~/.config/fab-downloader/session.bin`, readable only by your user but not
encrypted. **Sign out**, or `fabdl logout`, deletes it.

Engine detection, backups and downloads are covered in more detail in
[How it works](https://github.com/Fobal/fab-downloader/blob/main/docs/how-it-works.md).

## Command line

```
fabdl list [search]
fabdl install [search] [--project GAME.uproject | --engine-dir ENGINE_ROOT] [--engine 5.3]
fabdl outdated [--project GAME.uproject | --engine-dir ENGINE_ROOT]
fabdl download [search] [--engine 5.3] [--out FOLDER]
fabdl logout
```

`search` is an asset id or part of a title; if several items or engine versions match, it lists
them and asks. Without `--project` or `--engine-dir`, `install` and `outdated` use the project
last chosen in the window, and `install` picks the target's engine version unless `--engine` is
given. `download --out` must not exist yet. `--platform` (default `Windows`) sets the build
platform.

To sign in, the first command that needs a session opens the Epic login page and asks for the
`authorizationCode` value.

## Limits

- Fab has no public API. Fab Downloader uses the launcher's private endpoints, which Epic can
  change without notice and does not officially support.
- Encrypted manifests and chunks are not supported.
- Downloads cannot be paused or resumed; closing the window is the only way to cancel one.
- Builds are requested for Windows unless `--platform` says otherwise, so a code plugin's
  prebuilt binaries are Windows ones. On Linux, Unreal has to compile the plugin from its
  source, which source-built engines and C++ projects do.

## Contributing

Bug reports and pull requests are welcome; see
[CONTRIBUTING.md](https://github.com/Fobal/fab-downloader/blob/main/CONTRIBUTING.md). Report
security problems privately as described in
[SECURITY.md](https://github.com/Fobal/fab-downloader/blob/main/SECURITY.md).

## Sponsor

Fab Downloader is free. If it saves you time, you can support it through:

- [GitHub Sponsors](https://github.com/sponsors/Fobal)
- Bitcoin: `bc1qhj0n2mlyjr43zg7ygyu9y9k9hfqzleaxkvjprt`
- Ethereum, Polygon and other EVM networks: `0x4C6C4c665113C6311501567c0BFC38029b00a82e`

Starring the repo and sharing it with other Unreal developers are also greatly appreciated.

## Disclaimer

Fab Downloader is an independent project, not affiliated with, endorsed by or supported by
Epic Games, Inc. Epic Games, Fab and Unreal Engine are trademarks or registered trademarks of
Epic Games, Inc. in the United States and elsewhere.

It only downloads items in the signed-in account's own Fab library, through the same endpoints
the Epic Games Launcher uses. You are responsible for following Epic's terms and the license of
each item you download.

## License

Copyright (C) 2026 Fobal. Licensed under the
[GNU General Public License v3.0](https://github.com/Fobal/fab-downloader/blob/main/LICENSE).
