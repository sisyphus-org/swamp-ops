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
→ deterministic local Russian number normalization
→ one UTF-8 transcript for Hermes
```

Exact normalized boundary words are deduplicated only when at least three words match. If the overlap is uncertain, both fragments are retained; a small duplicate is safer than losing dictated content. Existing paragraph separators are preserved when a later overlap matches. A failed or empty chunk aborts the whole transcription and does not publish a partial output file.

The runtime fails closed unless the Qwen endpoint is explicit HTTP loopback (`127.0.0.1`, `::1`, or `localhost`) at `/transcribe`; HTTP redirects are disabled and a changed final URL is rejected. Chunk duration, overlap, total duration, chunk step and chunk count are finite and operationally bounded so malformed media metadata or environment values cannot cause unbounded or duplicate planning.

## Versioned source and deployed artifact

- Source: `scripts/chunked_qwen_stt.py`
- Vendored parser: `vendor/number_parser` (`number-parser` 0.3.2, BSD-3-Clause)
- Stable runtime artifact: `/Users/hermes/workspaces/runtime/hermes-stt/chunked_qwen_stt.py`
- Stable runtime vendor: `/Users/hermes/workspaces/runtime/hermes-stt/vendor/number_parser`
- Python: `/Users/hermes/.local/share/hermes-stt/.venv/bin/python`
- Qwen endpoint: `http://127.0.0.1:8127/transcribe`

The source/deployed script SHA-256 and deterministic vendor-tree SHA-256 must both match. New profile baselines use the stable runtime artifact through `scripts/hermes_profile_bootstrap.py`.

## Numeric formatting prompt

Qwen3-ASR receives free-form context from `HERMES_STT_VOCAB`; the local server passes it to `processor.apply_transcription_request(prompt=...)`. The canonical value is versioned in `config/qwen-stt-numeric-prompt.txt`. Because this interface is transcription context/hotwords rather than a general instruction channel, the prompt contains only desired output-style exemplars—no spoken number words or arrow mappings that could reinforce the unwanted spelling. It provides best-effort bias for quantities, negative and decimal numbers, dates, time, percentages, versions, and monetary amounts.

Live testing showed that prompt bias alone does not reliably change Qwen's Russian number spelling. The command wrapper therefore performs a deterministic, fully local normalization pass after chunk merge. Cardinal numbers use the vendored Russian parser; bounded contextual rules format negative values, decimal fractions, percentages, dates, clock time, and dotted versions. Audio and transcript remain local, and unrelated non-numeric text is preserved.

The system LaunchDaemon must contain the exact prompt under `EnvironmentVariables.HERMES_STT_VOCAB`. Because `/Library/LaunchDaemons` and `launchctl` are owner-controlled, rollout uses a reviewed plist draft and an owner-approved restart. The plan reports only prompt hashes, not the full prompt, and emits `set-qwen-prompt` with `ownerRequired=true` when live state differs.

## Swamp workflow

`chunked-qwen-stt` has three bounded modes:

```bash
swamp model validate chunked-qwen-stt
swamp workflow validate chunked-qwen-stt

# Read-only live config/hash/prompt audit
swamp workflow run chunked-qwen-stt --input mode=plan

# Stage local samples in ignored runtime data, then run real Qwen smokes
mkdir -p .swamp/stt-samples
cp /path/to/long-sample.ogg .swamp/stt-samples/books-7m.ogg
cp /path/to/numeric-sample.aiff .swamp/stt-samples/sis69-numbers.aiff
swamp workflow run chunked-qwen-stt --input mode=smoke
swamp workflow run chunked-qwen-stt --input mode=numeric-smoke
```

The committed workflow always uses fixed `books-7m` and `sis69-numbers` sample slugs, so no user-controlled sample value reaches the shell command. Smoke executes the stable deployed wrapper and vendored parser rather than the worktree source, proving the live Telegram path. The operations script still validates slugs for direct operator use. Symlinked samples, symlinked runtime parents/output roots, and paths resolving outside the fixed runtime root are rejected. Smoke output and source audio stay under ignored `.swamp/` runtime data. The versioned result contains only hashes, metrics, and the numeric assertion result—not transcript content.

## Deployment and config

Deploy the reviewed source to the stable runtime path and verify identical hashes. Then set this exact command for every Telegram-enabled profile using `hermes config set`:

```text
/Users/hermes/.local/share/hermes-stt/.venv/bin/python /Users/hermes/workspaces/runtime/hermes-stt/chunked_qwen_stt.py {input_path} {output_path}
```

The STT provider reads this config for each transcription; a Gateway restart was not required in the SIS-64 live readback. Always verify with a direct `tools.transcription_tools.transcribe_audio` call and rerun the Swamp plan until `result=compliant` with no planned actions.

For the shared Qwen service prompt, generate and review a plist draft that differs from the live plist only by `EnvironmentVariables.HERMES_STT_VOCAB`. The owner then installs the reviewed draft and restarts the system LaunchDaemon:

```bash
sudo cp /Users/hermes/workspaces/drafts/local.qwen-stt.sis69.plist /Library/LaunchDaemons/local.qwen-stt.plist && sudo chown root:wheel /Library/LaunchDaemons/local.qwen-stt.plist && sudo chmod 644 /Library/LaunchDaemons/local.qwen-stt.plist && sudo plutil -lint /Library/LaunchDaemons/local.qwen-stt.plist && sudo launchctl kickstart -k system/local.qwen-stt
```

After restart, verify `/health`, rerun plan until `qwenPromptCompliant=true`, then run `numeric-smoke`. A passing numeric smoke must report `numericAssertionsPassed=true` without including transcript content.

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
