# Chunked Qwen3-ASR

SIS-64 removes the practical 1024-output-token ceiling from long Telegram voice notes without changing the local Qwen model or sending audio to cloud services.

## Runtime path

```text
Telegram voice note
→ Hermes command STT provider
→ /Users/hermes/workspaces/runtime/hermes-stt/chunked_qwen_stt.py
→ ffprobe duration
→ short: one localhost Qwen request
→ long: 180 s chunks with 3 s overlap
→ localhost:8127/transcribe for each chunk
→ conservative overlap merge
→ one UTF-8 transcript for Hermes
```

Exact normalized boundary words are deduplicated only when at least three words match. If the overlap is uncertain, both fragments are retained; a small duplicate is safer than losing dictated content. Existing paragraph separators are preserved when a later overlap matches. A failed or empty chunk aborts the whole transcription and does not publish a partial output file.

The runtime fails closed unless the Qwen endpoint is explicit HTTP loopback (`127.0.0.1`, `::1`, or `localhost`) at `/transcribe`; HTTP redirects are disabled and a changed final URL is rejected. Chunk duration, overlap, total duration, chunk step and chunk count are finite and operationally bounded so malformed media metadata or environment values cannot cause unbounded or duplicate planning.

## Versioned source and deployed artifact

- Source: `scripts/chunked_qwen_stt.py`
- Stable runtime artifact: `/Users/hermes/workspaces/runtime/hermes-stt/chunked_qwen_stt.py`
- Python: `/Users/hermes/.local/share/hermes-stt/.venv/bin/python`
- Qwen endpoint: `http://127.0.0.1:8127/transcribe`

The source and deployed SHA-256 must match. New profile baselines use the stable runtime artifact through `scripts/hermes_profile_bootstrap.py`.

## Swamp workflow

`chunked-qwen-stt` has two bounded modes:

```bash
swamp model validate chunked-qwen-stt
swamp workflow validate chunked-qwen-stt

# Read-only live config/hash audit
swamp workflow run chunked-qwen-stt --input mode=plan

# Stage a local sample in ignored runtime data, then run real Qwen smoke
mkdir -p .swamp/stt-samples
cp /path/to/sample.ogg .swamp/stt-samples/books-7m.ogg
swamp workflow run chunked-qwen-stt --input mode=smoke
```

The committed workflow always uses the fixed `books-7m` sample slug, so no user-controlled sample value reaches the shell command. The operations script still validates slugs for direct operator use. Symlinked samples, symlinked runtime parents/output roots, and paths resolving outside the fixed runtime root are rejected. Smoke output and source audio stay under ignored `.swamp/` runtime data. The versioned result contains only hashes and metrics, not transcript content.

## Deployment and config

Deploy the reviewed source to the stable runtime path and verify identical hashes. Then set this exact command for every Telegram-enabled profile using `hermes config set`:

```text
/Users/hermes/.local/share/hermes-stt/.venv/bin/python /Users/hermes/workspaces/runtime/hermes-stt/chunked_qwen_stt.py {input_path} {output_path}
```

The STT provider reads this config for each transcription; a Gateway restart was not required in the SIS-64 live readback. Always verify with a direct `tools.transcription_tools.transcribe_audio` call and rerun the Swamp plan until `result=compliant` with no planned actions.

## Tuning

Optional environment variables:

- `HERMES_STT_CHUNK_SECONDS` — default `180`;
- `HERMES_STT_OVERLAP_SECONDS` — default `3`;
- `HERMES_QWEN_STT_ENDPOINT` — default localhost endpoint;
- `HERMES_QWEN_STT_TIMEOUT` — default `300` seconds per chunk.

Overlap must be smaller than chunk duration. Enforced bounds are 30–300 seconds per chunk, 0–30 seconds overlap, at most six hours of input and at most 720 chunks. Keep chunks below the observed single-pass output ceiling; raising the server's `max_new_tokens` is not the primary control.

## Rollback

Restore the previous command provider path:

```text
/Users/hermes/.local/share/hermes-stt/.venv/bin/python /Users/hermes/.local/share/hermes-stt/qwen3_stt.py {input_path} {output_path}
```

Do not stop or reconfigure the shared Qwen service for this rollback; only the command wrapper changes.
