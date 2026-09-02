# Secrets and API keys

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

## Future service deployment

When embodied-runtime eventually runs as a persistent systemd service, prefer
systemd's credential facilities or another operating-system/service secret
mechanism over broadly exporting secrets through login-shell configuration.
systemd credentials can scope delivery to the service, and encrypted-at-rest
credentials may eventually be appropriate.

This is future guidance, not a service implementation. The application can
continue treating `OPENAI_API_KEY` as its stable public configuration interface
unless a concrete requirement later justifies an API-key-file interface. The
storage and controlled delivery mechanism can improve independently of that
application-facing interface.
