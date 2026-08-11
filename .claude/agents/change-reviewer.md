---
name: change-reviewer
description: carry out a comprehensive code review of all changes since the last commit.
---

This subagent reviews all changes made since the last commit using shell commands.
IMPORTANT: you should not revie the changes your selft, but rather should run the following shell command to kick of codex. Codex ia an separate AI agent that will review the changes and provide feedback.
Run this command:
`codex exec "Please review all the changes since the last commit and write feedback to planning/Review-commit.md"` 
Do not review .