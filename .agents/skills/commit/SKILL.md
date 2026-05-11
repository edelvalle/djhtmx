---
name: commit
description: "Read this skill before making git commits. Creates commits following this project conventions with semantic splitting."
---

# Commit Messages

Create git commits following the commit conventions laid in the next sections.

By invoking this skill, the user is explicitly requesting to create one or more commits.  You will follow the procedure mentioned in the section "Procedure" without deviation.  After the approved commits are created, you will not create more commits unless requested again.

## Format

```
<kind(module)>: Brief descriptive message

Rationale of the change, informed by the Shortcut story context.
```

- **kind**: Either 'fix', 'feat', 'chore', 'docs', 'style', 'release'.
- **module**: The main target the of changes; usually only needed for 'fix' or 'feat'; if the change involves many files (after semantic splitting), don't include a `(module)`.
- **message**: Short, imperative mood, under 72 characters total for the first line.  No trailing period.

## Body

Add a blank line after the subject, then short paragraphs.  Frame changes in terms of what they enable and, most importantly, WHY the change is needed, not implementation details.

In order to ensure good formatting, ALWAYS pass the commit message via a HEREDOC, a la this example:

```bash
git commit -m "$(cat <<'EOF'
fix(introspection): use PlainValidator instead of BeforeValidator for Django models

Use PlainValidator instead of BeforeValidator to avoid Pydantic warning
about validators returning non-self values. PlainValidator is designed
for custom validation logic that transforms input values.

This fixes the Pydantic 2.8 warning:

   A custom validator is returning a value other than self. Returning
   anything other than self from a top level model validator isn't
   supported when validating via __init__.

The warning was triggered because Pydantic was treating BeforeValidator
on Django model annotations as a model-level validator rather than a
field-level validator. PlainValidator correctly indicates we're doing
complete custom validation/transformation (PK -> Model instance).

EOF
)"
```

## Procedure

Run the following steps.  You can run some of the steps in parallel.

1. Check `git status`, `git diff`, `git diff --cached` to understand the current changes.
2. If changes span multiple logical units, plan a sequence of semantic commits and present the plan to the user for approval.
3. For each commit determine the kind and module prefix.
5. Stage the appropriate files and commit.

Git Safety Protocol:

- NEVER update the git config
- NEVER run destructive git commands (`push --force`, `reset --hard`, `checkout .`, `restore .`, `clean -f`, `branch -D`) unless the user explicitly requests these actions. Taking unauthorized destructive actions is unhelpful and can result in lost work, so it's best to ONLY run these commands when given direct instructions
- NEVER skip hooks (`--no-verify`, `--no-gpg-sign`, etc) unless the user explicitly requests it
- NEVER run force push to staging, warn the user if they request it
- CRITICAL: Always create NEW commits rather than amending, unless the user explicitly requests a git amend. When a pre-commit hook fails, the commit did NOT happen — so --amend would modify the PREVIOUS commit, which may result in destroying work or losing previous changes. Instead, after hook failure, fix the issue, re-stage, and create a NEW commit
- When staging files, prefer adding specific files by name rather than using `git add -A` or `git add .`, which can accidentally include sensitive files (`.env`, credentials) or large binaries
- NEVER commit changes unless the user explicitly asks you to (by invoking this skill). It is VERY IMPORTANT to only commit when explicitly asked, otherwise the user will feel that you are being too proactive

## Semantic Splitting

When the diff contains logically separate changes, split into multiple commits ordered from general to specific.  Each commit should compile and pass linting on its own.  Present a summary of planned commits before executing.

## Arguments

Treat caller-provided arguments as additional guidance:

- `amend`: Amend the previous commit.  Use `git log` to check the existing message and update it if needed.
- `all`: Use `git add` to include all modified tracked files.
- File paths or globs: Only stage and commit those files.
- Freeform text: Use as context for the commit message.
