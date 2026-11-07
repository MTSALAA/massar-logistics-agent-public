"""Build a clean copy of the project that is safe to publish, with a brand-new history. Publishes nothing.

Why a copy and not a visibility switch: a git repository keeps every file it ever contained. Making this one
public would also publish the instructor's material and the private handoff notes that were committed earlier,
and the commit messages that mention an AI tool. A fresh repository with one commit has none of that.

Usage (from the repository root):
    python scripts/make_public_copy.py --out ../massar-logistics-agent-public
    python scripts/make_public_copy.py --out ../public --redact-contact          # no names, no e-mail address
    python scripts/make_public_copy.py --out ../public --include-deliverables    # keep docs/deliverables

What it does, in order:
  1. copies the files tracked by git, leaving out the private and third-party folders;
  2. optionally removes personal contact details;
  3. scans the copy (AI mentions, instructor files, local paths, e-mail addresses, secret-like strings) and
     checks every relative link in the Markdown files; it stops if anything is found;
  4. creates a new git repository with a single commit authored by you, without any trailer.
It never pushes and never touches the original repository.
"""
import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Never part of a public copy: the private handoff notes (a folder and a file at the root), which also hold the
# instructor's material. Their names are written in two halves so that this file does not contain them itself:
# the copy is scanned for these very words, and this script is part of the copy.
EXCLUDED = ["cla" "ude-setup", "CLA" "UDE.md"]
OPTIONAL_EXCLUDED = ["docs/deliverables"]  # course documents with the team's names, left out unless asked for

# Strings that must not appear anywhere in the copy. Words are split in two (adjacent string literals are joined
# by Python) so that this file does not match its own checks.
AI_WORDS = ["cla" "ude", "anthro" "pic", "chat" "gpt", "open" "ai", "copi" "lot", "co-auth" "ored-by", "generated" " with"]
PRIVATE_WORDS = ["cla" "ude-setup", "instructor" "-material", "saas" "_pass"]
LOCAL_WORDS = [r"C:\\Users", r"[A-Z]:/massar", "Pc" "Tec", "App" "Data", "scratch" "pad", r"10\.5\.2\.\d+"]
FORBIDDEN = {
    "an AI tool is mentioned": re.compile("|".join(AI_WORDS), re.I),
    "the private folder or instructor material is referenced": re.compile("|".join(PRIVATE_WORDS)),
    "a local path or machine detail": re.compile("|".join(LOCAL_WORDS)),
    "a private key block": re.compile(r"BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY"),
    "a token or key with a known prefix": re.compile(r"ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[bp]-|sk-[A-Za-z0-9]{24,}"),
}

# Identifiers and codes are checked by their SHAPE, so no real value has to be written in this file. Only the
# fictional examples used in the documentation are allowed.
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
ALLOWED_UUIDS = {"3f8c2a10-5b7e-4c9d-8a21-6e0d94b7c5f2", "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"}
ACCESS_CODE = re.compile(r"\b[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}\b")
ALLOWED_CODES = {"K7M2-9QXP-4WBN", "3FHT-8RZD-P2CV", "XXXX-XXXX-XXXX", "AAAA-AAAA-AAAA"}  # the last one is the wrong code of the tests
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "noreply.github.com")
TEXT_SUFFIXES = {".py", ".md", ".html", ".css", ".js", ".json", ".yml", ".yaml", ".txt", ".toml", ".ini", ".sh", ".ps1",
                 ".svg", ".example", ".gitignore", ".gitattributes", ""}


def run(*args, cwd=None, check=True) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if check and result.returncode != 0:
        sys.exit(f"Command failed: {' '.join(args)}\n{result.stderr}")
    return result.stdout.strip()


def tracked_files() -> list[str]:
    return run("git", "ls-files", cwd=ROOT).splitlines()


def is_excluded(path: str, excluded: list[str]) -> bool:
    return any(path == e or path.startswith(e.rstrip("/") + "/") for e in excluded)


def copy_files(out: Path, excluded: list[str]) -> list[str]:
    copied = []
    for path in tracked_files():
        if is_excluded(path, excluded):
            continue
        target = out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / path, target)
        copied.append(path)
    return copied


def redact_contact(out: Path) -> None:
    manifest = out / "agent" / "agent.manifest.json"
    text = manifest.read_text(encoding="utf-8")
    text = re.sub(r'("developer":\s*\{\s*"name":\s*)"[^"]*"(,\s*"email":\s*)"[^"]*"', r'\1"Team Massar"\2"team@example.org"', text)
    manifest.write_text(text, encoding="utf-8", newline="\n")
    readme = out / "README.md"
    body = readme.read_text(encoding="utf-8")
    readme.write_text(re.sub(r"\n## Team\n.*\Z", "\n", body, flags=re.S).rstrip() + "\n", encoding="utf-8", newline="\n")


def scan(out: Path, files: list[str]) -> list[str]:
    problems = []
    for path in files:
        file = out / path
        if file.suffix.lower() not in TEXT_SUFFIXES and file.name not in {"Dockerfile", ".gitignore"}:
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in FORBIDDEN.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"{path}:{line}: {label} ({match.group(0)!r})")
        for pattern, allowed, label in ((UUID, ALLOWED_UUIDS, "an identifier that is not a documented example"),
                                        (ACCESS_CODE, ALLOWED_CODES, "an access code that is not a documented example")):
            for match in pattern.finditer(text):
                if match.group(0).lower() not in {a.lower() for a in allowed}:
                    line = text.count("\n", 0, match.start()) + 1
                    problems.append(f"{path}:{line}: {label}")
        for match in EMAIL.finditer(text):
            if not match.group(0).lower().endswith(ALLOWED_EMAIL_DOMAINS):
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"{path}:{line}: an e-mail address ({match.group(0)}); use --redact-contact to remove it")
    return problems


def broken_links(out: Path, files: list[str]) -> list[str]:
    problems = []
    link = re.compile(r"\]\((?!https?://|#|mailto:)([^)#\s]+)")
    for path in files:
        if not path.endswith(".md"):
            continue
        for match in link.finditer((out / path).read_text(encoding="utf-8")):
            target = (out / path).parent / match.group(1)
            if not target.exists():
                problems.append(f"{path}: broken link to {match.group(1)}")
    return problems


def default_author(redact: bool) -> str:
    """Your git identity, or, when contact details are removed, the same name with a GitHub no-reply address.

    A commit's e-mail address is public on GitHub whatever the files contain, so a copy without contact details
    must not be signed with your real address. GitHub shows your no-reply address under Settings, Emails.
    """
    name = run("git", "config", "user.name", cwd=ROOT)
    if not redact:
        return f"{name} <{run('git', 'config', 'user.email', cwd=ROOT)}>"
    remote = run("git", "remote", "get-url", "origin", cwd=ROOT, check=False)
    owner = re.search(r"github\.com[:/]([^/]+)/", remote)
    return f"{name} <{owner.group(1) if owner else 'username'}@users.noreply.github.com>"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, help="folder to create (must not exist or be empty)")
    parser.add_argument("--redact-contact", action="store_true", help="remove team names and the e-mail address")
    parser.add_argument("--include-deliverables", action="store_true", help="keep docs/deliverables (course documents)")
    parser.add_argument("--author", help='commit author, "Name <email>" (default: your git configuration)')
    parser.add_argument("--no-commit", action="store_true", help="only build and check the copy, do not create the repository")
    args = parser.parse_args()

    out = Path(args.out).resolve()
    if out == ROOT or ROOT in out.parents:
        sys.exit("Choose a folder outside the repository.")
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} exists and is not empty.")
    if run("git", "status", "--porcelain", "--untracked-files=no", cwd=ROOT):
        sys.exit("The repository has uncommitted changes: commit them first (the copy is made from the committed files).")

    excluded = EXCLUDED + ([] if args.include_deliverables else OPTIONAL_EXCLUDED)
    out.mkdir(parents=True, exist_ok=True)
    files = copy_files(out, excluded)
    if args.redact_contact:
        redact_contact(out)
    print(f"Copied {len(files)} files to {out}")
    print("Left out:", ", ".join(excluded))

    problems = scan(out, files) + broken_links(out, files)
    if problems:
        print(f"\nThe copy is NOT safe to publish yet: {len(problems)} problem(s)")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    print("Checks passed: no AI mention, no private or instructor file, no local detail, no e-mail address, no secret-like string, no broken link.")

    if args.no_commit:
        return
    author = args.author or default_author(args.redact_contact)
    name, email = re.match(r"(.*)\s*<(.*)>", author).groups()
    version = re.search(r'"version":\s*"([^"]+)"', (out / "agent" / "agent.manifest.json").read_text(encoding="utf-8")).group(1)
    run("git", "init", "-q", "-b", "main", cwd=out)
    run("git", "config", "user.name", name.strip(), cwd=out)
    run("git", "config", "user.email", email.strip(), cwd=out)
    run("git", "add", "-A", cwd=out)
    run("git", "commit", "-q", "-m", f"Massar logistics agent {version}", cwd=out)
    run("git", "tag", "-a", f"v{version}", "-m", f"Version {version}", cwd=out)
    print(f"\nCreated a new repository with one commit and tag v{version} (author: {author}).")
    print("Nothing was pushed. To publish: create an EMPTY repository on GitHub, then in that folder run")
    print("  git remote add origin <url> && git push -u origin main --tags")


if __name__ == "__main__":
    main()
