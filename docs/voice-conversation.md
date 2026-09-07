# Bounded voice conversation

Voice is a bounded interaction capability, not Mira's execution backbone. The
robot continues to exist, observe, pursue goals, and exercise bounded initiative
without an active microphone or model stream.

The implementation is deliberately half-duplex and enters the same bounded
conversation either from the console `voice` command or an optional local,
exact recognition of a configured local wake phrase. A wake-triggered session
first plays a fixed, local acknowledgement chirp (100 ms at 880 Hz, a 20 ms
pause, then 100 ms at 1,175 Hz) and only then begins turn-1 listening. The cue
is deterministic speaker output: it does not involve cognition, TTS, working
memory, RuntimeState, or the EventBus. Manual `voice` sessions do not play it.

1. Enter `voice` in the runtime console.
2. The Fusion HAT voice provider uses local SunFounder Vosk STT to listen for
   one utterance (18 seconds by default).
3. `VoiceInteraction` sends recognized text through the same application
   cognition path as console `ask`.
4. The final text response is passed unchanged to a separate
   `TextToSpeechProvider`; either `FusionHatEspeakTTSProvider` or
   `FusionHatPiperTTSProvider` speaks it locally.
5. Vosk offers one short follow-up opportunity (10 seconds by default).
6. The session closes after the second utterance or a timeout, stops capture, and
   disables the Fusion HAT speaker.

Physical voice is opt-in and is only constructed for the Fusion HAT backend. A
minimal configuration is:

```toml
[voice]
enabled = true
wake_word_enabled = true
wake_words = ["mira", "mirror"]
tts = "espeak"
initial_timeout_seconds = 18
followup_timeout_seconds = 10
```

The application constructs `FusionHatVoiceProvider` for local STT and wake
interaction and the selected physical speech provider as distinct dependencies.
eSpeak is the historical default. To use offline neural speech, install the
optional maintained OHF Piper package and an operator-managed voice:

```console
python -m pip install -e '.[openai,piper]'
mkdir -p ~/.local/share/embodied-runtime/piper
python -m piper.download_voices \
  --data-dir ~/.local/share/embodied-runtime/piper \
  en_US-lessac-medium
```

`python -m pip install -e '.[piper]'` is sufficient when OpenAI support is not
needed. Confirm that both `en_US-lessac-medium.onnx` and
`en_US-lessac-medium.onnx.json` were downloaded, then select it:

```toml
[voice]
enabled = true
wake_word_enabled = true
wake_words = ["mira", "mirror"]
tts = "piper"
piper_model = "/home/pi/.local/share/embodied-runtime/piper/en_US-lessac-medium.onnx"
initial_timeout_seconds = 18
followup_timeout_seconds = 10
```

`~` is expanded in model paths. Piper loads the local model on first speech,
keeps that one voice resident across turns and sessions, synthesizes a complete
WAV in memory, and plays it through the Fusion HAT speaker. Once the package and
model files are present, synthesis requires no network. Startup does not
download models, and there is no automatic fallback to eSpeak. Voice models can
carry their own dataset/model licensing terms; review the voice's model card
before selecting or distributing it. `en_US-lessac-medium` is only the first
benchmark voice, not a hard-coded runtime choice.

`VoiceInteraction` coordinates both providers and
keeps microphone capture and TTS playback half-duplex. It owns session-level
coordination while input cleanup remains with the voice provider and speaker
cleanup remains with the TTS provider; failure to close either one does not
skip the other cleanup attempt.

The narrow `TextToSpeechProvider` seam keeps selection from changing the bounded
conversation architecture. There is no provider probing, fallback, runtime
switching, streaming synthesis, or TTS text rewriting.

Wake listening is local trigger detection, not an always-running cloud
conversation. Matching is case-insensitive and exact after trimming against
the configured phrases. `"mirror"` is intentionally accepted because live Vosk
testing commonly returned it for the spoken name “Mira”. Ambient non-matches
remain local and never enter cognition or working memory. There is one
microphone owner: a manual or wake-triggered bounded
session cooperatively stops the wake capture, owns recognition through the wake
cue (when applicable), all STT, TTS, speaker disable, and cleanup, and only then
allows wake listening to resume. Cue playback finishes and disables the speaker
before turn-1 microphone capture starts. Failure to play the acknowledgement is
logged but does not prevent the bounded session. The manual console command
remains available while wake mode is on.

The Vosk model may be downloaded by the vendor library on the first listen
command. Initialization and download errors are reported for that session and do
not stop the runtime. Raw audio and partial recognition results remain transient;
they are never placed in RuntimeState, the EventBus, or persistent memory.

The onboard MEMS microphone has shown low native sensitivity in bench testing.
Approximately +30 dB post-capture gain helped a sample recording, but this path
intentionally preserves the vendor capture implementation. Recognition quality
and input-level tuning require live validation before adding preprocessing.

## Intentionally out of scope

This version does **not** implement wake-word-plus-command parsing, fuzzy wake
aliases, continuous cloud transcription, a realtime LLM audio stream, full
duplex or interruption, barge-in, background recording, persistent audio
storage, OpenAI realtime audio, or SunFounder's `VoiceAssistant` orchestration.

Successful third-party HTTP request INFO lines are intentionally hidden in
normal logging; first-party lifecycle INFO records and third-party warnings and
errors remain visible.
