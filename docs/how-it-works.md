# How Fab Downloader works

## Signing in

- **Login:** the Epic Games Launcher's public OAuth client, with the authorization-code flow.
- **Session:** saved in `%APPDATA%\fab-downloader\session.bin`, encrypted for your Windows user
  with DPAPI, and refreshed when it expires.
- **Clipboard:** the window only reacts to copied text that contains `authorizationCode`, and
  stops watching the clipboard once it signs in, or after 10 minutes.
- **Settings:** the window remembers the destination, folder, project, engine and engine
  version in `%APPDATA%\fab-downloader\settings.json`.

## Engines

- **Detection** follows Unreal: an engine association like `5.3` is looked up in the
  launcher's install list (`%ProgramData%\Epic\UnrealEngineLauncher\LauncherInstalled.dat`),
  any other value in the engines registered under
  `HKCU\Software\Epic Games\Unreal Engine\Builds`, and an empty one in the folders around the
  project. The Engine list in the window comes from the same two places, plus engine folders
  you add with **...** (an engine's root, or the `Engine` folder inside it).
- **Staging:** downloads are assembled in the project's `Saved\fabdl` (or the engine's
  `Engine\Intermediate\fabdl`), on the same drive, so installing is a quick move.

## Code plugins

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

## Asset packs

- **Where:** the pack's `Content` folder is merged into the project's `Content`, so the assets
  keep their `/Game/...` paths, like the launcher's **Add to Project**.
- **Replacing:** files the project already has are moved to `Saved\ContentBackups\<date>`
  first. If a file cannot be moved, usually because the Unreal Editor has it open, every change
  is undone.
- **Not supported:** asset packs cannot be installed into an engine, and complete sample
  projects should be downloaded instead.

## Downloading from Fab

1. **Library:** `GET https://www.fab.com/e/accounts/{account}/ue/library`. Each item lists
   `projectVersions`, and each entry pairs an `artifactId` with its `engineVersions`.
2. **Build:** `POST https://www.fab.com/e/artifacts/{artifactId}/manifest` returns the current
   build version and signed manifest URLs for several CDNs.
3. **Files:** the build manifest (binary or JSON) lists each file as parts of compressed
   chunks. Fab Downloader downloads every chunk once, falling back to the other CDNs, and
   rebuilds the files. Each file must pass the SHA-1 check from the manifest. Files are written
   to a staging folder and moved into place only when all of them pass.
