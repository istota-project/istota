# Audio Transcription with Whisper

Transcribe audio files locally using faster-whisper (CPU, int8 quantization). Supports all common audio formats (wav, mp3, m4a, flac, ogg, webm).

## Usage

```bash
# Basic transcription (auto-selects model based on available RAM)
python -m istota.skills.whisper transcribe /path/to/audio.wav

# Specify model and language
python -m istota.skills.whisper transcribe /path/to/audio.wav --model small --language en

# Output as SRT subtitles and save to file
python -m istota.skills.whisper transcribe /path/to/audio.wav --output srt --save

# Output as WebVTT
python -m istota.skills.whisper transcribe /path/to/audio.wav --output vtt

# Plain text output
python -m istota.skills.whisper transcribe /path/to/audio.wav --output text

# List available models and RAM requirements
python -m istota.skills.whisper models

# Pre-download a model
python -m istota.skills.whisper download small
```

## Output (JSON format)

```json
{
  "status": "ok",
  "model": "small",
  "language": "en",
  "language_probability": 0.9876,
  "duration_seconds": 45.2,
  "processing_seconds": 12.3,
  "text": "Full transcription text...",
  "segments": [
    {
      "start": 0.0,
      "end": 3.5,
      "text": "Hello, this is a test.",
      "words": [
        {"start": 0.0, "end": 0.5, "word": "Hello,", "probability": 0.98},
        {"start": 0.6, "end": 0.9, "word": "this", "probability": 0.99}
      ]
    }
  ]
}
```

## Models

| Model | RAM (~GB) | Speed | Quality |
|-------|-----------|-------|---------|
| tiny | 1.0 | Fastest | Basic |
| base | 1.5 | Fast | Good for clear audio |
| small | 2.5 | Moderate | Good general-purpose |
| medium | 5.0 | Slow | High quality |
| large-v3 | 10.0 | Slowest | Best quality |

With `--model auto` (default), the largest model that fits in available RAM is selected.

## Guidelines

- **Short recordings** (< 5 min): Use default model auto-selection.
- **Long recordings** (> 10 min): Warn the user this may take a while. Processing is ~0.5-2x real-time depending on model and CPU.
- **Language**: Auto-detected by default. Specify `--language` if detection is unreliable or you know the language.
- **Use `--save`** when the user wants to keep the transcription as a file.
- **SRT/VTT** formats are useful when the user wants subtitles or time-aligned text.
- **Voice memos**: Commonly found in `/Users/{user_id}/inbox/` or shared files.
- Models must be pre-downloaded before use inside sandboxed environments.

## When to Use

- Voice memos and audio recordings shared by the user
- Dictation or speech-to-text requests
- Meeting recordings or interview transcripts
- Audio files in the user's inbox or shared folders
- Any audio-to-text conversion task
