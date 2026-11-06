# Security policy

CodePilot-Repair is an engineering preview for a single user on a local machine or trusted network. It is not a multi-tenant service. See [the security model](docs/06-security.md) for credentials, workspace boundaries, network access and Docker socket privileges.

## Reporting a vulnerability

Do not post credentials, private repository contents, exploit payloads containing private data, or unredacted logs in public issues.
Email [jeq0731@gmail.com](mailto:jeq0731@gmail.com) to report a vulnerability privately. Include the affected revision, impact and a minimal reproduction with secrets removed. Do not send live credentials. You may also use the repository Security tab's **Report a vulnerability** feature if it is enabled.

Only the latest preview revision is maintained on a best-effort basis; there is no response-time or backport guarantee. Run untrusted repositories only in an isolated environment. Do not expose the API or Docker socket to the public internet.

If a credential has been exposed, revoke or rotate it with its provider before attempting repository cleanup.
