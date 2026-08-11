# Working-tree review (`HEAD` plus untracked files)

## Findings

### P1 - The unattended review runs with unrestricted host access

[`settings.json`](../.claude/settings.json#L13) invokes Codex with `-s danger-full-access` on every Claude `Stop` event while passing it a prompt to inspect the dirty worktree. That lets untrusted content added to the repository influence an agent with access beyond the repository, including credentials and other local data. This review only needs to create the report within the workspace.

Run the hook with `workspace-write` instead, or reserve unrestricted execution for an explicit interactive command.

### P2 - The review artifact retriggers the Stop hook indefinitely

[`settings.json`](../.claude/settings.json#L13) schedules a review whenever `git status --porcelain` has any output. The invoked reviewer writes `planning/Review-commit.md`, which itself leaves the worktree dirty. Consequently, every subsequent Claude `Stop` event starts another review even when the source changes have not changed; asynchronous runs can also overlap and overwrite the report.

Ignore `planning/Review-commit.md` in the dirty-tree check, or record the reviewed Git state and skip work when that state is unchanged.

### P2 - The updated agent instruction fails whitespace validation

[`change-reviewer.md`](../.claude/agents/change-reviewer.md#L9) has trailing whitespace after the closing backtick. `git diff --check HEAD` reports it, which will fail whitespace-enforcing CI or pre-commit validation.

Remove the final space.

## Verification

`git diff --check HEAD` reports the trailing-whitespace error above. The only untracked file is this review artifact.
