# Change Review

Reviewed all changes since `d2144e9` (`first commit`), including the modified README and untracked project configuration, planning, and plugin files.

## Findings

### P1 — Quick Start cannot be followed from this revision

`README.md:19-20` directs users to copy `.env.example` and run scripts under `scripts/`, but neither `.env.example` nor the `scripts/` directory exists. The only `.env` in the working tree is empty. A new user therefore fails at the first command and cannot start the application using the documented path. Either add the promised template and scripts in this change, or clearly mark the commands as planned/unavailable until the implementation lands.

### P1 — API credentials can be accidentally committed

`planning/PLAN.md:105` says `.env` is gitignored and `.env.example` is committed, but the repository has no `.gitignore` and `.env` is currently an untracked file. Following the README's instruction to add `OPENROUTER_API_KEY` leaves that secret visible to `git status` and vulnerable to a routine `git add .`. Add a `.gitignore` that excludes `.env` (and runtime SQLite data), add a redacted `.env.example`, and remove any real values from the untracked `.env` before committing.

### P1 — The frontend skill targets a different product and will misdirect implementation and verification

`.claude/skills/run-frontend/SKILL.md:3-9` identifies the project as a "Mutual NDA Creator" with no backend, contradicting FinAlly's FastAPI/SSE architecture. Its prescribed interaction tests also require NDA form fields and PDF download behavior (`:41-53`), none of which belong to this project. Because agents are instructed to use this skill for frontend work, it will cause them to build or validate the wrong behavior. Replace it with FinAlly-specific startup, test, and browser-verification instructions, or omit it until the frontend exists.

## Notes

No executable application code or test suite exists yet, so runtime verification was not possible. The remaining findings are documentation/configuration issues in the newly added planning-stage files.
