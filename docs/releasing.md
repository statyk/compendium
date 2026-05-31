# Releasing Compendium

## Prerequisites (one-time setup)

1. **PyPI Trusted Publisher** — on https://pypi.org/manage/account/publishing/ add a pending publisher:
   - PyPI Project Name: `compendium-ils`
   - GitHub owner: `statyk`, repo: `compendium`, workflow: `release.yml`, environment: `pypi`

2. **GitHub Environment** — on https://github.com/statyk/compendium/settings/environments create an environment named `pypi` (no extra rules required; the name must match the workflow).

## How to cut a release

### 1. Bump the version

Edit `src/compendium/__init__.py` — it is the single source of truth:

```python
__version__ = "1.1.0"   # was 1.0.0
```

### 2. Commit and push

```bash
git add src/compendium/__init__.py
git commit -m "chore(release): bump version to 1.1.0"
git push origin master
```

### 3. Tag

```bash
git tag -a v1.1.0 -m "Compendium 1.1.0"
git push origin v1.1.0
```

### 4. Create the GitHub Release

Write release notes to a temp file, then publish:

```bash
cat > /tmp/release-notes.md << 'EOF'
Brief summary of what changed.

## Changes
- ...
EOF

gh release create v1.1.0 --title "Compendium 1.1.0" --notes-file /tmp/release-notes.md
```

Publishing the release triggers `.github/workflows/release.yml`, which builds the
package and publishes it to PyPI via OIDC (no token required).

### 5. Verify

Watch the Actions run:

```bash
gh run watch
```

Then confirm the package is live:

```bash
pip install --upgrade compendium-ils
compendium --version   # should print the new version
```

## Version numbering

This project follows [Semantic Versioning](https://semver.org/):

- **Patch** (`1.0.x`) — bug fixes, docs, minor tweaks; no new features.
- **Minor** (`1.x.0`) — new features, backward-compatible.
- **Major** (`x.0.0`) — breaking changes to the CLI interface, REST API, or DB schema migration path.
