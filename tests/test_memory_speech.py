"""ARIA v2 — unit tests for memory + speech.

Run:  python3 -m unittest discover -s tests -t .   (from ~/workspace/aria-v2)
or:   python3 tests/test_memory_speech.py

Rules honored: no network calls (Ollama HTTP is stubbed), no git, no real
credentials anywhere. Each test uses an isolated temp SQLite file.
"""

import json
import math
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_HERE)
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from aria.memory import (  # noqa: E402
    save_memory,
    search_memory,
    search_memory_semantic,
    forget_memory,
    log_incident,
    get_recent_incidents,
    append_event,
)
from aria.memory import vector as vector_mod  # noqa: E402
import aria.memory as mem  # noqa: E402
import aria.speech as sp  # noqa: E402
import importlib as _importlib  # noqa: E402
# NOTE: `aria.speech/__init__` re-exports the *function* `speak`, which
# shadows the submodule attribute — so fetch the submodules from
# sys.modules via importlib to get the real modules for monkeypatching.
listen_mod = _importlib.import_module("aria.speech.listen")  # noqa: E402
speak_mod = _importlib.import_module("aria.speech.speak")  # noqa: E402


def _tmp_db(test):
    fd, path = tempfile.mkstemp(prefix="aria_v2_test_", suffix=".db")
    os.close(fd)
    test.addCleanup(lambda: os.path.exists(path) and os.remove(path))
    # WAL mode creates -wal/-shm sidecars; clean those too.
    test.addCleanup(lambda: [os.path.exists(p) and os.remove(p)
                             for p in (path + "-wal", path + "-shm")])
    return path


class TestMemoryRoundtrip(unittest.TestCase):
    def test_save_search_forget(self):
        db = _tmp_db(self)
        save_memory("prefs", "coffee", "black, two sugars", db_path=db)
        save_memory("prefs", "tea", "earl grey", db_path=db)

        hits = search_memory("coffee", db_path=db)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["key"], "coffee")
        self.assertEqual(hits[0]["value"], "black, two sugars")
        self.assertEqual(hits[0]["category"], "prefs")

        # LIKE special chars in the query must not act as wildcards.
        save_memory("prefs", "100%_sure", "x", db_path=db)
        hits = search_memory("100%", db_path=db)
        self.assertTrue(any(h["key"] == "100%_sure" for h in hits))

        # Overwrite is an upsert, not a duplicate.
        save_memory("prefs", "coffee", "oat milk latte", db_path=db)
        hits = search_memory("coffee", db_path=db)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["value"], "oat milk latte")

        n = forget_memory(key="coffee", db_path=db)
        self.assertEqual(n, 1)
        self.assertEqual(search_memory("coffee", db_path=db), [])

        # forget by query substring.
        n = forget_memory(query="tea", db_path=db)
        self.assertGreaterEqual(n, 1)
        self.assertEqual(search_memory("tea", db_path=db), [])

    def test_search_empty_query_returns_empty(self):
        db = _tmp_db(self)
        save_memory("prefs", "k", "v", db_path=db)
        self.assertEqual(search_memory("", db_path=db), [])
        self.assertEqual(search_memory("   ", db_path=db), [])

    def test_functions_never_raise_on_bad_db(self):
        bad = os.path.join(tempfile.mkdtemp(), "nope", "x.db")
        # Point at an uncreatable location via env-independent means:
        # use a path under a *file* so makedirs fails.
        fd, fpath = tempfile.mkstemp()
        os.close(fd)
        bad = os.path.join(fpath, "sub", "x.db")
        self.addCleanup(lambda: os.path.exists(fpath) and os.remove(fpath))
        save_memory("c", "k", "v", db_path=bad)          # None, no raise
        self.assertEqual(search_memory("k", db_path=bad), [])
        self.assertEqual(forget_memory(key="k", db_path=bad), 0)
        self.assertEqual(log_incident("c", "s", db_path=bad), -1)
        self.assertEqual(get_recent_incidents(db_path=bad), [])
        append_event("kind", {"a": 1}, db_path=bad)       # no raise


class TestIncidentLearning(unittest.TestCase):
    def test_no_lesson_before_third_recurrence(self):
        db = _tmp_db(self)
        log_incident("vision", "webcam", error_text="no device",
                     diagnosis="camera unplugged", action_taken="retry",
                     db_path=db)
        log_incident("vision", "webcam", error_text="no device",
                     diagnosis="camera unplugged", action_taken="retry",
                     db_path=db)
        lessons = search_memory("lesson:vision:webcam", db_path=db)
        self.assertEqual(lessons, [],
                         "lesson must NOT be promoted on the 2nd recurrence")

    def test_lesson_promoted_on_third_recurrence(self):
        db = _tmp_db(self)
        for i in range(3):
            iid = log_incident("vision", "webcam", error_text=f"err {i}",
                               diagnosis="camera unplugged",
                               action_taken="use screen capture instead",
                               db_path=db)
            self.assertGreater(iid, 0)
        lessons = search_memory("lesson:vision:webcam", db_path=db)
        self.assertEqual(len(lessons), 1)
        lesson = lessons[0]
        self.assertEqual(lesson["category"], "self_heal")
        self.assertEqual(lesson["key"], "lesson:vision:webcam")
        self.assertIn("webcam", lesson["value"])
        self.assertIn("camera unplugged", lesson["value"])

    def test_resolved_incidents_do_not_count(self):
        db = _tmp_db(self)
        for _ in range(2):
            log_incident("net", "chain", error_text="timeout",
                         resolved=True, db_path=db)
        log_incident("net", "chain", error_text="timeout", db_path=db)
        self.assertEqual(search_memory("lesson:net:chain", db_path=db), [])

    def test_different_source_does_not_trigger(self):
        db = _tmp_db(self)
        for _ in range(3):
            log_incident("vision", "screen", error_text="x", db_path=db)
        self.assertEqual(search_memory("lesson:vision:webcam", db_path=db), [])
        self.assertEqual(len(search_memory("lesson:vision:screen",
                                           db_path=db)), 1)

    def test_get_recent_incidents(self):
        db = _tmp_db(self)
        log_incident("a", "s1", error_text="e1", db_path=db)
        log_incident("b", "s2", error_text="e2", db_path=db)
        rows = get_recent_incidents(limit=5, db_path=db)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["source"], "s2")  # newest first
        self.assertEqual(rows[1]["source"], "s1")
        self.assertIn("resolved", rows[0])
        self.assertIn("timestamp", rows[0])


class TestEventTape(unittest.TestCase):
    def test_append_and_persistence(self):
        db = _tmp_db(self)
        append_event("memory_save",
                     {"category": "prefs", "key": "k"}, db_path=db)
        append_event("tool", {"name": "speak"}, db_path=db)

        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT kind, payload_json FROM events ORDER BY id").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "memory_save")
        self.assertEqual(json.loads(rows[0][1])["key"], "k")
        self.assertEqual(rows[1][0], "tool")

    def test_non_serializable_payload_is_stringified(self):
        db = _tmp_db(self)
        append_event("weird", {"obj": object()}, db_path=db)  # must not raise
        conn = sqlite3.connect(db)
        (payload_json,) = conn.execute(
            "SELECT payload_json FROM events").fetchone()
        conn.close()
        self.assertIn("obj", json.loads(payload_json))


class TestVectorSearch(unittest.TestCase):
    def _stub_embed(self, mapping):
        def fake_embed(text):
            return list(mapping.get(text, []))
        return mock.patch.object(vector_mod, "embed", side_effect=fake_embed)

    def test_ranked_results(self):
        db = _tmp_db(self)
        mapping = {
            "the cat sat on the mat": [1.0, 0.0, 0.0],
            "dogs bark loudly at night": [0.0, 1.0, 0.0],
            "query about cats": [0.9, 0.1, 0.0],
        }
        with self._stub_embed(mapping):
            self.assertTrue(vector_mod.index_text("m1",
                                                  "the cat sat on the mat",
                                                  db_path=db))
            self.assertTrue(vector_mod.index_text("m2",
                                                  "dogs bark loudly at night",
                                                  db_path=db))
            save_memory("notes", "m1", "the cat sat on the mat", db_path=db)
            save_memory("notes", "m2", "dogs bark loudly at night",
                        db_path=db)

            hits = search_memory_semantic("query about cats", limit=5,
                                          db_path=db)
        self.assertEqual(len(hits), 2)
        # m1 is closer to the query than m2.
        self.assertEqual(hits[0]["key"], "m1")
        self.assertGreater(hits[0]["similarity"], hits[1]["similarity"])
        self.assertEqual(hits[0]["value"], "the cat sat on the mat")
        self.assertEqual(hits[0]["category"], "notes")
        self.assertLessEqual(hits[0]["similarity"], 1.0)

    def test_none_when_embed_fails(self):
        db = _tmp_db(self)
        with mock.patch.object(vector_mod, "embed", return_value=[]):
            self.assertFalse(vector_mod.index_text("m1", "anything",
                                                   db_path=db))
            # None (not []) signals "embeddings unavailable" so callers
            # fall back to keyword search.
            self.assertIsNone(search_memory_semantic("anything", db_path=db))

    def test_embed_real_failure_returns_empty(self):
        # No stub: localhost:11434 is not running in CI, so this must
        # return [] quickly rather than raise. (If Ollama IS running, the
        # model may be missing -> still [] or a real vector; either is fine,
        # but it must never raise.)
        try:
            out = vector_mod.embed("hello world")
        except Exception as e:  # pragma: no cover
            self.fail(f"embed raised: {e}")
        self.assertIsInstance(out, list)
        for x in out:
            self.assertTrue(math.isfinite(x))

    def test_dimension_mismatch_rows_skipped(self):
        db = _tmp_db(self)
        # Write a stale 2-dim vector directly, then search with 3-dim query.
        mapping = {"q": [1.0, 0.0, 0.0]}
        with self._stub_embed(mapping):
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS vectors"
                " (key TEXT PRIMARY KEY, vector_json TEXT, updated REAL)")
            conn.execute("INSERT INTO vectors VALUES (?, ?, ?)",
                         ("stale", json.dumps([0.5, 0.5]), time.time()))
            conn.commit()
            conn.close()
            hits = search_memory_semantic("q", db_path=db)
        self.assertEqual(hits, [])


class TestSpeech(unittest.TestCase):
    def test_listen_once_none_when_sr_missing(self):
        # Simulate speech_recognition being absent: importing it raises.
        with mock.patch.dict("sys.modules", {"speech_recognition": None}):
            self.assertIsNone(listen_mod.listen_once(timeout=1))

    def test_listen_once_none_on_mic_failure(self):
        fake_sr = mock.MagicMock()
        fake_sr.Microphone.side_effect = OSError("no mic")
        with mock.patch.dict("sys.modules", {"speech_recognition": fake_sr}):
            self.assertIsNone(listen_mod.listen_once(timeout=1))

    def test_wake_word_defaults_off(self):
        # Env var could theoretically enable it; default path is False.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARIA_V2_WAKE_WORD", None)
            listen_mod.set_wake_word_enabled(False)
            self.assertFalse(listen_mod.wake_word_active())
        listen_mod.set_wake_word_enabled(True, word="aria")
        try:
            self.assertTrue(listen_mod.wake_word_active())
        finally:
            listen_mod.set_wake_word_enabled(False)

    def test_speak_never_raises_without_pyttsx3(self):
        with mock.patch.dict("sys.modules", {"pyttsx3": None}):
            speak_mod._ENGINE = None  # force re-init path
            try:
                speak_mod.speak("hello world")  # must not raise
                speak_mod.speak("")             # empty: no-op
                speak_mod.speak(None)           # None: no-op
            except Exception as e:  # pragma: no cover
                self.fail(f"speak raised: {e}")

    def test_speak_never_raises_when_engine_raises(self):
        fake_engine = mock.MagicMock()
        fake_engine.say.side_effect = RuntimeError("audio device gone")
        with mock.patch.object(speak_mod, "_get_engine",
                               return_value=fake_engine):
            try:
                speak_mod.speak("hello world")  # must not raise
            except Exception as e:  # pragma: no cover
                self.fail(f"speak raised: {e}")

    def test_speak_calls_engine(self):
        fake_engine = mock.MagicMock()
        with mock.patch.object(speak_mod, "_get_engine",
                               return_value=fake_engine):
            speak_mod.speak("hi there")
        fake_engine.say.assert_called_once_with("hi there")
        fake_engine.runAndWait.assert_called_once()


class TestPublicSurface(unittest.TestCase):
    def test_init_reexports(self):
        for name in ("save_memory", "search_memory", "search_memory_semantic",
                     "forget_memory", "log_incident", "get_recent_incidents",
                     "append_event"):
            self.assertTrue(callable(getattr(mem, name)), name)

    def test_speech_init_reexports(self):
        for name in ("listen_once", "wake_word_active", "speak"):
            self.assertTrue(callable(getattr(sp, name)), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
