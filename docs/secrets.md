# Secrets and API keys

`TWILIO_PUBLIC_MEDIA_BASE_URL` is optional and is not a secret, but it is trusted
configuration: set it to the externally reachable HTTPS origin to enable outbound
MMS media. Existing SMS-only configurations may omit it. Never use
an inbound `Host` or forwarding header to construct it. Outbound media paths are
temporary bearer credentials and must be treated as sensitive.

embodied-runtime uses environment variables as its process interface for
secrets. In particular, OpenAI cognition expects `OPENAI_API_KEY`. An
environment variable delivers a value to a process; it is not persistent
secret storage and does not encrypt or otherwise protect the value by itself.

For current Mira and Raspberry Pi development, store the OpenAI environment
setting outside the Git checkout at:

```text
~/.config/embodied-runtime/openai.env
```

## Set up local storage and delivery

Create a private configuration directory and edit the secret file:

```sh
mkdir -p ~/.config/embodied-runtime
chmod 700 ~/.config/embodied-runtime
vi ~/.config/embodied-runtime/openai.env
```

Insert the real credential manually in the editor, replacing the placeholder:

```sh
export OPENAI_API_KEY='YOUR_KEY_HERE'
```

Protect the file, then source it to deliver the value to the current shell and
processes launched from that shell:

```sh
chmod 600 ~/.config/embodied-runtime/openai.env
source ~/.config/embodied-runtime/openai.env
```

Launch the runtime normally:

```sh
python main.py --cognition openai-responses --console
```

For initiative testing:

```sh
python main.py \
  --camera picamera2 \
  --cognition openai-responses \
  --initiative \
  --console
```

Sourcing affects only the current shell and its subsequently launched child
processes. Source the file again in a new shell when OpenAI cognition is needed.

## Verify without displaying the secret

Check only whether the variable is nonempty. Do not print its value:

```sh
if [ -n "$OPENAI_API_KEY" ]; then
    echo "OPENAI_API_KEY is set"
else
    echo "OPENAI_API_KEY is not set"
fi
```

## Practical security rules

- Never commit API keys to Git.
- Never place a real key in repository documentation, examples, tests, source,
  TOML configuration, or profiles.
- Do not store the key anywhere in the embodied-runtime repository.
- Do not log the key or include it in command-line arguments.
- Avoid putting the literal key directly in a shell command, where it may enter
  shell history. Prefer editing the protected secret file with the user's
  editor.
- Keep `~/.config/embodied-runtime` at permission `700` and `openai.env` at
  permission `600`.
- Source the file only into shells and processes that need the credential.
- A process that legitimately receives a secret can potentially expose it if
  its account or process is compromised. Local file permissions are useful
  protection, not magic encryption.

## Why not `~/.bashrc`?

Putting an `export OPENAI_API_KEY=...` setting directly in `~/.bashrc` works,
but it is less desirable for this project. Every interactive shell then
inherits the credential, while `.bashrc` is general-purpose configuration that
is more commonly copied, inspected, or shared during troubleshooting. A
dedicated protected file makes its ownership and purpose clearer and lets the
operator source it only when needed. This does not mean `.bashrc` is inherently
insecure; it is simply not the preferred embodied-runtime procedure.

## Why not a repository `.env` file?

The recommended storage location is outside the repository. Even if `.env` is
ignored by Git, keeping secrets out of the checkout reduces the chance of an
accidental commit or of copying the secret with the project, a patch, an
archive, or a troubleshooting bundle. embodied-runtime does not require a
`.env` loader: the shell supplies the existing `OPENAI_API_KEY` interface.

## OpenAI project and key containment

Where practical, use a dedicated OpenAI project and API key for Mira rather
than reusing an unrelated, broad development credential. Apply least privilege
or restricted permissions where the provider supports them, and configure
sensible project usage or budget controls and alerts. Rotate and revoke the key
if exposure is suspected.

## Rotating the key

1. Create or obtain a replacement key through the provider.
2. Edit `~/.config/embodied-runtime/openai.env` and replace the old value
   without printing either credential.
3. Restore the required file permission:

   ```sh
   chmod 600 ~/.config/embodied-runtime/openai.env
   ```

4. Reload it into the current shell:

   ```sh
   source ~/.config/embodied-runtime/openai.env
   ```

5. Restart every running embodied-runtime process that inherited the previous
   environment.
6. After confirming the replacement works, revoke the old key.

To remove the credential from the current shell when it is no longer needed:

```sh
unset OPENAI_API_KEY
```

## Quick setup on a new Mira/Pi

Run these commands as the user who will launch embodied-runtime:

```sh
mkdir -p ~/.config/embodied-runtime
chmod 700 ~/.config/embodied-runtime
vi ~/.config/embodied-runtime/openai.env
chmod 600 ~/.config/embodied-runtime/openai.env
source ~/.config/embodied-runtime/openai.env
```

In the editor, manually add `export OPENAI_API_KEY='YOUR_KEY_HERE'` with the
real credential substituted locally. Never put that real value in project
documentation. Verify without displaying it:

```sh
if [ -n "$OPENAI_API_KEY" ]; then
    echo "OPENAI_API_KEY is set"
else
    echo "OPENAI_API_KEY is not set"
fi
```

Then launch:

```sh
python main.py --cognition openai-responses --console
```

## systemd service delivery

The Startup Wizard can explicitly capture only the provider variables required
by the effective launch into a protected, service-specific file such as
`/etc/embodied-runtime/mira.env`. The generated unit references that file with
`EnvironmentFile=`. Capture is opt-in (`--capture-env`), the file is mode 0600,
and the wizard never prints or passes secret values on a command line. Routine
service uninstall preserves it; deletion requires the separate `--purge-env`
choice. See [Startup Wizard and systemd](startup.md).

The application's stable interface remains environment variables. systemd
credentials could provide later hardening without changing that interface.

## ngrok ingress credential separation

An SMS deployment using Startup Wizard ngrok ingress requires
`NGROK_AUTHTOKEN`. Load it into the shell before an explicit `--capture-env`
install. The wizard reports only whether it is set and writes only that variable
to `/etc/embodied-runtime/<name>-ngrok.env` at mode 0600. The ngrok unit does
not receive OpenAI, ElevenLabs, or Twilio credentials, and the runtime unit does
not receive the ngrok token. The generated v3 YAML contains only endpoint,
domain, and upstream configuration—no token value or environment-variable
reference. `mira-ngrok.service` loads the dedicated file with
`EnvironmentFile=/etc/embodied-runtime/mira-ngrok.env`, making the token
available only in the ngrok process environment. This contract was verified
with ngrok v3.39.11 on the target Raspberry Pi.

The public `TWILIO_WEBHOOK_URL` is derived from the validated stable domain and
effective SMS webhook path when managed ngrok ingress is captured. Other Twilio
values remain operator supplied. Reused runtime and ngrok environment files
must each be regular, non-symlink files protected from group/world read and
write. Uninstall preserves both by default; use `--purge-env` only for deliberate
deletion. systemd credentials and encrypted-at-rest provisioning remain
possible later hardening improvements; this phase does not add provider-specific
key-file APIs.

## Twilio SMS

SMS uses the same operator-owned, sourced-file pattern; the runtime does not load
`.env` files. Create `~/.config/embodied-runtime/twilio.env` containing placeholders
replaced only on the target host:

```sh
export TWILIO_ACCOUNT_SID='AC...'
export TWILIO_AUTH_TOKEN='...'
export TWILIO_PHONE_NUMBER='+1...'
export MIRA_SMS_OPERATOR_NUMBER='+1...'
export TWILIO_WEBHOOK_URL='https://example.ngrok-free.app/sms'
export TWILIO_PUBLIC_MEDIA_BASE_URL='https://example.ngrok-free.app'
```

```sh
mkdir -p ~/.config/embodied-runtime
chmod 700 ~/.config/embodied-runtime
vi ~/.config/embodied-runtime/twilio.env
chmod 600 ~/.config/embodied-runtime/twilio.env
source ~/.config/embodied-runtime/twilio.env
```

Verify presence without printing values:

```sh
for name in TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN TWILIO_PHONE_NUMBER \
    MIRA_SMS_OPERATOR_NUMBER TWILIO_WEBHOOK_URL TWILIO_PUBLIC_MEDIA_BASE_URL
do
    if printenv "$name" >/dev/null; then
        echo "$name is set"
    else
        echo "$name is not set"
    fi
done
```
