# Transcribe - Client Conversation to Structured Requirements

Transcribe an audio or video recording of a client conversation and parse it into structured requirements ready for issue creation.

## Usage
```
/transcribe <file_path>
```

## Parameters
- `file_path`: Path to an audio file (.mp3, .wav, .m4a) or video file (.mp4, .mov, .webm)

## Workflow

### Step 1: Validate input

Check the file exists and determine if it's audio or video:

```bash
ls -la "$file_path"
ffmpeg -i "$file_path" 2>&1 | head -20
```

If it's a video file, extract the audio track first:

```bash
ffmpeg -i "$file_path" -vn -acodec pcm_s16le -ar 16000 -ac 1 /tmp/cw-transcribe-audio.wav -y
```

### Step 2: Transcribe with Whisper

Run local Whisper transcription. Use the `base` model by default (fast, good enough for English):

```bash
python3 -c "
import whisper
model = whisper.load_model('base')
result = model.transcribe('$audio_path', fp16=False)
for seg in result['segments']:
    print(f\"[{seg['start']:.1f}s - {seg['end']:.1f}s] {seg['text'].strip()}\")
"
```

If the file is a video AND the user wants screenshot cross-references, use the full transcription script:

```bash
python3 ~/repos/dgrd/scripts/transcribe_with_screenshots.py \
  --audio "$audio_path" \
  --video "$file_path" \
  --out /tmp/cw-transcription/
```

### Step 3: Parse transcript into structured requirements

Read the transcript and parse it into these categories:

**Bug Reports** (things that are broken):
- Title
- Severity: critical / high / medium / low
- Description of the problem
- Steps to reproduce (if mentioned)
- Expected vs actual behaviour

**Feature Requests** (things they want):
- Title
- User story: As a [role], I want [capability] so that [benefit]
- Acceptance criteria (extracted or inferred)
- Priority hint (based on emphasis, repetition, urgency in conversation)

**Clarifications Needed** (ambiguous items):
- What was said
- What needs to be clarified before creating a ticket
- Suggested follow-up question

### Step 4: Present summary

Output a markdown summary with all parsed items, grouped by category. For each item, include the timestamp range from the transcript so the user can verify.

Ask the user:
1. Are the categorisations correct?
2. Should any items be split or merged?
3. Are there clarifications needed before creating issues?
4. Ready to create issues? (suggest using `/create-issue` for each item)
