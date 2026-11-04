import hashlib
import json
from types import SimpleNamespace

import pytest
import velopack

from quickres import updater
from scripts import release


@pytest.fixture
def sdk(monkeypatch):
    updater._sessions.clear()
    calls = []
    info = SimpleNamespace(TargetFullRelease=SimpleNamespace(Version='99.0.0', Size=100, FileName='QuickRes-99.0.0-full.nupkg', SHA256='a' * 64))

    class Manager:
        def check_for_updates(self):
            return info

        def download_updates(self, update, callback):
            assert update is info
            calls.append('download')
            callback(50)
            callback(100)

        def wait_exit_then_apply_updates(self, update, **kwargs):
            assert update is info
            assert kwargs == {'silent': True, 'restart': True}
            calls.append('apply')

    manager = Manager()
    monkeypatch.setattr(updater, '_create_manager', lambda: manager)
    yield manager, calls, updater.fetch_version_info()
    updater._sessions.clear()


def test_only_checked_native_update_is_downloaded_then_applied(sdk, monkeypatch):
    _, calls, info = sdk
    events = []
    updater.apply_update('https://untrusted.invalid/payload.exe', info,
                         download_only=True, progress_callback=events.append)
    assert calls == ['download']
    assert events[1]['downloaded_bytes'] == 50
    assert events[-1]['stage'] == 'verifying'
    exits = []
    monkeypatch.setattr(updater.os, '_exit', exits.append)
    updater.confirm_update(None, info, reuse_download=True)
    assert calls == ['download', 'apply']
    assert exits == [0]


def test_repeated_checks_reuse_the_same_available_update_session(sdk):
    _, calls, initial = sdk
    for _ in range(40):
        assert updater.fetch_version_info()['update_token'] == initial['update_token']
    assert len(updater._sessions) == 1


def test_unchecked_updates_and_reuse_before_download_are_refused(sdk):
    _, calls, info = sdk
    with pytest.raises(RuntimeError, match='Check for updates'):
        updater.apply_update('https://github.com/lxzydev/QuickRes/fake.exe', {'version': '99.0.0'})
    with pytest.raises(RuntimeError, match='No verified update'):
        updater.apply_update(None, info, reuse_download=True)
    assert calls == []


def test_native_download_integrity_failure_never_allows_apply(sdk):
    manager, calls, info = sdk

    def failed(*args):
        raise RuntimeError('checksum mismatch')

    manager.download_updates = failed
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        updater.apply_update(None, info, download_only=True)
    with pytest.raises(RuntimeError, match='No verified update'):
        updater.apply_update(None, info, reuse_download=True)
    assert calls == []


def test_failed_apply_does_not_exit_and_can_be_retried(sdk, monkeypatch):
    manager, _, info = sdk
    updater.apply_update(None, info, download_only=True)
    manager.wait_exit_then_apply_updates = lambda *a, **kw: (_ for _ in ()).throw(OSError('locked'))
    exits = []
    monkeypatch.setattr(updater.os, '_exit', exits.append)
    with pytest.raises(OSError, match='locked'):
        updater.confirm_update(None, info, reuse_download=True)
    assert exits == []


def test_legacy_build_has_explicit_migration_instead_of_sdk_update(monkeypatch):
    monkeypatch.setattr(updater, '_create_manager', lambda: (_ for _ in ()).throw(RuntimeError('not installed')))
    info = updater.fetch_version_info()
    assert info['migration_required'] is True
    assert info['installer_url'] == 'https://github.com/lxzydev/QuickRes/releases/latest'
    assert updater.update_available('1.0.0', info) is False


def test_stable_and_preview_channels_are_explicit(monkeypatch):
    calls = []
    monkeypatch.setattr(updater.velopack, 'GithubSource', lambda *a, **kw: calls.append((a, kw)) or 'source')
    monkeypatch.setattr(updater.velopack, 'UpdateOptions', lambda *a: calls.append(a) or 'options')
    monkeypatch.setattr(updater.velopack, 'UpdateManager', lambda *a: a)
    for version, prerelease, channel in [('1.3.0', False, 'win'), ('1.4.0-beta.1', True, 'win-beta')]:
        monkeypatch.setattr(updater, '__version__', version)
        updater._create_manager()
        assert calls[-2] == (('https://github.com/lxzydev/QuickRes',), {'prerelease': prerelease})
        assert calls[-1] == (False, 10, channel)


@pytest.mark.parametrize('remote,expected', [('1.3.0-beta.1', False), ('1.3.0', True), ('1.3.0garbage', False)])
def test_semver_prerelease_ordering(remote, expected):
    assert updater.is_newer_version('1.3.0-beta.2', remote) is expected


def test_real_sdk_local_feed_checks_and_downloads_with_integrity(tmp_path):
    app = tmp_path / 'app'
    app.mkdir()
    (app / 'Update.exe').touch()
    manifest = app / 'sq.version'
    manifest.write_text('<package><metadata><id>QuickRes</id><version>1.0.0</version><title>QuickRes</title><authors>QuickRes</authors><description>Test</description><mainExe>QuickRes.exe</mainExe></metadata></package>')
    feed = tmp_path / 'feed'
    feed.mkdir()
    payload = b'update package bytes'
    package = feed / 'QuickRes-2.0.0-full.nupkg'
    package.write_bytes(payload)
    asset = {'PackageId': 'QuickRes', 'Version': '2.0.0', 'Type': 'Full', 'FileName': package.name,
             'Size': len(payload), 'SHA1': hashlib.sha1(payload).hexdigest(), 'SHA256': hashlib.sha256(payload).hexdigest()}
    (feed / 'releases.win.json').write_text(json.dumps({'Assets': [asset]}))
    locator = velopack.VelopackLocatorConfig(app, app / 'Update.exe', app / 'packages', manifest, app, True)
    manager = velopack.UpdateManager(str(feed), velopack.UpdateOptions(False, -1, 'win'), locator)
    info = manager.check_for_updates()
    assert info.TargetFullRelease.Version == '2.0.0'
    progress = []
    manager.download_updates(info, progress.append)
    assert progress and progress[-1] == 100
    assert (app / 'packages' / package.name).read_bytes() == payload
    (app / 'packages' / package.name).unlink()
    package.write_bytes(b'x' * len(payload))
    with pytest.raises(Exception):
        manager.download_updates(info)
    assert not (app / 'packages' / package.name).exists()


def test_release_assets_include_all_feed_files_and_verify_packages(tmp_path, monkeypatch):
    monkeypatch.setattr(release, 'ROOT', tmp_path)
    (tmp_path / 'quickres').mkdir()
    (tmp_path / 'quickres' / '__init__.py').write_text('__version__ = "1.3.0"')
    folder = tmp_path / 'dist' / 'releases'
    folder.mkdir(parents=True)
    package = folder / 'QuickRes-1.3.0-full.nupkg'
    package.write_bytes(b'package')
    asset = {'FileName': package.name, 'Size': 7, 'SHA256': hashlib.sha256(b'package').hexdigest()}
    (folder / 'releases.win.json').write_text(json.dumps({'Assets': [asset]}))
    for name in ['QuickRes-win-Setup.exe', 'QuickRes-win-Portable.zip', 'assets.win.json', 'RELEASES']:
        (folder / name).write_bytes(b'file')
    release.checksum()
    sums = (folder / 'SHA256SUMS.txt').read_text()
    for name in [p.name for p in release.release_assets()]:
        assert name in sums
    package.write_bytes(b'changed')
    with pytest.raises(ValueError, match='checksum mismatch'):
        release.release_assets()
