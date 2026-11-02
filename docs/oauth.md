# OAuth setup for maintainers

Browser sign-in uses the project's GitHub OAuth App. Its **public Client ID**
is set as `OAUTH_CLIENT_ID` in `custom_components/github_copilot/const.py`.
The device flow uses no client secret or callback URL, so no secret is shipped.

The OAuth App must keep these settings:

- **Device flow** enabled.
- **Expiring user tokens** enabled. GitHub then issues 8-hour access tokens and
  6-month refresh tokens. The integration also requests the `offline_access`
  scope, which asks for expiring tokens even if the app setting is off.
  It requests no repository access.

The integration stores both tokens in the config entry, renews the access
token when roughly an hour remains (as required by the SDK), and saves the new
pair each time without reloading the integration or discarding active chats.
Only options changes reload automatically; completing reauthentication explicitly
reloads once. If GitHub rejects the refresh, for example because the refresh
token expired or the user revoked the app, Home Assistant starts reauthentication.
OAuth responses without a token lifetime are rejected. The `offline_access`
scope requests expiring tokens independently of the app's default setting.

The OAuth dialog polls automatically and cancels polling when closed. GitHub's
immutable account ID is saved at setup and checked during reauthentication.
Existing entries bind to their current account at startup; if old credentials
are already invalid, account identity cannot be safely inferred and the user
must explicitly remove/re-add the entry to choose an account. The same account
can still be used by multiple separately configured conversation agents.

The fallback is a user-owned fine-grained PAT with the **Copilot Requests**
account permission, supplied through the SDK's static-token option. The
integration does not renew PATs or invent an expiry time for them. Classic PATs
are not supported. Never put access tokens in source files, issue reports, or
chat messages.
