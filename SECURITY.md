# Security policy

## Reporting a vulnerability

Email **security@mapdex.ai** with what you found and how to reproduce it.
Please do not open a public issue for a vulnerability first. We aim to
acknowledge within two working days.

If you prefer GitHub, use *Security → Report a vulnerability* on this
repository; it stays private until we publish an advisory.

## What this plugin handles

- **An access token for your Mapdex account.** It is obtained through a
  browser authorization and stored as the password of an encrypted QGIS
  Authentication Manager configuration, never in plain QGIS settings. If the
  authentication database is unavailable the token stays in memory for the
  session instead of being written in the clear.
- **The files you explicitly send.** Nothing else is uploaded; the plugin does
  not scan your project.

Deliberate constraints, worth knowing if you audit the code:

- The token is only ever sent to the configured API origin. Result payloads
  carry absolute artifact URLs, and object storage uses pre-signed links that
  need no credential, so those requests carry neither the token nor the tenant
  header.
- Redirects that leave the API origin have the `Authorization` header removed.
- Plain `http://` endpoints are refused unless they are on this machine
  (`localhost`, `127.0.0.1`), because the token travels with every request.
- Changing the endpoint drops the stored session: a token belongs to the
  deployment that issued it.
- A published build is pinned to `https://api.mapdex.ai` and does not expose
  endpoint fields at all.
- Server-supplied identifiers are reduced to a safe alphabet before they are
  used as file names, and a URL returned by the API is only opened in a
  browser when it is plain `http(s)`.

## Supported versions

The latest release published on
[plugins.qgis.org](https://plugins.qgis.org/plugins/) receives fixes.
