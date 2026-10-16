# Jev Ultrafast

Read README.md before editing. Keep the loop small: page -> indexed elements -> operation + target -> execution.

- The input is one natural-language goal. Do not add site-specific plans or hardcoded field values.
- TypeSafe chooses an operation and operation-specific target heads in one request. Consume only the selected operation's target.
- Targets must map to observed elements and supported operations. Never let the model emit selectors or executable code.
- TYPE_TEXT invokes the text LLM. Cache a stale retry's value only while its entire helper input is identical.
- Never retry a browser mutation. Log execution before observing its result.
- Screenshots are optional; the model does not consume them. Keep demonstration footage at its original speed.
- Keep credentials server-side and .env ignored. Tests must not call paid APIs.
- Verify actual final outcomes independently. A DONE choice is not proof of success.
- Keep examples, README claims, raw evidence, and model-call counts consistent.
- Do not commit or push unless the user requests it.

Recipes (usecases/recipes/*.json) are a trace cache, not a plan:

- Never hand-author or hand-edit one. Every field is derived mechanically from an
  observation Jev acted on; `usecases/run.py record` is the only way to make one.
- Regenerability is the invariant. Each recipe carries the verbatim goal, timestamp,
  model and source hashes, and must be reproducible by re-running the agent on that goal.
  The natural-language goal stays the source of truth; the recipe sits beside it.
- Never persist element identity (node, action id, rect, fingerprint, marker, guards) or
  a typed value. A step records only the param a value comes from.
- Replay makes zero model calls. Report agent timings and replay timings separately;
  never quote a replay number as the agent's.
- `match` is a fixed schema read by code. No regex, no expression language, no
  model-authored string - otherwise the model emits selectors through the back door.
- Repair is a re-record from the original goal, never a splice, and only after a failure
  that dispatched no input. A verifier the recipe cannot rewrite stays the authority.

Checks: uv run ruff check ., uv run pytest, node --check jev_ultrafast/static/app.js, uv build.
