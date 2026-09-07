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
2. Local SunFounder Vosk STT listens for one utterance (18 seconds by default).
3. Recognized text follows the same application cognition path as console `ask`.
4. Local SunFounder Espeak TTS speaks only the final text response.
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
initial_timeout_seconds = 18
followup_timeout_seconds = 10
```

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
