# yt-subtitler

Clip a section of any YouTube video, transcribe or translate it to English, and burn the subtitles into the output. Self-hosted, runs locally, no third-party API required.

Three subtitle modes, picked per-job in the UI:

| Mode | What it does | When to use |
|---|---|---|
| **Whisper only** *(default)* | Runs `faster-whisper` on the clip's audio. `large-v3` by default for best quality. | Any video. Most reliable. |
| **YouTube subs + LM Studio translate** | Pulls YouTube's original-language subtitle track (json3 format with word-level timing), sends it in batches to a local OpenAI-compatible LLM endpoint for translation. | Foreign-language videos with YT subs. Faster than Whisper, often better translation quality. |
| **YouTube English only** | Downloads YouTube's English subs (manual or auto-translated). | Quick captions for English-language videos. |
| **Auto** | Tries YouTube English subs first, falls back to Whisper. | Mixed bag of videos. |

Subtitles are font-scaled to the video resolution, capped to 2 lines, and split into multiple cues if a single Whisper segment is too long to fit.

A second tab lets you re-trim any previously-burned video without re-running translation.

A third tab exposes the LLM endpoint (URL, model, API key) so you can point it at LM Studio, Ollama, vLLM, OpenRouter, or OpenAI directly.

---

## Quick start (Docker)

This command clones the full `Slop-Tools` repository, then opens the `yt-subtitler` folder inside it.

```bash
git clone https://github.com/FraudHawker/Slop-Tools.git
cd Slop-Tools/yt-subtitler
docker compose up -d --build
open http://localhost:8077
```

The first time you run with the Whisper model `large-v3`, the app will download ~3 GB of weights to the `./models/` volume when you run your first Whisper job. Subsequent runs reuse them. Outputs land in `./data/`.

If the app restarts mid-job, that job is restored in the UI as `interrupted by restart` instead of silently disappearing.

### LM Studio integration (optional)

If you want the **YouTube subs + LM Studio translate** mode:

1. Install [LM Studio](https://lmstudio.ai/) on the same machine
2. Load any chat model (Qwen3 / Aya Expanse / Gemma-3 work well for translation)
3. Start the local server (Developer tab → toggle on, default port 1234)
4. The container auto-discovers it via `host.docker.internal` — no further setup needed

To use a different OpenAI-compatible endpoint (Ollama, OpenRouter, OpenAI…), open the **Settings** tab and change the Base URL.

Browser-cookie extraction is disabled by default in the public build. If a specific video requires it, you can opt in by setting `COOKIES_FROM_BROWSER` yourself.

### Download just this tool

If you only want the `yt-subtitler` folder instead of the full repo:

```bash
git clone --filter=blob:none --sparse https://github.com/FraudHawker/Slop-Tools.git
cd Slop-Tools
git sparse-checkout set yt-subtitler
cd yt-subtitler
docker compose up -d --build
open http://localhost:8077
```

---

## Configuration

All settings have sensible defaults. Override via env vars or the in-app Settings tab.

| Env var | Default | Notes |
|---|---|---|
| `LISTEN_PORT` | `8077` | Web UI port |
| `DATA_DIR` | `./data` (or `/data` in Docker) | Where clips/subs/settings.json live |
| `MODEL_CACHE` | `./models` (or `/models` in Docker) | Faster-whisper weights cache |
| `LLM_BASE_URL` | `http://127.0.0.1:1234/v1` (`http://host.docker.internal:1234/v1` in Docker) | OpenAI-compatible endpoint |
| `LLM_MODEL` | *(blank)* | Forced model id; blank = pick first from `/v1/models` |
| `LLM_API_KEY` | `lm-studio` | Most local servers ignore this |
| `COOKIES_FROM_BROWSER` | `""` | Optional yt-dlp browser-cookie import for videos that need it |
| `MAX_QUEUED_JOBS` | `8` | Max backlog waiting for the single worker |
| `MAX_SAVED_JOBS` | `25` | How many completed clips to keep on disk |
| `MODEL_IDLE_EVICT_SECONDS` | `900` | Unload the Whisper model after this much idle time |

---

## How it works

1. **Download**: `yt-dlp --download-sections "*START-END" --force-keyframes-at-cuts` fetches just the requested time range (no full-video download).
2. **Subtitles** (one of three paths):
   - **Whisper**: `faster-whisper` transcribes/translates the clip's audio.
   - **YT json3**: yt-dlp pulls the original-language subtitle track in YouTube's `json3` format (word-level timing). A custom parser builds clean non-overlapping cues, dropping YT's "rolling overlap" markers that cause flicker when burned.
   - **YT json3 + LLM**: same as above, then sends batches to the LLM with a JSON-schema-enforced response format. Per-line retry on any garbage output.
3. **Smooth**: post-processes the SRT to enforce non-overlap, minimum duration, line-wrap to 2 lines × ~N chars (N scales with video width).
4. **Burn**: ffmpeg's `subtitles` filter with `original_size=WxH` so libass scales fonts to the actual resolution. Output `.mp4` plus the source `.srt`.

---

## Testing

Run the smoke test:

```bash
./test.sh
```

It verifies startup, settings persistence behavior, validation, and basic runtime wiring. It does not exercise live YouTube downloads or Whisper transcription, which happen at runtime rather than image build time.

---

## License

MIT — see [LICENSE](LICENSE).
