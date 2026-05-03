# Phase 15A: Voice Design Session

## Purpose

Voice Design Session lets a user describe a chip block in short voice notes, review the transcript, generate a structured spec draft, then run the normal ChipLoop workflow.

This is the new recommended path:

```text
voice note -> Whisper transcription -> LLM spec draft -> user review/edit -> workflow run
```

Notion is no longer required for the core voice flow.

## Backend Endpoints

- `POST /studio/voice/transcribe`
  - Browser session auth required.
  - Trial/paid checkout required.
  - Accepts multipart `file`.
  - Uses Whisper through OpenAI audio transcription.
  - Returns `{ transcript }`.

- `POST /studio/voice/spec-draft`
  - Browser session auth required.
  - Trial/paid checkout required.
  - Accepts `{ transcripts, loop_type, target }`.
  - Uses the existing LLM fallback chain to create a structured spec draft.
  - Returns `{ spec_draft }`.

## Frontend Integration

Spec-driven Apps now use a shared `VoiceSpecDraft` component next to their spec box:

1. Record one or more voice notes.
2. Review transcript cards.
3. Click `Generate Spec`.
4. Edit the generated spec in the normal app spec box if needed.
5. Run the App normally.

System Apps that need multiple specs expose separate voice flows:

- Digital spec voice draft.
- Analog spec voice draft.
- SoC integration spec voice draft.

This keeps System RTL, System Simulation, System PD, System Firmware, and System End-to-End aligned with the three-spec workflow.

## Legacy Notion/Voice Status

Older endpoints such as `/voice_stream`, `/summarize_notion`, `/voice_to_spec`, and `/spec_live_feedback` still exist for compatibility, but they should be treated as legacy/prototype paths.

Recommended future cleanup:

- Move legacy voice endpoints behind `/legacy` or remove after Studio no longer references them.
- Add optional Notion import/export as a separate integration.
- Do not require Notion for first-run voice workflows.

## Safety

- No frontend calls to `/sdk/*`.
- No secrets in frontend.
- Voice features require an authenticated browser session.
- Voice features require checkout/trial because transcription and drafting use paid backend resources.
