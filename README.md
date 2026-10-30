# Fab Downloader

Fab Downloader is a downloader for Fab, Epic's marketplace. Without the Epic Games Launcher,
it can:

- add Fab items to an Unreal project: code plugins go into `Plugins`, asset packs into `Content`;
- install code plugins into an engine, like the launcher's **Install to Engine**;
- download a chosen engine version of any Unreal Engine item you own on Fab.

It helps with source-built engines, which the launcher cannot install plugins into. Windows only.

> Fab Downloader is not affiliated with, endorsed by or supported by Epic Games. See
> [Disclaimer](#disclaimer).

## Install

- **No Python needed:** download `fab-downloader.exe` from the
  [Releases](https://github.com/Fobal/fab-downloader/releases) page and run it.
- **From source:** clone the repo and run `fab-downloader.pyw` for the window, or
  `py -3 -m fabdl` in the repo folder for the command line. Only the standard library is used.
- **With Python 3.11+:** `pipx install fab-downloader` adds the `fab-downloader` command for
  the window and the `fabdl` command line.

The executable is built by GitHub Actions from the tagged source with PyInstaller and is
not code-signed, so Windows SmartScreen may warn on first run, and some antivirus products flag
PyInstaller one-file executables as false positives. If that happens, use pipx or the source.

## Window

1. Sign in once (see [Signing in](#signing-in)).
2. Search the library, select an item and pick the engine version.
3. Under **Save to**, choose where it goes:
   - **Folder** (the default): **Download** saves the build in `<folder>\<title> <engine>`.
   - **Project**: pick your `.uproject` with **...**. **Add to project** installs a code
     plugin or merges an asset pack (see [Installing](#installing)). The engine version
     starts on the project's engine.
   - **Engine**: pick one of the engines found on this computer, or add any engine folder with
     **...** (its root, or the `Engine` folder inside it). **Install to engine** puts a code
     plugin into that engine.

   For Project and Engine, the **Installed** column shows plugins Fab Downloader installed
   there, and **Check for updates** asks Fab for each one's current build. To update a plugin,
   select it and install it again.

The window remembers the destination, folder, project, engine and engine version.

## Command line

The command line comes with pipx (`fabdl`) or the source (`py -3 -m fabdl` in the repo folder);
it is not released as an executable.

```
fabdl list [search]
fabdl install [search] [--project GAME.uproject | --engine-dir ENGINE_ROOT] [--engine 5.3]
fabdl outdated [--project GAME.uproject | --engine-dir ENGINE_ROOT]
fabdl download [search] [--engine 5.3] [--out FOLDER]
fabdl logout
```

- `search` is an asset id or part of a title. If several items match, or `--engine` is
  missing and the item has several versions, the tool lists them and asks.
- `install` adds the item to the project, or with `--engine-dir` (an engine's root, or the
  `Engine` folder inside it) installs the plugin into that engine. Without either, it uses the project chosen last in the window. It installs the
  target's engine version unless `--engine` is given.
- `download --out` must be a folder that does not exist yet. The default is
  `<title> <engine>` in the current folder.
- `--platform` (default `Windows`) picks the build platform for `install` and `download`.

## Installing

- **Engine detection** follows Unreal: an engine association like `5.3` is looked up in the
  launcher's install list (`%ProgramData%\Epic\UnrealEngineLauncher\LauncherInstalled.dat`),
  any other value in the engines registered under
  `HKCU\Software\Epic Games\Unreal Engine\Builds`, and an empty one in the folders around
  the project. The Engine list in the window comes from the same two places, plus engine
  folders you add with **...**.
- **Downloads** are assembled in the project's `Saved\fabdl` (or the engine's
  `Engine\Intermediate\fabdl`), on the same drive, so installing is a quick move.

### Code plugins

- **Where:** a project's `Plugins\<PluginName>`, or an engine's
  `Engine\Plugins\Marketplace\<PluginName>`. If the plugin is already there, it is replaced
  where it is. Fab Downloader never replaces an engine's own built-in plugins.
- **Replacing:** the old copy is moved to `Saved\PluginBackups\<folder>-<date>` (in the project
  or the engine). If it cannot be moved, usually because the Unreal Editor has its DLLs open,
  nothing changes.
- **Source-built engines:** the plugin's prebuilt `Binaries` and `Intermediate` folders target
  the launcher build, so they are removed and the build compiles the plugin. Engines with
  `Engine\Build\InstalledBuild.txt` (launcher and installed builds) keep them.
- **Enabled or not:** Fab Downloader reports when the plugin will not load until enabled: in a
  project, because its `.uplugin` sets `EnabledByDefault` to false or the `.uproject` disables
  it; in an engine, unless it sets `EnabledByDefault` to true. It never edits the `.uproject`;
  enable the plugin in **Edit > Plugins**.
- **Record:** `.fabdl.json` in the plugin folder stores the Fab item, engine version and build.
  Update checks compare it with Fab's current build, one request per installed plugin.

### Asset packs

**Add to project** merges the pack's `Content` folder into the project's `Content`, so the
assets keep their `/Game/...` paths, like the launcher's **Add to Project**. Files the project
already has are moved to `Saved\ContentBackups\<date>` first. If a file cannot be moved,
usually because the Unreal Editor has it open, every change is undone. Asset packs cannot be
installed into an engine, and complete sample projects should be downloaded instead.

## Signing in

Signing in uses Epic's login page in your browser. After you sign in, the page shows JSON text.
In the window, just copy that whole page (Ctrl+A, Ctrl+C): Fab Downloader notices the copied
page and signs in. You can also paste the `authorizationCode` value into the box, or into the
prompt on the command line. Fab Downloader only reacts to a copied page that contains
`authorizationCode`, and stops watching the clipboard after signing in or after 10 minutes.

The window and the command line share the session. It is saved in
`%APPDATA%\fab-downloader\session.bin`, encrypted for your Windows user with DPAPI, and
refreshed when it expires. **Sign out** in the window, or `fabdl logout`, ends the session and
deletes the file. Window choices are kept in `%APPDATA%\fab-downloader\settings.json`.

## How it works

1. **Login:** the Epic Games Launcher's public OAuth client, with the authorization-code
   flow.
2. **Library:** `GET https://www.fab.com/e/accounts/{account}/ue/library`. Each item lists
   `projectVersions`, and each entry pairs an `artifactId` with its `engineVersions`.
3. **Build:** `POST https://www.fab.com/e/artifacts/{artifactId}/manifest` returns the
   current build version and signed manifest URLs for several CDNs.
4. **Files:** the build manifest (binary or JSON) lists each file as parts of compressed
   chunks. The tool downloads every chunk once, falling back to the other CDNs, and rebuilds
   the files. Each file must pass the SHA-1 check from the manifest. Files are written to a
   staging folder and moved into place only when all of them pass.

## Limits

- Fab has no public API. These are the launcher's private endpoints, so Epic can change
  them without notice, and using them is not something Epic officially supports.
- Encrypted manifests and chunks are rejected.
- Downloads cannot be cancelled except by closing the window, and do not resume.

## Development

```
py -3 -m unittest discover -s tests -t .
```

Pushing a tag `vX.Y.Z` that matches `fabdl.__version__` runs `.github/workflows/release.yml`:
it runs the tests, builds `fab-downloader.exe` and the Python package, and attaches them to a
GitHub Release. Publishing to PyPI is an optional job; it needs a PyPI trusted publisher, a
`pypi` environment and the repository variable `PUBLISH_TO_PYPI` set to `true`.

Contributions are welcome; see
[CONTRIBUTING.md](https://github.com/Fobal/fab-downloader/blob/main/CONTRIBUTING.md). Report
security problems privately as described in
[SECURITY.md](https://github.com/Fobal/fab-downloader/blob/main/SECURITY.md).

## Support

Fab Downloader is free and open source. If it saves you time, you can support the developer:

- [GitHub Sponsors](https://github.com/sponsors/Fobal)
- EVM network (Ethereum, Polygon, etc): `0x4C6C4c665113C6311501567c0BFC38029b00a82e`
- Bitcoin: `bc1qhj0n2mlyjr43zg7ygyu9y9k9hfqzleaxkvjprt`
- Solana: `C1WMmKNztix6UWxpSpp6CueJDZW9ikbEnQ7peFCRdV2D`
- Tron: `TApEv8Syq6VxX67s57W52pLapr1FtDznnu`

Starring the repository, reporting bugs and sharing Fab Downloader with other Unreal developers
help too.

## Disclaimer

Fab Downloader is an independent project. It is not affiliated with, endorsed by or supported
by Epic Games, Inc. Epic Games, Fab and Unreal Engine are trademarks or registered trademarks
of Epic Games, Inc. in the United States and elsewhere.

Fab Downloader only downloads items in the signed-in account's own Fab library, through the
same endpoints the Epic Games Launcher uses. You are responsible for following Epic's terms and
the license of each item you download.

## License

Copyright (C) 2026 Fobal

Fab Downloader is licensed under the
[GNU General Public License v3.0](https://github.com/Fobal/fab-downloader/blob/main/LICENSE). You
may use, share and modify it; if you distribute it or a modified version, you must do so under
the same license, with the source code.
