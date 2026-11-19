# Git hygiene hooks

Generic hooks for agent workers (and humans): push feature branches without
dragging the whole history through the scanner, and never ship AI attribution
in commit messages.

## Hooks

- `commit-msg` — strips `Co-Authored-By:` trailers (and `Generated with/by ...`
  lines) that name an AI model or tool, before the commit is created. Human
  co-authors are kept.
- `pre-push` — scans **only the commits being pushed**:
  - updated ref: `remote_sha..local_sha`
  - new branch/ref: `local_sha --not --remotes=<remote>` (commits the remote
    already has are never re-scanned, so a clean new branch pushes even if old
    history would trip the guard)
  - deleted ref: skipped

  It blocks outgoing commits that carry AI co-author trailers (backstop for
  `--no-verify` commits) and, optionally, added lines matching a forbidden
  content regex.

## Install

Per-hook (keeps any other hooks you already have):

    cp hooks/commit-msg hooks/pre-push "$(git rev-parse --git-common-dir)/hooks/"
    chmod +x "$(git rev-parse --git-common-dir)/hooks/commit-msg" \
             "$(git rev-parse --git-common-dir)/hooks/pre-push"

Or use the directory directly (replaces ALL hooks in `.git/hooks`, so only do
this if you have no other local hooks):

    git config core.hooksPath hooks

Worktrees share the common dir's hooks, so installing once covers every
worktree of the repo.

## Configuration

Env vars win over git config. The AI pattern is an extended regex, matched
case-insensitively; setting it REPLACES the built-in default list
(`claude|anthropic|openai|chatgpt|gpt-[0-9]|codex|gemini|copilot|cursor|aider|devin|qwen|deepseek|llama|mistral|🤖`).

    git config hygiene.aiPattern 'claude|my-internal-bot'
    git config hygiene.forbiddenPattern 'INTERNAL_SECRET|/private/path'
    # or: GIT_HYGIENE_AI_PATTERN / GIT_HYGIENE_FORBIDDEN_PATTERN

`hygiene.forbiddenPattern` is off by default; when set, `pre-push` rejects
outgoing commits whose added lines match it.

Tests: `python -m pytest tests/test_git_hygiene_hooks.py`.