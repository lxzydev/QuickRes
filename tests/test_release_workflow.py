import subprocess

import pytest

from scripts.release import read_version, select_previous, write_notes


def test_version_is_read_without_executing_source(tmp_path):
    source = tmp_path / "__init__.py"
    source.write_text('__version__ = "1.3.0-beta.2"\nraise RuntimeError("must not execute")\n')
    assert read_version(source, "v1.3.0-beta.2") == "1.3.0-beta.2"
    with pytest.raises(ValueError, match="does not match"):
        read_version(source, "1.3.0")


def test_previous_release_ignores_drafts_and_uses_version_order():
    releases = [
        {"tag_name": "v1.1.0", "draft": False},
        {"tag_name": "v1.3.0", "draft": True},
        {"tag_name": "v1.2.0", "draft": False},
    ]
    assert select_previous(releases, "1.3.0") == "v1.2.0"


@pytest.mark.parametrize("version", ["1.1.1", "1.2.0"])
def test_published_version_cannot_be_replaced_or_downgraded(version):
    with pytest.raises(ValueError, match="published"):
        select_previous([{"tag_name": "v1.2.0", "draft": False}], version)


def test_changelog_contains_only_changes_since_previous_release(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "Old change")
    git("tag", "v1.2.0")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "Fix hotkey recovery")
    notes = write_notes(tmp_path, "owner/repo", "1.3.0", "v1.2.0", git("rev-parse", "HEAD"))
    assert "Fix hotkey recovery" in notes
    assert "Old change" not in notes
    assert "/compare/v1.2.0...v1.3.0" in notes
    assert "QuickRes 1.3.0" in notes


def test_new_release_is_always_a_draft_for_the_built_commit(tmp_path, monkeypatch):
    from scripts import release

    (tmp_path / "quickres").mkdir()
    (tmp_path / "quickres" / "__init__.py").write_text('__version__ = "1.3.0"')
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "releases_for", lambda _: [])
    monkeypatch.setattr(release, "git", lambda *args: "a" * 40)
    commands = []
    monkeypatch.setattr(release.subprocess, "run", lambda command, **kwargs: commands.append(command))

    release.draft("owner/repo")

    assert len(commands) == 1
    command = commands[0]
    assert command[:4] == ["gh", "release", "create", "v1.3.0"]
    assert "--draft" in command
    assert command[command.index("--target") + 1] == "a" * 40
    assert str(tmp_path / "dist" / "QuickRes.exe") in command


def test_draft_for_another_commit_is_not_overwritten(tmp_path, monkeypatch):
    from scripts import release

    (tmp_path / "quickres").mkdir()
    (tmp_path / "quickres" / "__init__.py").write_text('__version__ = "1.3.0"')
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "git", lambda *args: "a" * 40)
    monkeypatch.setattr(release, "releases_for", lambda _: [
        {"tag_name": "v1.3.0", "draft": True, "target_commitish": "b" * 40},
    ])
    with pytest.raises(ValueError, match="another commit"):
        release.draft("owner/repo")
