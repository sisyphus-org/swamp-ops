import importlib.util
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError


SCRIPT = Path(__file__).parents[1] / "scripts" / "chunked_qwen_stt.py"
SPEC = importlib.util.spec_from_file_location("chunked_qwen_stt", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import chunked STT script: {SCRIPT}")
chunked = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = chunked
SPEC.loader.exec_module(chunked)


class ChunkPlanningTests(unittest.TestCase):
    def test_long_audio_is_split_with_overlap_and_full_tail_coverage(self):
        chunks = chunked.plan_chunks(426.22, chunk_seconds=180.0, overlap_seconds=3.0)
        self.assertEqual(
            chunks,
            [(0.0, 180.0), (177.0, 357.0), (354.0, 426.22)],
        )

    def test_short_audio_uses_one_full_length_chunk(self):
        self.assertEqual(
            chunked.plan_chunks(26.56, chunk_seconds=180.0, overlap_seconds=3.0),
            [(0.0, 26.56)],
        )

    def test_invalid_overlap_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            chunked.plan_chunks(200.0, chunk_seconds=30.0, overlap_seconds=30.0)

    def test_exact_chunk_boundary_stays_single_pass(self):
        self.assertEqual(
            chunked.plan_chunks(180.0, chunk_seconds=180.0, overlap_seconds=3.0),
            [(0.0, 180.0)],
        )

    def test_operational_tuning_bounds_are_enforced(self):
        cases = (
            {"duration_seconds": 200.0, "chunk_seconds": 29.0},
            {"duration_seconds": 200.0, "chunk_seconds": 301.0},
            {"duration_seconds": 200.0, "overlap_seconds": 31.0},
            {
                "duration_seconds": 30.00000005,
                "chunk_seconds": 30.00000001,
                "overlap_seconds": 30.0,
            },
            {"duration_seconds": chunked.MAX_AUDIO_SECONDS + 1.0},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    chunked.plan_chunks(**kwargs)

    def test_non_finite_duration_or_chunk_settings_are_rejected(self):
        cases = (
            {"duration_seconds": float("nan")},
            {"duration_seconds": float("inf")},
            {"duration_seconds": float("-inf")},
            {"duration_seconds": 200.0, "chunk_seconds": float("nan")},
            {"duration_seconds": 200.0, "overlap_seconds": float("inf")},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, "finite"):
                    chunked.plan_chunks(**kwargs)


class EndpointValidationTests(unittest.TestCase):
    def test_loopback_transcribe_endpoints_are_allowed(self):
        for endpoint in (
            "http://127.0.0.1:8127/transcribe",
            "http://localhost:8127/transcribe",
            "http://[::1]:8127/transcribe",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(chunked.validate_qwen_endpoint(endpoint), endpoint)

    def test_non_loopback_or_ambiguous_endpoints_are_rejected(self):
        for endpoint in (
            "https://127.0.0.1:8127/transcribe",
            "http://example.com/transcribe",
            "http://127.0.0.1:8127/other",
            "http://user@127.0.0.1:8127/transcribe",
            "http://127.0.0.1:8127/transcribe?next=http://example.com",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(ValueError, "loopback"):
                    chunked.validate_qwen_endpoint(endpoint)

    def test_http_redirects_are_not_followed(self):
        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", "/escaped")
                self.end_headers()

            def do_GET(self):
                body = b'{"text":"redirected"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f"http://127.0.0.1:{server.server_port}/transcribe"
            with self.assertRaises(HTTPError) as raised:
                chunked.qwen_transcribe(
                    Path("fixture.ogg"),
                    endpoint=endpoint,
                    timeout_seconds=2.0,
                )
            self.assertEqual(raised.exception.code, 302)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)


class EnvironmentTuningTests(unittest.TestCase):
    def test_zero_overlap_is_allowed_but_non_finite_values_fail(self):
        with mock.patch.dict("os.environ", {"HERMES_STT_OVERLAP_SECONDS": "0"}):
            self.assertEqual(
                chunked._nonnegative_float_from_env(
                    "HERMES_STT_OVERLAP_SECONDS", 3.0
                ),
                0.0,
            )
        for value in ("-1", "nan", "inf"):
            with self.subTest(value=value):
                with mock.patch.dict(
                    "os.environ", {"HERMES_STT_OVERLAP_SECONDS": value}
                ):
                    with self.assertRaises(ValueError):
                        chunked._nonnegative_float_from_env(
                            "HERMES_STT_OVERLAP_SECONDS", 3.0
                        )


class TranscriptMergeTests(unittest.TestCase):
    def test_exact_normalized_word_overlap_is_removed_once(self):
        merged = chunked.merge_transcripts(
            [
                "Первая часть заканчивается общей фразой здесь.",
                "Общей фразой здесь, затем продолжается вторая часть.",
            ],
            minimum_overlap_words=3,
        )
        self.assertEqual(
            merged,
            "Первая часть заканчивается общей фразой здесь, затем продолжается вторая часть.",
        )

    def test_uncertain_overlap_preserves_both_transcripts(self):
        merged = chunked.merge_transcripts(
            ["Первая уникальная часть.", "Совершенно другая вторая часть."],
            minimum_overlap_words=3,
        )
        self.assertEqual(
            merged,
            "Первая уникальная часть.\n\nСовершенно другая вторая часть.",
        )

    def test_later_overlap_does_not_collapse_existing_paragraph_breaks(self):
        merged = chunked.merge_transcripts(
            [
                "Первый абзац без совпадения.",
                "Второй абзац заканчивается общей границей слов",
                "общей границей слов и продолжается дальше",
            ],
            minimum_overlap_words=3,
        )
        self.assertEqual(
            merged,
            "Первый абзац без совпадения.\n\n"
            "Второй абзац заканчивается общей границей слов и продолжается дальше",
        )
    def test_overlap_at_paragraph_start_preserves_separator(self):
        merged = chunked.merge_transcripts(
            [
                "Первый абзац.\n\nобщая граница слов",
                "общая граница слов и продолжается дальше",
            ],
            minimum_overlap_words=3,
        )
        self.assertEqual(
            merged,
            "Первый абзац.\n\nобщая граница слов и продолжается дальше",
        )

    def test_overlap_and_right_continuation_keep_paragraph_separators(self):
        merged = chunked.merge_transcripts(
            [
                "Первый абзац.\n\nобщая\n\nграница слов",
                "общая\n\nграница слов\n\nСледующий абзац.",
            ],
            minimum_overlap_words=3,
        )
        self.assertEqual(
            merged,
            "Первый абзац.\n\nобщая\n\nграница слов\n\nСледующий абзац.",
        )


class RussianNumberNormalizationTests(unittest.TestCase):
    def test_paragraph_boundaries_prevent_cross_chunk_number_merging(self):
        self.assertEqual(
            chunked.normalize_russian_numbers("стоит двадцать\n\nпять человек"),
            "стоит 20\n\n5 человек",
        )

    def test_contextual_rewrites_never_cross_paragraph_boundaries(self):
        cases = (
            ("три целых\n\nпять десятых", "3 целых\n\n5 десятых"),
            ("минус\n\nсемь", "минус\n\n7"),
            ("двенадцать\n\nпроцентов", "12\n\nпроцентов"),
            (
                "четырнадцать часов\n\nтридцать минут",
                "14 часов\n\n30 минут",
            ),
            ("версия три\n\nточка два", "версия 3\n\nточка 2"),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(chunked.normalize_russian_numbers(source), expected)

    def test_spoken_numbers_are_rendered_in_contextual_digit_formats(self):
        source = (
            "В заказе двадцать пять деталей. Температура минус семь градусов. "
            "Расход три целых пять десятых литра. Скидка двенадцать процентов. "
            "Встреча двадцать восьмого августа две тысячи двадцать шестого года "
            "в четырнадцать часов тридцать минут. Версия три точка два. "
            "Сумма одна тысяча двести рублей."
        )
        self.assertEqual(
            chunked.normalize_russian_numbers(source),
            "В заказе 25 деталей. Температура -7 градусов. "
            "Расход 3,5 литра. Скидка 12%. "
            "Встреча 28 августа 2026 года в 14:30. Версия 3.2. "
            "Сумма 1200 рублей.",
        )

    def test_non_numeric_text_is_preserved(self):
        source = "Обычная фраза без чисел и без специальных преобразований."
        self.assertEqual(chunked.normalize_russian_numbers(source), source)


class RuntimeTests(unittest.TestCase):
    def test_normalization_failure_writes_raw_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "transcript.txt"
            with mock.patch.object(
                chunked,
                "normalize_russian_numbers",
                side_effect=RuntimeError("vendor unavailable"),
            ):
                result = chunked.run_to_output(
                    Path("short.ogg"),
                    output,
                    duration_seconds=10.0,
                    transcribe=lambda path: "двадцать пять деталей",
                    render_chunk=lambda *args: None,
                )
            self.assertEqual(result.text, "двадцать пять деталей")
            self.assertEqual(output.read_text(), "двадцать пять деталей")

    def test_short_audio_calls_qwen_once_without_rendering_chunks(self):
        rendered = []
        transcribed = []

        def render(source, start, end, output):
            rendered.append((source, start, end, output))

        def transcribe(path):
            transcribed.append(path)
            return "короткая запись полностью"

        result = chunked.transcribe_in_chunks(
            Path("short.ogg"),
            duration_seconds=26.56,
            transcribe=transcribe,
            render_chunk=render,
        )
        self.assertEqual(result.text, "короткая запись полностью")
        self.assertEqual(result.chunk_count, 1)
        self.assertEqual(transcribed, [Path("short.ogg")])
        self.assertEqual(rendered, [])

    def test_long_audio_renders_and_transcribes_every_planned_chunk(self):
        rendered = []
        rendered_paths = []
        transcribed = []
        responses = iter(
            [
                "первая часть общая граница слов",
                "общая граница слов вторая часть другая граница слов",
                "другая граница слов финальная часть полностью",
            ]
        )

        def render(source, start, end, output):
            if rendered_paths:
                self.assertFalse(rendered_paths[-1].exists())
            rendered.append((round(start, 2), round(end, 2)))
            output.write_bytes(b"fixture")
            rendered_paths.append(output)

        def transcribe(path):
            transcribed.append(path)
            return next(responses)

        result = chunked.transcribe_in_chunks(
            Path("long.ogg"),
            duration_seconds=426.22,
            transcribe=transcribe,
            render_chunk=render,
            chunk_seconds=180.0,
            overlap_seconds=3.0,
        )
        self.assertEqual(rendered, [(0.0, 180.0), (177.0, 357.0), (354.0, 426.22)])
        self.assertEqual(len(transcribed), 3)
        self.assertTrue(all(not path.exists() for path in rendered_paths))
        self.assertEqual(result.chunk_count, 3)
        self.assertEqual(
            result.text,
            "первая часть общая граница слов вторая часть другая граница слов финальная часть полностью",
        )

    def test_failed_or_empty_chunk_aborts_without_partial_output(self):
        for failure in (RuntimeError("qwen unavailable"), "   "):
            with self.subTest(failure=failure):
                with tempfile.TemporaryDirectory() as tmp:
                    output = Path(tmp) / "transcript.txt"
                    output.write_text("previous complete transcript")

                    def render(source, start, end, chunk_output):
                        chunk_output.write_bytes(b"fixture")

                    calls = 0

                    def transcribe(path):
                        nonlocal calls
                        calls += 1
                        if calls == 2:
                            if isinstance(failure, Exception):
                                raise failure
                            return failure
                        return "first chunk"

                    with self.assertRaises((RuntimeError, ValueError)):
                        chunked.run_to_output(
                            Path("long.ogg"),
                            output,
                            duration_seconds=426.22,
                            transcribe=transcribe,
                            render_chunk=render,
                        )
                    self.assertEqual(
                        output.read_text(),
                        "previous complete transcript",
                    )

    def test_atomic_write_cleans_temp_file_when_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "transcript.txt"
            output.write_text("previous complete transcript")
            temp_path = root / ".transcript.txt.fixture.tmp"

            class FailingHandle:
                name = str(temp_path)

                def __enter__(self):
                    temp_path.write_text("")
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return False

                def write(self, text):
                    temp_path.write_text("partial")
                    raise OSError("disk write failed")

            with mock.patch.object(
                chunked.tempfile,
                "NamedTemporaryFile",
                return_value=FailingHandle(),
            ):
                with self.assertRaisesRegex(OSError, "disk write failed"):
                    chunked.atomic_write_text(output, "replacement")
            self.assertEqual(output.read_text(), "previous complete transcript")
            self.assertFalse(temp_path.exists())


if __name__ == "__main__":
    unittest.main()
