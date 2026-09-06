# Bounded voice conversation

Voice is a bounded interaction capability, not Mira's execution backbone. The
robot continues to exist, observe, pursue goals, and exercise bounded initiative
without an active microphone or model stream.

The first implementation is deliberately half-duplex and operator-triggered:

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
initial_timeout_seconds = 18
followup_timeout_seconds = 10
```

The Vosk model may be downloaded by the vendor library on the first `voice`
command. Initialization and download errors are reported for that session and do
not stop the runtime. Raw audio and partial recognition results remain transient;
they are never placed in RuntimeState, the EventBus, or persistent memory.

The onboard MEMS microphone has shown low native sensitivity in bench testing.
Approximately +30 dB post-capture gain helped a sample recording, but this path
intentionally preserves the vendor capture implementation. Recognition quality
and input-level tuning require live validation before adding preprocessing.

## Intentionally out of scope

This version does **not** implement a wake word, continuous listening or
transcription, a realtime LLM audio stream, full duplex or interruption,
barge-in, background recording, persistent audio storage, autonomous voice
session initiation, OpenAI realtime audio, or SunFounder's `VoiceAssistant`
orchestration.
