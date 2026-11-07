# Security review (version 1.2.1)

## What it was for

Before making the repository public, the project was audited: its files and its whole git history, its code, its dependencies and its container. This page records what was found, what was fixed, and what remains a decision or a known limit. The standing summary for readers is [SECURITY.md](../../SECURITY.md).

## How it was done

| Method | Tool or approach |
|--------|------------------|
| Secrets and sensitive files | Searched every added line of every commit on every branch and tag, and every path ever committed; scanned the files with `detect-secrets` |
| Personal and third-party material | Searched the files for names, e-mail addresses, local paths and mentions of tools; listed what belongs to other people |
| Static analysis of the code | `bandit` |
| Known vulnerabilities in dependencies | `pip-audit` on the full dependency tree |
| Manual review | Sign-in and sessions, injection, escaping, request size, headers, external scripts, memory growth, container user |
| Proof that the fixes work | New tests (`tests/test_security.py`), and the key ones were checked to **fail against the old code** before being trusted |

## Findings and what was done

| Finding | Severity | Resolution |
|---------|----------|------------|
| 24 known vulnerabilities in three dependencies (Jinja2, protobuf through OR-Tools, Starlette through FastAPI) | Mostly low for this app (the vulnerable features were not used), but real | Upgraded to current versions (FastAPI 0.162, Starlette 1.10, Jinja2 3.1.6, OR-Tools 9.15, protobuf 6.33). `pip-audit` now reports none. The whole suite, the Docker image and a real plan on the road network were re-verified on the new versions. |
| Request bodies had no size limit (the form reader loaded any body into memory) | Medium | Bodies above 1 MB are refused with 413, by the announced length and by counting the bytes that arrive. |
| A value from the address bar could be inserted into a JSON attribute of a button | Medium (reflected parameter tampering) | The value now goes through a JSON encoder. A test renders the page with a hostile value and checks that no field or event handler can be added; it fails on the old template. Vehicle names are also limited to letters, digits, space and `. - _ /`. |
| Pages could be shown inside another site's frame; authenticated pages were cacheable | Medium (clickjacking, shared computers) | `X-Frame-Options`, CSP `frame-ancestors 'none'`, `Cache-Control: no-store` on pages, `nosniff` and a restrictive referrer policy. |
| The two libraries loaded from a public CDN were not integrity-checked | Medium (supply chain) | Pinned to an exact version and a SHA-384 hash. The hashes match the values the libraries' projects publish, and the browser accepted them. |
| The bookkeeping of failed sign-ins could grow without limit | Low | Bounded (5,000 entries, oldest forgotten first); the company and code fields are cut at their real maximum length. |
| One company could ask for a plan of any number of stops | Low | Limited to 500 shipments per plan. |
| The container ran as root | Medium (impact of any flaw) | It now starts through `entrypoint.py`, which hands the data volume to an unprivileged user, drops root and starts the server. Old volumes created by root are handed over automatically (checked on the real volume, data intact). |
| `bandit`: a weak hash for tracking numbers; two string-built SQL statements; a seeded random generator | Low / false positives | The hash is not used for security and is marked so; the SQL statements only use the module's own table names with a parameter for the value (explained in a comment); the generator is seeded test data on purpose. `bandit` now reports no issue. |

## What was found about the repository itself (for the decision to publish)

| Finding | Why it matters | What to do |
|---------|----------------|------------|
| **Two commits carry a co-author trailer naming an AI tool** (the first two, from the start of the project), and they are inside every branch and tag | Public history would show it, against the project's own rule | Publish from a fresh history (below). |
| **The instructor's material** (the contract documents and the platform core's source, 33 files) is in the files and in the history | It is not the team's to publish | Leave it out of any public copy. |
| **Private handoff notes that mention an AI tool** (a file at the root and a private folder, which also holds the instructor's material) | Against the rule that no project document mentions one | Leave them out of any public copy. |
| **Team members' names** (README, manifest) and **a personal e-mail address** (manifest); the course documents in `docs/deliverables/` carry names too | Personal data of students | A decision for the team: keep, or publish without. |
| **No licence file** | Without one, nobody may legally reuse the code | A decision for the team (and, possibly, the school). |
| Test values from the platform's own local set-up (a database password in the private notes) | Harmless but looks like a default credential | They stay in the private notes, which are not published. |

**No real secret was found** in the files or in any commit: no key, token, password, private key, or access code.

## The safe way to publish

Do **not** switch this repository to public: its history would go with it. Instead:

```bash
python scripts/make_public_copy.py --out ../massar-logistics-agent-public --redact-contact
```

`scripts/make_public_copy.py` copies the committed files without the private and third-party material, optionally removes personal contact details, **scans the copy** (AI mentions, instructor files, local paths, e-mail addresses, secret-like strings) and **checks every Markdown link**, refuses to go on if anything is found, and then creates a brand-new repository with a single commit and no trailer. It never pushes. The private repository stays as the working one.

## Limits (also in SECURITY.md)

No HTTPS in the agent itself (use a proxy); the company's key is stored in the database file in clear text because the agent must send it; activation cannot be authenticated; port 9000 is published on all interfaces by the demo compose file; the two libraries still come from a public CDN.
