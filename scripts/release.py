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
    return f"# QuickRes {version}\n\n## Changes\n\n{changes or 'No code changes.'}\n\n[Full changelog]({link})\n\nWindows x64 installer and portable ZIP. Updates are delivered by Velopack.\n"


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


def release_assets():
    folder = ROOT / "dist" / "releases"
    channel = "win-beta" if Version(read_version(ROOT / "quickres" / "__init__.py")).is_prerelease else "win"
    feed = folder / f"releases.{channel}.json"
    data = json.loads(feed.read_text(encoding="utf-8"))
    packages = []
    for asset in data["Assets"]:
        name = asset["FileName"]
        if Path(name).name != name or "/" in name or "\\" in name:
            raise ValueError("Unsafe package filename in release feed")
        path = folder / name
        if not path.is_file() or path.stat().st_size != asset["Size"]:
            raise ValueError(f"Missing or incomplete update package: {name}")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest.lower() != asset["SHA256"].lower():
            raise ValueError(f"Update package checksum mismatch: {name}")
        packages.append(path)
    installers = list(folder.glob("*-Setup.exe"))
    portable = list(folder.glob("*-Portable.zip"))
    if not packages or len(installers) != 1 or len(portable) != 1:
        raise ValueError("Release requires update packages, one installer, and one portable ZIP")
    extra = [path for path in (folder / f"assets.{channel}.json", folder / "RELEASES") if path.is_file()]
    return sorted(set(packages + [feed] + installers + portable + extra))


def checksum():
    lines = []
    for path in release_assets():
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        lines.append(f"{digest}  {path.name}\n")
    (ROOT / "dist" / "releases" / "SHA256SUMS.txt").write_text("".join(lines), encoding="utf-8")


def pack(vpk="vpk"):
    version = read_version(ROOT / "quickres" / "__init__.py")
    subprocess.run([
        vpk, "pack", "--packId", "QuickRes", "--packVersion", version,
        "--packDir", str(ROOT / "dist" / "QuickRes"), "--mainExe", "QuickRes.exe",
        "--outputDir", str(ROOT / "dist" / "releases"), "--runtime", "win-x64",
        "--channel", "win-beta" if Version(version).is_prerelease else "win",
        "--icon", str(ROOT / "icon.ico"), "--releaseNotes", str(ROOT / "dist" / "release-notes.md"),
    ], check=True)


def draft(repository):
    version = read_version(ROOT / "quickres" / "__init__.py")
    tag = f"v{version}"
    head = git(ROOT, "rev-parse", "HEAD")
    releases = releases_for(repository)
    select_previous(releases, version)
    existing = next((item for item in releases if item["tag_name"] == tag), None)
    options = ["--repo", repository]
    if existing and (not existing["draft"] or existing["target_commitish"] != head):
        raise ValueError("Refusing to replace a published release or a draft for another commit")
    assets = [str(path) for path in release_assets()] + [str(ROOT / "dist" / "releases" / "SHA256SUMS.txt")]
    if not Path(assets[-1]).is_file():
        raise ValueError("Generate release checksums before drafting")
    if existing:
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
    parser.add_argument("command", choices=["prepare", "pack", "checksum", "draft"])
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--version", default="")
    parser.add_argument("--vpk", default="vpk")
    args = parser.parse_args()
    if args.command in {"prepare", "draft"} and not args.repository:
        parser.error("--repository is required")
    if args.command == "prepare":
        prepare(args.repository, args.version)
    elif args.command == "pack":
        pack(args.vpk)
    elif args.command == "checksum":
        checksum()
    else:
        draft(args.repository)
