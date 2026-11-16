# Security policy

## Reporting a vulnerability

Use GitHub's **Report a vulnerability** private security-advisory form for this repository
when it is available. If private reporting is unavailable, open a public issue containing
only a request for a private reporting channel. Do not include exploit details, tokens,
recordings, node data, or other sensitive material in that issue.

No response-time or release-time guarantee is currently published.

## Operational boundary

The Freeze agent provides remote control of a node. Bind it to localhost, require its
token, and use an SSH tunnel for access from another machine. Never expose the agent or
broker port directly to an untrusted network. Python scripts passed to `freeze-debugger
script` are trusted local code and are not sandboxed.

Use controlled devnets for altered traffic. The project intentionally refuses unsupported
or unsafe edit and re-signing paths; changes must preserve those fail-closed boundaries.