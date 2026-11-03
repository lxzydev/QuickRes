# QuickRes
<p align="center">
  <img src="QuickRes.png" width="140" alt="QuickRes">
</p>
if youve been false banned read https://sites.google.com/view/quickresonline/home

A tiny Windows tool for instantly switching your resolution with one click.

Built with stretched resolutions for Valorant in mind, but it works for switching resolution on anything.

## Why use this over NVIDIA Control Panel?

Switching resolution manually through NVIDIA Control Panel (or AMD/Intel equivalents) requires opening the panel, finding the resolution, and clicking Apply. QuickRes skips all of that: set a hotkey, pick a resolution, done.

## Install

1. Go to releases and download the latest `QuickRes.exe`
2. Run it (no install needed, it's a single portable exe)

Windows may show a SmartScreen warning ("Windows protected your PC") since the exe isn't code-signed.

## Using it with Valorant

Stretched resolutions only works while you're fully loaded into a match. Applying too early (agent select, loading screen) gives you black bars instead of stretch. Here's the setup:

**One-time setup:**
1. Open Monitors in quickre
2. Disable every monitor listed
3. In Valorant's video settings, set your aspect ratio method to Fill

**Every match:**
1. During agent select and while loading in, stay on your **native resolution** (e.g. 1920x1080, or 2560x1440 on a larger monitor)
2. Once you are **fully loaded into the game** (not agent select, not the loading screen), press your hotkey to switch to stretch
3. Play the match at your stretched res
4. Press your hotkey again to go native
5. Repeat next match, since loading into a new game resets you back to native resolution

## Custom resolutions

QuickRes can only switch to resolutions your GPU driver already knows about. If a resolution doesn't show up or gives an error, you need to register it with your driver first:

- **NVIDIA:** NVIDIA Control Panel > Display > Change Resolution > Customize > Create Custom Resolution
- **AMD:** AMD Software > Display > Custom Resolutions > Create New
- **Intel:** Intel Graphics Software > Display > Custom Resolutions

Once it's added there, QuickRes will be able to switch to it. You can also enter any custom width x height directly in the app.

More troubleshooting is in the in-app FAQ.

## License / usage

This project is fully open source. Feel free to modify it for your own use.

**Do not sell this tool or any modified version of it.**

## Building from source

### GitHub Actions

The `CI` workflow runs Python syntax checks, Ruff error checks, dependency checks, and the test suite on Windows for pushes and pull requests. Test reports are saved as workflow artifacts.

To prepare a release:

1. Set `__version__` in `quickres/__init__.py` to a version newer than all published releases, such as `1.3.0` or `1.3.0-beta.1`, and push the change.
2. Open **Actions > Draft release > Run workflow** and choose the branch to build. The optional version field must match the source version; it does not change it.
3. After checks pass, the workflow builds `QuickRes.exe` from `QuickRes.spec` and creates a draft containing the executable, `SHA256SUMS.txt`, and a changelog of commits since the previous published version. Files and release notes are also available as a workflow artifact.
4. Review the draft and publish it manually. Beta, alpha, and RC versions are marked as prereleases.

Rerunning the same commit can refresh its draft. Published releases, existing tags pointing to other commits, and drafts for other commits are not overwritten.

QuickRes uses [pywebview](https://pywebview.flowrl.com/) for its GUI (the panel is a single `quickres/webview/panel.html` file rendered in an embedded EdgeChromium window), not Tkinter. Windows only.

```bash
pip install -r requirements.txt
python -m PyInstaller --onefile --windowed --icon=icon.ico --add-data "icon.ico;." --add-data "quickres/webview;quickres/webview" --name QuickRes main.py
```

`QuickRes.spec` bundles `quickres/webview/panel.html` and `quickres/webview/QuickRes.png` at the correct path for the frozen build and excludes Tkinter (unused since the GUI rewrite). The resulting single-file `dist/QuickRes.exe` is portable, no install needed.
