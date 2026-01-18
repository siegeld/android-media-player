# /commit

Commit changes with smart version bumping, changelog updates, tagging, and push to all remotes.

## Workflow

### 1. Gather Current State (parallel)

Run these commands in parallel:
- `git status` - see untracked/modified files (never use -uall flag)
- `git diff` - see staged and unstaged changes
- `git log --oneline -5` - recent commit message style
- `git remote -v` - list all remotes for pushing

Read current versions:
- `app/build.gradle.kts`: versionCode and versionName (APK version)
- `update-server.py`: SERVER_VERSION constant (Server version)

### 2. Determine What Changed

Categorize modified files:

**APK changes** (bump APK version):
- `app/src/**` - Kotlin/Java source code
- `app/build.gradle.kts` - dependencies or build config
- `app/*.xml` - Android manifests/layouts

**Server changes** (bump SERVER_VERSION):
- `update-server.py` - server code
- `Dockerfile*` - Docker configuration
- `docker-compose*.yml` - Docker compose config

**No version bump needed**:
- `README.md`, `CHANGELOG.md` - documentation only
- `.claude/**` - Claude configuration
- `.gitignore`, `.editorconfig` - project config

### 3. Smart Version Bump Rules

Parse current version as MAJOR.MINOR.PATCH (e.g., "2.0.6" -> 2, 0, 6)

**MAJOR bump** (X.0.0) - Breaking changes:
- API contract changes that break existing clients
- Removal of features or endpoints
- Database schema changes requiring migration
- User explicitly requests major bump

**MINOR bump** (X.Y.0) - New features:
- New functionality added
- New API endpoints
- New configuration options
- Significant enhancements to existing features

**PATCH bump** (X.Y.Z) - Bug fixes (default):
- Bug fixes
- Performance improvements
- Code refactoring without behavior change
- Dependency updates
- Documentation in code

**Heuristics for auto-detection**:
- Files added = likely MINOR (new feature)
- Files deleted = could be MAJOR (removal) - ask user
- Only modifications = likely PATCH (fix/refactor)
- New classes/functions with "Feature" or significant new code = MINOR
- Changes with "fix", "bug", "issue" in context = PATCH

When uncertain, ask user: "Is this a patch (bug fix), minor (new feature), or major (breaking change)?"

### 4. Update Version Numbers

**For APK version** in `app/build.gradle.kts`:
```kotlin
versionCode = N      // Increment by 1
versionName = "X.Y.Z" // Apply version bump
```

**For Server version** in `update-server.py`:
```python
SERVER_VERSION = "X.Y.Z"  # Apply version bump
```

### 5. Update CHANGELOG.md

Add entry at top of appropriate section following Keep a Changelog format:

**For Server changes** (under `# Server/Monitor Changelog`):
```markdown
## [X.Y.Z] - YYYY-MM-DD

### Added/Changed/Fixed/Removed
- **Feature name** - Brief description
```

**For APK changes** (under `# Android APK Changelog`):
```markdown
## [X.Y.Z] - YYYY-MM-DD

### Added/Changed/Fixed/Removed
- **Feature name** - Brief description
```

**Update version history tables** at bottom of CHANGELOG.md:
- Add row to `### Server/Monitor Versions` table if server changed
- Add row to `### APK Versions` table if APK changed

### 6. Rebuild if Needed

**If APK changed**:
```bash
./build.sh
```
This updates the APK binary in `app/build/outputs/apk/debug/`

**If Server changed**:
```bash
docker compose build update-server
docker compose up -d update-server
```

### 7. Commit

Stage and commit all changes:
```bash
git add -A
git commit -m "$(cat <<'EOF'
Brief description of changes (vX.Y.Z)

Optional longer description if needed.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
EOF
)"
```

**Commit message guidelines**:
- First line: imperative mood, max 72 chars, include version
- Focus on "why" not "what"
- Reference issue numbers if applicable

### 8. Tag

Always tag with the server version (git tags follow server versioning):
```bash
git tag -a vX.Y.Z -m "Server vX.Y.Z / APK vY.Y.Z - Brief description"
```

Include both versions in tag message if both changed.

### 9. Push to All Remotes

Push commits and tags to all configured remotes:
```bash
git remote | xargs -I {} git push {} master --tags
```

Or explicitly:
```bash
git push origin master --tags
git push github master --tags
```

### 10. Verify

After push, confirm:
- `git status` shows clean working tree
- `git log --oneline -1` shows new commit
- `git tag -l | tail -1` shows new tag

## Safety Rules

- **NEVER** commit files containing secrets (.env, credentials.json, API keys)
- **NEVER** use `--no-verify` to skip pre-commit hooks
- **NEVER** use `--force` push unless explicitly requested
- **NEVER** amend commits that have been pushed
- **ASK** user if unsure about version bump type
- **WARN** if committing binary files other than the APK

## Examples

**Bug fix to APK**:
- Bump: 2.0.6 -> 2.0.7, versionCode 36 -> 37
- Commit: "Fix audio sync drift in Sendspin playback (v3.0.4)"
- Tag: v3.0.4

**New server feature**:
- Bump: 3.0.4 -> 3.1.0
- Commit: "Add device grouping to web dashboard (v3.1.0)"
- Tag: v3.1.0

**Both APK and Server changed**:
- Bump both versions appropriately
- Commit: "Add real-time log streaming (v3.1.0)"
- Tag: v3.1.0 (server version)
- Tag message: "Server v3.1.0 / APK v2.1.0 - Add real-time log streaming"
