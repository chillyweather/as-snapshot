# Figma MCP configuration — never paste the token value

Issue #287: a (now revoked) Figma personal access token leaked into a
committed `workflow-log.json` because subprocess diagnostics and the
workflow-log sanitizer did not recognize the token's format. The code has
since been hardened (`autoscout/secret_redaction.py`, applied to Codex
subprocess stderr and to every workflow-log persistence point, plus a
fail-closed scan that blocks publication if a recognizable credential shape
is found in a generated artifact). This note covers the operator half: how
to configure Figma MCP access without ever putting the literal token
anywhere it could be captured, logged, or committed.

## Reference the environment variable NAME, not the token value

Any MCP server configuration, prompt, script, or doc that needs a Figma
token must reference the **name** of the environment variable holding it —
for example `FIGMA_TOKEN` — never the literal `figd_...` value.

- Good: `"FIGMA_TOKEN": "${FIGMA_TOKEN}"` in an MCP config, or a shell
  script that reads `os.environ["FIGMA_TOKEN"]`.
- Bad: `"FIGMA_TOKEN": "[REDACTED:figma_pat]..."` written into any config file,
  prompt, chat message, issue, commit, or log.

Store the actual token value only in a secret manager, your shell's
environment, or a local `.env` file that is git-ignored and never
committed. Rotate a token immediately if it is ever pasted into a chat,
prompt, config file, or committed artifact — assume it is compromised the
moment it appears in plaintext outside the secret store, even if you delete
it afterward, since git history and chat transcripts can retain it.

## Diagnostics are redacted, but that is a backstop, not a license

`autoscout/secret_redaction.py` recognizes common credential shapes
(Figma PATs, GitHub tokens, OpenAI-style keys, AWS access key IDs, Slack
tokens, bearer tokens, and `key=`/`secret=`-style assignments) and redacts
them before Codex subprocess stderr is stored as a diagnostic, and again as
a second pass over everything written to `workflow-log.json`. The weekly
workflow also fail-closes: before publishing a run's canonical artifacts, it
scans them for recognizable credential shapes and blocks commit/push if any
are found.

This is defense in depth for accidents (a misconfigured tool that echoes a
token to stderr, for example) — it is **not** a reason to ever knowingly
paste a real token into a prompt, config, or file. Pattern-based redaction
cannot guarantee it recognizes every possible credential shape, and the
git-history rewrite required to purge the already-leaked token from past
commits is tracked separately and is out of scope for this note. The only
reliable protection is to never write the literal token value anywhere in
the first place.
