# Managed ngrok edge policy

The Startup Wizard always attaches a Traffic Policy to managed ngrok SMS ingress.
Configuration is generated locally in the existing profile-scoped ngrok YAML;
ngrok executes the policy in its cloud **before forwarding to the Pi**. There is
no unprotected fallback or separate proxy. The runtime's Twilio SDK signature
validation against `TWILIO_WEBHOOK_URL`, exact account/destination/operator checks,
bounded inbox, sequential worker, deduplication, restricted media downloads,
image limits, and expiring outbound bearer URLs remain in place. Keep the default
loopback listener; the inbox bounds accepted work, not unauthenticated HTTP traffic.

## Policy and limits

| Traffic | Edge behavior |
| --- | --- |
| Exact configured `sms.webhook_path`, POST | 30 requests per 60 seconds, then enforced `verify-webhook` with provider `twilio` |
| `/outbound-media/<token>.jpg` or `.png`, GET/HEAD | Separate 60 requests per 60 seconds; no browser OAuth or webhook signature requirement |
| Other paths or methods | 403, without forwarding |
| Allowed routes with declared body size over 65,536 bytes | 413, without forwarding |

The media matcher is anchored and accepts only the application's URL-safe token
alphabet and accepted 40–64 character length. The application still checks token
existence and expiry. It currently generates 43-character random tokens. HEAD and
GET share the media bucket; JPEG and PNG share that bucket too. There is no
per-token bucket that an attacker could multiply by guessing different paths.

Both rate limits explicitly enforce `sliding_window`, with independent constant
bucket keys aggregating all source IPs. Media fetches cannot spend the SMS budget.
30/min leaves room for one operator and webhook retries while reducing bursts
of form parsing/signature checks on a Pi Zero 2 W. The SMS limiter precedes cloud
signature verification, so invalid requests consume this budget and can temporarily
deny valid deliveries; this trades availability during abuse for bounded ingress.
60/min accommodates four staged images, HEAD probes, GETs and repeated provider
fetches while limiting image-serving pressure. These are request limits, not byte
or cognition limits; a burst can still fill the application's 16-item inbox and
get 503. They do not guarantee successful retry delivery or prevent quota charges.

ngrok documents capacity **per ingress server**, not a single global ceiling.
It returns 429 with `Retry-After` when a bucket is exhausted. The supported minimum
rate window is 60 seconds, so no subsecond burst guarantee is claimed. See the
[rate-limit reference](https://ngrok.com/docs/gateway/traffic-policy/actions/rate-limit).

The body check uses documented `req.content_length`. 64 KiB allows generously
encoded SMS text and MMS metadata; inbound MMS carries media references, not
image bytes. This also permits future optional Twilio fields. A streamed request
without a declared length is not fully bounded by this rule. There is no documented
general streaming-body ceiling or in-flight request concurrency limiter in the
selected Traffic Policy actions; neither is invented here. Circuit breaking reacts
to upstream errors/volume and is not a concurrency limit. The existing application
parser/image limits remain necessary. See [request variables](https://ngrok.com/docs/gateway/traffic-policy/variables/req)
and [circuit breaker](https://ngrok.com/docs/gateway/traffic-policy/actions/circuit-breaker).

## Versions, plans and trust

Official documentation checked 2026-10-19:

- [v3 agent configuration](https://ngrok.com/docs/gateway/agent/config/v3)
  supports inline `endpoints[].traffic_policy`. [Agent releases](https://ngrok.com/docs/gateway/agent/changelog)
  introduced stable v3 endpoints in 3.16.0 and phase names in 3.17.0. The wizard
  requires **>=3.17.0 and <4**. Prefer current stable **3.39.11** (also the prior
  target-Pi authentication baseline). The manual secret setup below uses its CLI;
  `--value-file` arrived in 3.25.1 and `--vault-name` in 3.31.0.
- [Webhook verification](https://ngrok.com/docs/gateway/traffic-policy/actions/verify-webhook)
  supports [Twilio SMS](https://ngrok.com/docs/integrations/webhooks/twilio-webhooks)
  and CEL secret interpolation; explicit `enforce: true` rejects
  failed signatures with 403. Application validation remains independent. Twilio
  signatures do not supply timestamp-based replay protection; existing message
  deduplication semantics are unchanged.
- The policy has four rules, within the documented Free five-rule ceiling.
  [Free limits](https://ngrok.com/docs/pricing-limits/free-plan-limits) currently
  include 500 webhook verifications/month, 20,000 HTTP requests/month and 1 GB
  outbound/month. [Current pricing](https://ngrok.com/pricing) meters Traffic
  Policy through usage credit/TPUs. Free credit is finite; Hobbyist also stops
  endpoints when credit runs out. Pay-as-you-go supports additional billed usage.
  The assigned dev domain works on Free; selecting an ngrok-branded domain requires
  Hobbyist or above, and bringing your own domain requires Pay-as-you-go or above.
  Do not assume a legacy account has the same entitlements. Confirm rate-limit,
  verify-webhook and vault access on the actual account during acceptance. A plan
  rejection must be fixed in the account; never remove a control to make it start.
- [Vaults and secrets](https://ngrok.com/docs/gateway/traffic-policy/secrets)
  are available on Free (five vaults/five secrets total), Hobbyist and PayGo with
  larger secret quotas. Policies use `${secrets.get('vault-name', 'secret-name')}`.
  **ngrok must possess the actual Twilio account auth token to verify at its edge.**
  This is not an ngrok authtoken or Twilio API key. ngrok encrypts vault values at
  rest and resolves them in its cloud. Trusting ngrok with this token expands the
  credential trust boundary: the token also authorizes Twilio account API actions.
  Use the account/subaccount dedicated to this runtime where available and restrict
  ngrok account access. Updates to the cloud secret affect referencing policies.

The wizard does not upload, read, or compare cloud secrets and cannot prove their
value or entitlement locally. Local runtime credentials retain the existing
[protected environment-file workflow](secrets.md). The separate managed ngrok
environment contains only `NGROK_AUTHTOKEN`; no Twilio token is added to it. Names
go in CLI arguments and generated policy, never secret values.

The generated agent config disables its console UI and local web UI and uses
warning-level logs to avoid routine request-line logging. The policy adds no log
actions. In the dashboard's **Settings → Observability**, keep **Full Capture**
off. Cloud [Traffic Inspector](https://ngrok.com/docs/obs/traffic-inspection)
still retains request metadata by default; URLs can disclose media bearers even
without body capture. Restrict dashboard access and avoid exporting request events
or screenshots containing these URLs. Local UI settings do not disable cloud
retention. Use sanitized application method/status records and status-only test
results for acceptance, not captured payloads, signatures, or bearer paths.

## Operator setup and installation

No services or accounts are changed by implementation/tests. Perform these steps
on the deployment host when ready. Examples assume service/profile `mira`.

1. Install/select ngrok 3.39.11 or a newer supported stable v3 agent yourself.
   Confirm your assigned/reserved domain and the account limits above. Source the
   protected runtime provider files and ngrok authtoken file described in
   [startup](startup.md). Disable shell tracing; never paste tokens into commands.
   For one-time cloud setup, create an ngrok API key in the dashboard, store it
   outside Git in a mode-0600 file as `export NGROK_API_KEY='…'` using an editor,
   and source that file. This key is needed only for setup, not by either service.
2. Create a vault and secret once in that same ngrok account. Use non-secret names
   `robot-webhooks` and `twilio-auth-token` below, or substitute your own. Skip vault
   creation if it already exists. These are explicit account mutations performed
   by the operator. The protected temporary file below holds only the token value,
   without a trailing newline; do not use the shell `.env` file as `--value-file`.

   ```sh
   ngrok api vaults create --name robot-webhooks --log false
   python3 - <<'PY'
   import os
   import subprocess
   import tempfile
   with tempfile.NamedTemporaryFile(mode="w") as secret_file:
       os.chmod(secret_file.name, 0o600)
       secret_file.write(os.environ["TWILIO_AUTH_TOKEN"])
       secret_file.flush()
       result = subprocess.run([
           "ngrok", "api", "secrets", "create", "--vault-name", "robot-webhooks",
           "--name", "twilio-auth-token", "--value-file", secret_file.name,
           "--log", "false",
       ], capture_output=True)
   if result.returncode:
       raise SystemExit("Cloud secret creation failed; check API-key permissions, vault name and plan. Diagnostics withheld.")
   print("Cloud secret created; value not displayed.")
   PY
   unset NGROK_API_KEY
   ```

   The [CLI API reference](https://ngrok.com/docs/gateway/agent/cli-api#ngrok-api-secrets-create)
   documents these flags. For rotation use `secrets update <secret-id>` with
   `--value-file` by the same protected-file approach, then update/capture the
   runtime token too. Never use `--value "$TWILIO_AUTH_TOKEN"` or debug logging.
3. From the repository, render and check with your real hostname. The resource
   names are required for every managed-ngrok wizard invocation. The interactive
   wizard also prompts for them; it never prompts for the secret value.

   ```sh
   ingress_args=(--config config/mira-agentic.toml --with-sms --with-ngrok
     --ngrok-domain YOUR-ASSIGNED-HOST.ngrok-free.app
     --ngrok-twilio-vault robot-webhooks --ngrok-twilio-secret twilio-auth-token)
   .venv/bin/python scripts/startup_wizard.py render "${ingress_args[@]}" --component ngrok
   .venv/bin/python scripts/startup_wizard.py check "${ingress_args[@]}" --capture-env
   .venv/bin/python scripts/startup_wizard.py install "${ingress_args[@]}" --capture-env
   ```

   Set `[sms] webhook_path` in the selected TOML before these commands if needed;
   the policy derives that exact path. Managed paths must be static absolute URL
   paths using letters, digits, `_~./-`, without percent encoding, dot segments,
   repeated slashes, templates, queries, fragments or the outbound-media prefix.
   Runtime-only deployments keep their existing configuration behavior.

   `check` and `install` both run `ngrok config check` on a private temporary YAML
   before installing any artifacts. Parsing is attempted even when reusing an
   existing protected token file without loading its value. If your agent requires
   authentication for that command, source the protected ngrok authtoken and retry.
   Parser errors abort installation; raw stdout/stderr is withheld because it
   could contain inherited credentials. This parser does **not** establish cloud
   CEL compilation, secret resolution, plan authorization or enforcement.
4. Configure Twilio **A MESSAGE COMES IN → Webhook → POST** to the exact HTTPS URL
   printed by the wizard. It derives `TWILIO_WEBHOOK_URL` and
   `TWILIO_PUBLIC_MEDIA_BASE_URL` for the protected runtime environment. Review,
   then start the runtime and managed ingress explicitly, or restart only the
   active units reported by the wizard:

   ```sh
   sudo systemctl start mira.service
   sudo systemctl start mira-ngrok.service
   systemctl status mira-ngrok.service
   ```

   Cloud policy/feature/secret errors surface when starting the endpoint or
   processing the request; ngrok must remain unavailable/reject traffic in that
   case. Check the account's endpoint error and documented error code privately;
   fix the version, name, credential or entitlement, then repeat acceptance. Do
   not remove `traffic_policy`, disable enforcement, or start an ad-hoc tunnel.

## Updates and rollback

The same managed YAML, sibling unit and secure environment paths are used. No
global ngrok configuration changes. An existing different ngrok artifact requires
`install --force` after reviewing the render. `--force` does not bypass validation.
Policy-only changes need an explicit ingress restart, not runtime secret capture.
A changed domain/webhook path requires runtime capture and restarts of both active
services. A cloud secret value change under the same name is not locally detectable;
coordinate runtime-token rotation and repeat signature/SMS/MMS acceptance.

Before updating, save a private backup of the managed unit/YAML/state and, only if
rotating secrets, the environment files with their existing 0600 permissions. To
roll back, stop `mira-ngrok.service` first, restore the reviewed previous artifacts
and any coordinated cloud/runtime secret version, run local validation, reload
systemd if the unit changed, then restart and repeat acceptance. If the previous
configuration lacked a policy, keep ingress stopped until hardened configuration
passes acceptance; do not silently re-expose the unrestricted listener. Runtime
operation can continue independently. Existing `uninstall --with-ngrok --yes`
removes managed ingress artifacts and preserves secrets unless `--purge-env` is
explicit; it does not delete the cloud vault, secret, domain or account.

## Verification and live acceptance

Local regression tests cover generated policy structure, exact custom paths,
methods, token route shape, enforced verification, separate rate limits, resource
name validation, secret-free rendering/diagnostics, pre-install validation,
update/restart behavior and existing SMS/MMS authorization and delivery. Run:

```sh
.venv/bin/python -m unittest tests.test_ngrok_policy tests.test_startup tests.test_sms tests.test_multi_image_sms tests.test_outbound_images
```

Tests use fake command runners and local HTTP listeners, not a live ngrok account.
Do this short live acceptance on the actual plan after enabling the policy:

1. Prove edge rejection independently of app validation: with the SMS listener
   temporarily stopped and ngrok running, POST a small unsigned form to the exact
   webhook URL, then one with `X-Twilio-Signature: invalid`. Expect **403**, not an
   upstream connection failure. Start the runtime afterwards. Do this below the
   rate limit; a 429 means wait for `Retry-After`. Print only status codes, e.g.
   `curl -s -o /dev/null -w '%{http_code}\n' -X POST "$TWILIO_WEBHOOK_URL" --data 'Body=test'`.
2. Send a real operator SMS and an inbound MMS (including multiple images if used)
   through Twilio. Confirm exactly one expected response/analysis. Retry a delivery
   within the limit and confirm the existing MessageSid deduplication still works.
3. Send SMS/MMS from a different phone. Confirm the sanitized
   `ignored reason=external_participant` record and **no cognition or media download**
   for that message; no rejected/external body should be logged. Also retain the
   existing account-SID/destination authorization checks.
4. Ask the operator dialogue to send a fresh JPEG and a retained PNG. Confirm both
   arrive. During their 15-minute staging window, privately obtain a MediaUrl from
   the sent Twilio message record and fetch it with GET and HEAD: expect 200,
   correct image type/Content-Length and an empty HEAD body. A private script using
   `getpass.getpass` for the URL keeps it out of argv/history/output; print only
   method/status/type/byte counts and suppress exception URLs. Never use verbose
   curl or print a real bearer URL. Confirm invalid/expired tokens cannot fetch bytes.
5. GET/HEAD the webhook, POST/PUT/OPTIONS/DELETE a correctly shaped media path, and
   request `/`, `/sms-extra` or another unknown path. Expect **403 at the edge**.
   Use a fabricated 43-character token for these tests, never a real bearer. With
   a stopped listener these requests must still return 403. An oversized declared
   body on an allowed route should give 413.
6. In a quiet test window, issue 65 quick HEAD requests to
   `/outbound-media/` + 43 `a` characters + `.jpg`. These fabricated paths are not
   credentials; expect initial application 404s and then edge **429**, with
   `Retry-After`. Immediately send a valid operator SMS: media exhaustion must not
   consume its budget. Separately send 35 small unsigned webhook POSTs: expect
   403s then 429, with no application receipt; after the retry window confirm a
   real SMS succeeds. Routing across ingress servers can require additional
   bounded requests; do not claim an exact worldwide threshold. Record only
   counts/statuses/Retry-After and sanitized runtime method/status metadata.

Complete acceptance with Full Capture off. No token, signature, form payload,
operator number or media bearer URL belongs in an acceptance report. No live
edge, account or hardware acceptance is implied by the local checks.
