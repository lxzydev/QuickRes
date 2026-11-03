import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from packaging.version import InvalidVersion, Version


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?")


def read_version(path, expected=""):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    value = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
    )
    if not isinstance(value, str) or not VERSION_PATTERN.fullmatch(value):
        raise ValueError("Use a version such as 1.3.0 or 1.3.0-beta.1")
    if expected and expected.removeprefix("v") != value:
        raise ValueError(f"Requested version {expected} does not match source version {value}")
    return value


def select_previous(releases, version):
    published = []
    for release in releases:
        if release["draft"]:
            continue
        try:
            parsed = Version(release["tag_name"].removeprefix("v"))
        except InvalidVersion:
            continue
        if parsed >= Version(version):
            raise ValueError(f"Version {version} must be newer than published {release['tag_name']}")
        published.append((parsed, release["tag_name"]))
    return max(published)[1] if published else None


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True, encoding="utf-8").strip()


def write_notes(root, repository, version, previous, head):
    if previous:
        subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", previous, head], check=True)
    revision = f"{previous}..{head}" if previous else head
    changes = git(root, "log", "--no-merges", "--format=- %s (%h)", revision)
    link = f"https://github.com/{repository}/compare/{previous}...v{version}" if previous else f"https://github.com/{repository}/commits/{head}"
    return f"# QuickRes {version}\n\n## Changes\n\n{changes or 'No code changes.'}\n\n[Full changelog]({link})\n\nWindows x64 portable build.\n"


def releases_for(repository):
    result = subprocess.check_output(
        ["gh", "api", "--paginate", "--slurp", f"repos/{repository}/releases?per_page=100"],
        text=True, encoding="utf-8",
    )
    return [release for page in json.loads(result) for release in page]


def prepare(repository, expected):
    version = read_version(ROOT / "quickres" / "__init__.py", expected)
    releases = releases_for(repository)
    previous = select_previous(releases, version)
    head = git(ROOT, "rev-parse", "HEAD")
    tag = f"v{version}"
    existing = next((item for item in releases if item["tag_name"] == tag), None)
    if existing and existing["target_commitish"] != head:
        raise ValueError("An existing draft targets another commit; choose a new version")
    tagged = subprocess.run(["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"], cwd=ROOT, capture_output=True, text=True)
    if tagged.returncode == 0 and tagged.stdout.strip() != head:
        raise ValueError(f"Tag {tag} already points to another commit")
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    (output / "release-notes.md").write_text(write_notes(ROOT, repository, version, previous, head), encoding="utf-8")
    values = {"version": version, "tag": tag, "prerelease": str(Version(version).is_prerelease).lower()}
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            for name, value in values.items():
                stream.write(f"{name}={value}\n")
    print(json.dumps(values))


def checksum():
    path = ROOT / "dist" / "QuickRes.exe"
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    (path.parent / "SHA256SUMS.txt").write_text(f"{digest}  QuickRes.exe\n", encoding="utf-8")


def draft(repository):
    version = read_version(ROOT / "quickres" / "__init__.py")
    tag = f"v{version}"
    head = git(ROOT, "rev-parse", "HEAD")
    releases = releases_for(repository)
    select_previous(releases, version)
    existing = next((item for item in releases if item["tag_name"] == tag), None)
    assets = [str(ROOT / "dist" / name) for name in ("QuickRes.exe", "SHA256SUMS.txt")]
    options = ["--repo", repository]
    if existing:
        if not existing["draft"] or existing["target_commitish"] != head:
            raise ValueError("Refusing to replace a published release or a draft for another commit")
        subprocess.run(["gh", "release", "upload", tag, *assets, *options, "--clobber"], check=True)
        subprocess.run(["gh", "release", "edit", tag, *options, "--notes-file", str(ROOT / "dist" / "release-notes.md")], check=True)
    else:
        subprocess.run([
            "gh", "release", "create", tag, *assets, *options, "--draft", "--target", head,
            "--title", f"QuickRes {version}", "--notes-file", str(ROOT / "dist" / "release-notes.md"),
            "--prerelease=" + str(Version(version).is_prerelease).lower(),
        ], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "checksum", "draft"])
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--version", default="")
    args = parser.parse_args()
    if args.command != "checksum" and not args.repository:
        parser.error("--repository is required")
    if args.command == "prepare":
        prepare(args.repository, args.version)
    elif args.command == "checksum":
        checksum()
    else:
        draft(args.repository)
