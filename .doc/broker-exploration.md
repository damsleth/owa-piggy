# owa-piggy: token-acquisition exploration

Findings from a session spent chasing why `owa-piggy` stopped rotating, and an
architecture survey for how to get rid of the rotation entirely.

## 1. Why the cron stopped rotating

`owa-piggy` exchanges a SPA-scoped refresh token with a **24h fixed lifetime**.
The RT rotates on every use; running the tool at least once per 24h keeps it
alive indefinitely. Missing that window = dead token, reseed required.

The original `setup-cron.sh` installed an hourly `cron` entry, which on macOS
**does not fire missed jobs when the Mac was asleep**. An overnight-closed
laptop silently skipped the 18:00 and 19:00 Apr 16 runs; the token died
before 20:00 could fire. AADSTS700084 in the log was the symptom.

**Fix applied**: rewrote `setup-cron.sh` -> `setup-refresh.sh` to install a
**`launchd` LaunchAgent** (`com.damsleth.owa-piggy`) with
`StartCalendarInterval`, which **does** fire missed runs on wake. Hourly, same
cadence, but resilient to sleep.

Secondary hardening applied along the way:

- **Atomic `save_config`**: temp file + `fsync` + `os.replace` so a crash mid-write
  can never corrupt the only live RT.
- **Env-only semantics preserved**: if `OWA_REFRESH_TOKEN` came from env and
  not from the config file, the rotated RT is no longer silently persisted to
  disk. Instead it prints a stderr notice.
- **Hidden input for the secret prompt** (`read_input(..., secret=True)`),
  with `getpass` fallback.
- **Clean-checkout support in `setup-refresh.sh`**: `PROGRAM_ARGS` falls back
  to `python3 <repo>/owa_piggy.py` when `owa-piggy` isn't on PATH.
- **Bracketed-paste corruption fix**: terminals wrap pasted text with
  `ESC [200~ ... ESC [201~`; in raw-tty mode these leaked into the token and
  made AAD reject it as malformed. `read_input` now detects and drops these,
  strips stray CSI escapes, and the setup flow accepts piped stdin as well
  (`pbpaste | owa-piggy --save-config`).
- **Token-shape preflight**: before calling AAD, assert the RT starts with
  `0.` or `1.` (real FOCI prefix). Fail fast with an actionable message
  instead of the cryptic AADSTS9002313.

## 2. The Vivaldi vs. Edge revelation

Both Vivaldi and Edge are Chromium. Both run OWA fine. But:

| Browser | `|refreshtoken|` cache fields | `data`/`secret` head |
|---|---|---|
| **Edge** | `credentialType, homeAccountId, environment, clientId, secret, lastUpdatedAt, expiresOn` | `1.AQwAca1Hj8...` (real FOCI RT) |
| **Vivaldi** | `id, nonce, data, lastUpdatedAt` | `L68r5nsceHf...` (opaque, not an AAD RT) |

Edge integrates with Microsoft's native SSO broker and stores a real FOCI
refresh token. Plain Chromium (Vivaldi, Brave, Chrome) falls back to a
lighter, session-bound token that AAD rejects as malformed. That also
explains why OWA logs Vivaldi out more often - its session token has a
shorter fuse. **`owa-piggy` must be seeded from Edge.** Documented in
README + setup output + help text.

## 3. The Microsoft SSO Extension on macOS

Not a browser extension. An **Apple Extensible Enterprise SSO extension**
(`ASAuthorizationProviderExtension`, macOS 10.15+), shipped as part of
the Intune Company Portal app:

```
/Applications/Company Portal.app/Contents/PlugIns/Mac SSO Extension.appex
```

Activated by an MDM-deployed `com.apple.extensiblesso` configuration profile
that registers Microsoft identity URLs with the extension. Once active, apps
that opt into the broker protocol can silently acquire MSAL tokens against
any FOCI audience, backed by a device-bound PRT in the Secure Enclave.

### What's deployed on this Mac

From `app-sso platform -s`:

- Platform SSO fully registered (`registrationCompleted: true`).
- PRT bound to Secure Enclave (`loginType: POLoginTypeUserSecureEnclaveKey`).
- Broker-issued SSO tokens valid until **2026-05-01**.
- Tenant: `8f47ad71-44ca-48bf-afe3-56b9360a4495`.
- Device registered via `MS-Organization-Access` cert.
- Broker client ID: `29d9ed98-a469-4536-ade2-f981bc1d605e` (well-known
  Microsoft Authentication Broker).
- Kerberos cloud TGT also active (`tgt_cloud` for `KERBEROS.MICROSOFTONLINE.COM`).

### The gate that blocks a DIY broker client

From `sudo profiles show` on the `com.apple.extensiblesso.551981e4-...` payload:

```
AppPrefixAllowList            = "com.microsoft.,com.apple.,org.mozilla.firefox."
TeamIdentifier                = UBF8T346G9    # Microsoft's Apple team
Type                          = Redirect
URLs                          = login.microsoftonline.com,
                                 login.microsoft.com,
                                 sts.windows.net
browser_sso_interaction_enabled = 1
disable_explicit_app_prompt    = 1
```

To call the broker, a binary must have a bundle ID starting with
`com.microsoft.` / `com.apple.` / `org.mozilla.firefox.` **and** be signed
with Microsoft's team ID `UBF8T346G9`. The first is squatting; the second
is impossible without Microsoft's signing key. A third-party helper cannot
be on the allowlist without an IT change to the profile.

## 4. Options surveyed

### A. Signed Swift helper using `ASAuthorizationSingleSignOnProvider` / MSAL-ObjC

The architecturally clean endgame: a ~80-line Swift CLI wrapping MSAL-ObjC,
which knows how to invoke the broker over XPC. No RT on disk, no cron,
no browser; each run pulls a fresh access token via the device PRT.

**Blocked by the `AppPrefixAllowList` above.** Requires Crayon IT to add
`no.damsleth.` (or similar) to the `com.apple.extensiblesso` payload. Not
viable right now due to pre-merger IT freeze.

### B. Headless Edge re-seed automation

Since `browser_sso_interaction_enabled = 1`, Edge silently brokers and ends
up with a fresh FOCI RT in its localStorage. Drive Edge headlessly via
Chrome DevTools Protocol, evaluate the same `find()/parse()` snippet via
`Runtime.evaluate`, and pipe the two values into `owa-piggy --save-config`.

Patterns:

- **Copy-on-use**: `rsync -a` the Edge user-data-dir to `/tmp`, point
  `--user-data-dir` at the copy so it doesn't collide with the live Edge.
  Clean but expensive (~500MB-1GB per run).
- **Sidecar profile**: create a dedicated Edge profile at
  `~/.config/owa-piggy/edge-profile`, log into OWA there once, and reuse
  that profile for every headless reseed. Tiny (~5-50MB), isolated, no
  collision with the live Edge. Preferred.

Required flags:

```sh
--user-data-dir="..."           # root of the user-data-dir (not a profile subfolder)
--profile-directory="Profile 3" # specific profile inside that root
--headless=new --remote-debugging-port=9222
```

Profile-to-account mapping lives in `<user-data-dir>/Local State` under
`profile.info_cache`. Extract with:

```sh
jq -r '.profile.info_cache | to_entries[] |
  "\(.key)\t\(.value.user_name // "-")\t\(.value.name // "-")"' \
  "$HOME/Library/Application Support/Microsoft Edge/Local State"
```

This route still has moving parts (Edge binary, CDP) but eliminates
manual console paste and is fully scriptable.

### C. MSAL Python + `msal-extensions` keychain cache

Doesn't use the SSO extension (so bypasses the allowlist gate entirely),
but gets most of the "no moving parts" feel:

- One interactive login (browser opens once).
- MSAL persists the RT encrypted in the login keychain, ACL-scoped to the
  script.
- Every call uses the cached RT; MSAL rotates silently. No cron needed.

Trade-off: still a refresh-token on disk (in keychain), still a browser-
based initial login, not broker-backed.

### D. Keychain reads of existing MSAL caches

Dead end. Items tagged `com.microsoft.CompanyPortalMac.ssoextension` are
ACL-gated to the extension's signing identity. `security -g` from a user
shell can't read them. `com.microsoft.adalcache` item exists but holds a
metadata stub; the actual RT is encrypted elsewhere or held in memory by
the extension. Design intent - the PRT never leaves the broker.

### E. `app-sso` CLI

Earlier speculation: dead end. `app-sso` only covers **Kerberos** realms
and **Platform SSO** diagnostics (`platform -s`, `platform -m`). There is
no general "invoke arbitrary extensible-SSO extension" subcommand. `-i` is
`--realminfo` for Kerberos, not what we wanted.

## 5. Current status

- `setup-refresh.sh` installs the LaunchAgent and catches up on wake.
- Config file writes are atomic.
- Setup accepts piped stdin (`pbpaste | owa-piggy --save-config`) to
  sidestep bracketed-paste corruption.
- Token-shape preflight rejects non-FOCI tokens with a clear message.
- README + help text call out that the seed must come from Edge.

The 24h rotation window is comfortable under launchd. Reseed is only
required if AAD invalidates the family (password change, admin revoke),
which with a healthy PRT is rare.

## 6. Recommended path forward

1. **Short-term**: stay on option C-equivalent (current design + LaunchAgent).
   It's the correct answer under current constraints.
2. **Medium-term, if the Edge seed step ever becomes a recurring annoyance**:
   implement option B with the sidecar-profile pattern. Fully automates
   the reseed without IT involvement.
3. **Post-merger / when IT freeze thaws**: file the allowlist ticket for
   option A and migrate to a signed Swift broker CLI. Everything else
   becomes legacy.

## 7. Useful diagnostics (keep around)

```sh
# Platform SSO + broker state
app-sso platform -s

# Who is allowed to call the extension, against which URLs
sudo profiles show -output stdout 2>/dev/null | \
  awk '/com.apple.extensiblesso/,/<\/dict>/'

# Is Company Portal's SSO extension running?
ps -Ao pid,comm | grep -i 'Mac SSO Extension'

# Which Edge profile holds the Crayon account?
jq -r '.profile.info_cache | to_entries[] |
  "\(.key)\t\(.value.user_name // "-")"' \
  "$HOME/Library/Application Support/Microsoft Edge/Local State"

# LaunchAgent health
launchctl list | grep owa-piggy
tail -n 5 ~/.config/owa-piggy/refresh.log
```
