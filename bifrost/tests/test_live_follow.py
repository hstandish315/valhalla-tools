#!/usr/bin/env python3
"""
Live mode, round two: the bugs found when a real Spotify session was tried.

  * Spotify opens a NEW stream for each track/restart, and its streams disagree
    about their own name ("Spotify" vs "Chromium"), so an app must be followed by
    identity, not by stream number.
  * Bifrost's own output stream has no process ID in the graph; it must still
    never be offered (sending it through the sink is a feedback loop).
  * Stalls between audio blocks (the likely source of static) must be counted.

The stream shapes below are copied from what the real graph showed.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from bifrost import dsp, engine, live  # noqa: E402
from test_live import FakePipeWire, graph, node  # noqa: E402


def stream(i, **props):
    return node(i, "Stream/Output/Audio", **props)


# what the real graph showed for Spotify (Flatpak), at two different moments
SPOTIFY_A = stream(105, **{"application.name": "Spotify", "media.name": "Spotify", "node.name": "Spotify"})
SPOTIFY_B = stream(112, **{"application.name": "Chromium", "application.process.binary": "spotify",
                           "application.process.id": 261, "media.name": "Playback", "node.name": "Chromium",
                           "pipewire.access.portal.app_id": "com.spotify.Client"})
LIBREWOLF = stream(120, **{"application.name": "LibreWolf", "application.process.binary": "librewolf",
                           "media.name": "Some tab"})
OTHER_CHROMIUM = stream(121, **{"application.name": "Chromium", "application.process.binary": "brave",
                                "media.name": "Playback"})
# Bifrost's own player: NO application.process.id, as in the real graph
OURS_NO_PID = stream(88, **{"application.name": "Bifrost", "node.name": "bifrost-output", "media.name": "-"})
OURS_OLD_STYLE = stream(89, **{"application.name": "pw-play", "node.name": "bifrost-output", "media.name": "-"})


def first(streams):
    return live.parse_streams(graph(streams))[0]


class TestIdentity(unittest.TestCase):
    def test_spotifys_two_differently_named_streams_are_the_same_app(self):
        a, b = first([SPOTIFY_A]), first([SPOTIFY_B])
        self.assertTrue(a.tokens & b.tokens, (a.tokens, b.tokens))
        self.assertEqual(a.app, "Spotify")
        self.assertEqual(b.app, "Spotify", "a stream calling itself 'Chromium' should still read as Spotify")

    def test_generic_names_do_not_make_unrelated_apps_match(self):
        spot, other = first([SPOTIFY_B]), first([OTHER_CHROMIUM])
        self.assertFalse(spot.tokens & other.tokens)
        self.assertEqual(other.app, "Brave")
        self.assertNotIn("chromium", spot.tokens)

    def test_distinct_apps_stay_distinct(self):
        self.assertFalse(first([SPOTIFY_B]).tokens & first([LIBREWOLF]).tokens)

    def test_label_falls_back_gracefully(self):
        bare = first([stream(5)])
        self.assertTrue(bare.app)

    def test_our_own_stream_is_hidden_even_without_a_process_id(self):
        names = [s.app for s in live.parse_streams(graph([SPOTIFY_B, OURS_NO_PID, OURS_OLD_STYLE]))]
        self.assertEqual(names, ["Spotify"])

    def test_is_own(self):
        self.assertTrue(live.is_own({"node.name": "bifrost-output"}))
        self.assertTrue(live.is_own({"node.name": "bifrost-capture"}))
        self.assertTrue(live.is_own({"application.name": "Bifrost"}))
        self.assertFalse(live.is_own({"node.name": "Chromium", "application.name": "Spotify"}))

    def test_our_streams_carry_the_names_the_filter_looks_for(self):
        self.assertTrue(engine.OWN_NODE_NAME.startswith("bifrost"))
        self.assertTrue(live.CAPTURE_NODE_NAME.startswith("bifrost"))


class TestFollow(unittest.TestCase):
    def setup_router(self, streams):
        fp = FakePipeWire(streams)
        r = fp.router()
        r.create_sink()
        return fp, r

    def moves(self, fp):
        return [c[1] for c in fp.cmds if c[0] == "pw-metadata" and c[1] != "-d"]

    def test_a_new_track_stream_is_moved_automatically_after_the_app_was_sent(self):
        fp, r = self.setup_router([SPOTIFY_A])
        a = r.streams()[0]
        r.send(a)
        self.assertEqual(self.moves(fp), ["105"])
        # track two: Spotify opens a new stream under a different name
        fp.streams = [SPOTIFY_A, SPOTIFY_B]
        moved = r.follow(r.streams())
        self.assertEqual(moved, [112])
        self.assertEqual(self.moves(fp), ["105", "112"])

    def test_following_ignores_other_apps(self):
        fp, r = self.setup_router([SPOTIFY_A])
        r.send(r.streams()[0])
        fp.streams = [SPOTIFY_A, LIBREWOLF, OTHER_CHROMIUM]
        self.assertEqual(r.follow(r.streams()), [])

    def test_a_failed_send_does_not_leave_the_app_followed(self):
        fp, r = self.setup_router([SPOTIFY_A])
        r._run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom") if cmd[0] == "pw-metadata" else fp.runner(cmd, **kw)
        with self.assertRaises(live.LiveError):
            r.send(r.streams()[0])
        self.assertEqual(r.sticky, {})

    def test_follow_is_idempotent(self):
        fp, r = self.setup_router([SPOTIFY_A])
        r.send(r.streams()[0])
        fp.streams = [SPOTIFY_A, SPOTIFY_B]
        r.follow(r.streams())
        before = len(fp.cmds)
        self.assertEqual(r.follow(r.streams()), [])
        self.assertEqual([c for c in fp.cmds[before:] if c[0] == "pw-metadata"], [])

    def test_nothing_is_followed_until_the_user_sends_an_app(self):
        fp, r = self.setup_router([SPOTIFY_B])
        self.assertEqual(r.follow(r.streams()), [])
        self.assertEqual(r.sticky, {})

    def test_send_also_moves_the_apps_other_playing_streams(self):
        fp, r = self.setup_router([SPOTIFY_A, SPOTIFY_B])
        streams = r.streams()
        r.send(streams[0], streams)
        self.assertEqual(sorted(self.moves(fp)), ["105", "112"])

    def test_release_returns_every_stream_of_the_app_and_stops_following(self):
        fp, r = self.setup_router([SPOTIFY_A, SPOTIFY_B])
        streams = r.streams()
        r.send(streams[0], streams)
        streams = r.streams()
        r.release(streams[0], streams)
        restored = sorted(c[2] for c in fp.cmds if c[:2] == ["pw-metadata", "-d"])
        self.assertEqual(restored, ["105", "112"])
        self.assertEqual(r.sticky, {})
        fp.streams = [SPOTIFY_B]
        self.assertEqual(r.follow(r.streams()), [], "released apps must stay released")

    def test_forget_stops_following_without_touching_streams(self):
        fp, r = self.setup_router([SPOTIFY_A])
        r.send(r.streams()[0])
        r.forget("Spotify")
        self.assertEqual(r.sticky, {})

    def test_destroy_clears_following_and_restores_everything(self):
        fp, r = self.setup_router([SPOTIFY_A])
        r.send(r.streams()[0])
        r.destroy_sink()
        self.assertEqual(r.sticky, {})
        self.assertEqual(r.moved, set())
        self.assertIn(["pw-metadata", "-d", "105", "target.object"], fp.cmds)


class TestGraphEvents(unittest.TestCase):
    def setUp(self):
        self.fp = FakePipeWire([SPOTIFY_A])
        self.r = self.fp.router()
        self.r.create_sink()
        self.r.send(self.r.streams()[0])
        self.changes = []
        self.r.on_change = lambda: self.changes.append(1)

    def test_a_new_stream_event_moves_it_and_notifies(self):
        self.r.on_graph_event([SPOTIFY_B])
        self.assertIn(112, self.r.moved)
        self.assertEqual(self.changes, [1])

    def test_an_unrelated_stream_event_is_ignored(self):
        self.r.on_graph_event([LIBREWOLF])
        self.assertNotIn(120, self.r.moved)
        self.assertEqual(self.changes, [])

    def test_removal_event_forgets_the_stream(self):
        self.r.on_graph_event([{"id": 105, "info": None}])
        self.assertNotIn(105, self.r.moved)

    def test_malformed_events_do_not_crash(self):
        self.r.on_graph_event([None, 5, "x", {}, {"type": "PipeWire:Interface:Node"}, {"id": 3, "info": {}}])

    def test_the_stream_update_that_changes_media_name_does_not_remove_it_from_following(self):
        self.r.on_graph_event([SPOTIFY_B])
        again = stream(112, **{**SPOTIFY_B["info"]["props"], "media.name": "Next track"})
        before = len(self.fp.cmds)
        self.r.on_graph_event([again])
        self.assertEqual([c for c in self.fp.cmds[before:] if c[0] == "pw-metadata"], [])


class TestJsonStream(unittest.TestCase):
    def feed(self, chunks):
        it = iter(chunks)
        return list(live.iter_json_docs(lambda: next(it, b"")))

    def test_documents_split_across_chunks(self):
        doc = json.dumps([{"id": 1, "x": "é€"}]).encode()
        for cut in range(1, len(doc)):
            self.assertEqual(self.feed([doc[:cut], doc[cut:]]), [[{"id": 1, "x": "é€"}]], cut)

    def test_several_documents_in_one_chunk(self):
        data = b'[{"id":1}]\n[{"id":2}]\n  [{"id":3}]'
        self.assertEqual([d[0]["id"] for d in self.feed([data])], [1, 2, 3])

    def test_pretty_printed_output_like_pw_dump(self):
        pretty = json.dumps([{"id": 7, "info": {"props": {"a": 1}}}], indent=2).encode()
        self.assertEqual(self.feed([pretty[:11], pretty[11:40], pretty[40:]])[0][0]["id"], 7)

    def test_empty_stream_ends_cleanly(self):
        self.assertEqual(self.feed([]), [])


class WatcherProc:
    """A fake `pw-dump -m`: yields prepared output, then blocks until killed."""

    def __init__(self, chunks):
        self.chunks, self.stopped = list(chunks), threading.Event()
        self.stdout = self
        self.killed = False

    def read(self, n):
        if self.chunks:
            return self.chunks.pop(0)
        self.stopped.wait(5)
        return b""

    def poll(self): return 0 if self.killed else None

    def kill(self):
        self.killed = True
        self.stopped.set()

    def wait(self, timeout=None): return 0
    def close(self): pass


class TestWatcherThread(unittest.TestCase):
    def test_a_new_stream_is_moved_by_the_background_watcher(self):
        fp = FakePipeWire([SPOTIFY_A])
        spotify_b_event = json.dumps([SPOTIFY_B]).encode()
        watcher = WatcherProc([b"[]", spotify_b_event])
        base_popen = fp.popen

        def popen(cmd, **kw):
            return watcher if cmd[:2] == ["pw-dump", "-m"] else base_popen(cmd, **kw)

        r = live.LiveRouter(fp.runner, popen, sleep=lambda s: None, timeout=1.0)
        r.sticky["Spotify"] = live.tokens_for(SPOTIFY_A["info"]["props"])   # the user already sent Spotify
        r.create_sink()
        deadline = time.time() + 3
        while time.time() < deadline and 112 not in r.moved:
            time.sleep(0.02)
        self.assertIn(112, r.moved, "the watcher thread should have moved the new stream")
        r.destroy_sink()
        self.assertTrue(watcher.killed, "the watcher must be stopped with the sink")

    def test_the_watcher_is_stopped_even_if_it_never_produced_anything(self):
        fp = FakePipeWire()
        watcher = WatcherProc([])
        base_popen = fp.popen
        r = live.LiveRouter(fp.runner, lambda cmd, **kw: watcher if cmd[:2] == ["pw-dump", "-m"] else base_popen(cmd, **kw),
                            sleep=lambda s: None, timeout=1.0)
        r.create_sink()
        t0 = time.time()
        r.destroy_sink()
        self.assertLess(time.time() - t0, 3.0)
        self.assertTrue(watcher.killed)


class TestPriming(unittest.TestCase):
    def test_the_first_bytes_written_are_exactly_the_requested_silence(self):
        from test_live import ToneLive
        sink = engine.MemorySink()
        eng = ToneLive(dsp.Chain(fade_in_s=0.0), live.LiveRouter(), sink_factory=lambda: sink)
        eng.prime_ms = 100
        eng.start()
        time.sleep(0.8)
        eng.stop()
        first = sink.chunks[0]
        self.assertEqual(len(first), int(dsp.SAMPLE_RATE * 0.1) * engine.BYTES_PER_FRAME)
        self.assertEqual(first.count(0), len(first), "the cushion must be pure silence")
        self.assertGreater(len(sink.chunks), 3)

    def test_no_priming_when_disabled(self):
        from test_live import ToneLive
        sink = engine.MemorySink()
        eng = ToneLive(dsp.Chain(fade_in_s=0.0), live.LiveRouter(), sink_factory=lambda: sink)
        eng.prime_ms = 0
        eng.start()
        time.sleep(0.6)
        eng.stop()
        self.assertNotEqual(len(sink.chunks[0]), int(dsp.SAMPLE_RATE * 0.1) * engine.BYTES_PER_FRAME)


class TestStallStatistics(unittest.TestCase):
    def make(self, per_write_sleep):
        class Slow(engine.MemorySink):
            def write(self, data):
                time.sleep(per_write_sleep)
                super().write(data)
        from test_live import ToneLive
        chain = dsp.Chain(fade_in_s=0.0)
        return ToneLive(chain, live.LiveRouter(), sink_factory=Slow)

    def test_a_healthy_stream_reports_no_stalls(self):
        eng = self.make(0.0)
        eng.start()
        time.sleep(1.2)
        eng.stop()
        self.assertGreater(eng.stats["blocks"], 20)
        self.assertEqual(eng.stats["stalls"], 0, eng.stats)

    def test_a_blocked_consumer_is_counted_as_stalls(self):
        eng = self.make(0.15)                      # 150 ms per 20 ms block: far over budget
        eng.start()
        time.sleep(1.6)
        eng.stop()
        self.assertGreater(eng.stats["stalls"], 3, eng.stats)
        self.assertGreater(eng.stats["max_stall_ms"], engine.STALL_MS)

    def test_stats_reset_for_each_run(self):
        eng = self.make(0.15)
        eng.start()
        time.sleep(0.8)
        eng.stop()
        first_stalls = eng.stats["stalls"]
        eng.sink_factory = engine.MemorySink
        eng._sink_factory = engine.MemorySink
        eng.start()
        time.sleep(0.5)
        eng.stop()
        self.assertGreaterEqual(first_stalls, 1)
        self.assertEqual(eng.stats["stalls"], 0)


class TestCommandLines(unittest.TestCase):
    def popen_args(self, fn):
        with mock.patch("subprocess.Popen") as p:
            p.return_value = mock.MagicMock()
            fn()
        return p.call_args[0][0]

    def test_our_player_is_named_so_live_mode_can_recognise_it(self):
        args = self.popen_args(lambda: engine.PwPlaySink(latency="100ms", target="speakers"))
        joined = " ".join(args)
        self.assertIn(f"node.name={engine.OWN_NODE_NAME}", joined)
        self.assertIn("application.name=Bifrost", joined)
        self.assertEqual(args[args.index("--target") + 1], "speakers")
        self.assertEqual(args[args.index("--latency") + 1], "100ms")

    def test_the_capture_stream_is_named_and_reads_the_sink_monitor(self):
        eng = live.LiveEngine(dsp.Chain(), live.LiveRouter())
        args = self.popen_args(eng._spawn_source)
        joined = " ".join(args)
        self.assertIn(f"node.name={live.CAPTURE_NODE_NAME}", joined)
        self.assertIn("stream.capture.sink=true", joined)
        self.assertEqual(args[args.index("--target") + 1], live.SINK)
        self.assertIn("--raw", args)

    def test_live_output_is_primed_with_silence_and_file_playback_is_not(self):
        self.assertGreaterEqual(live.LiveEngine.PRIME_MS, 60, "60 ms was the smallest cushion measured to give 0 underruns")
        self.assertEqual(engine.Engine(dsp.Chain()).prime_ms, 0)
        self.assertEqual(live.LiveEngine(dsp.Chain(), live.LiveRouter()).prime_ms, live.LiveEngine.PRIME_MS)


@unittest.skipUnless(os.environ.get("BIFROST_PW_TEST") == "1" and not live.missing_tools(),
                     "set BIFROST_PW_TEST=1 (plays very quiet tones through PipeWire)")
class TestRealFollow(unittest.TestCase):
    """
    The real graph, reproducing what Spotify does: an app is sent through Bifrost, then
    opens a NEW stream (under a different name) for its next track. No manual step: the
    background watcher must move it.
    """

    SPOTIFY_NAME = "{ application.name=Spotify media.name=Spotify }"
    SPOTIFY_CHROMIUM = ("{ application.name=Chromium application.process.binary=spotify "
                        "pipewire.access.portal.app_id=com.spotify.Client media.name=Playback }")
    OTHER = "{ application.name=SomeOtherApp media.name=Other }"

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="bifrost-follow-")
        self.wav = os.path.join(self.tmp, "t.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=40",
                        "-ac", "2", self.wav], check=True)
        self.procs = []
        self.router = live.LiveRouter()
        self._shutil = shutil

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
            p.wait()
        self.router.destroy_sink()
        time.sleep(0.5)
        self._shutil.rmtree(self.tmp, ignore_errors=True)

    def play(self, props):
        p = subprocess.Popen(["pw-play", "--volume", "0.02", "-P", props, self.wav])
        self.procs.append(p)
        return p

    def wait_for(self, pred, timeout=4.0):
        end = time.time() + timeout
        while time.time() < end:
            hit = pred()
            if hit:
                return hit
            time.sleep(0.05)
        return None

    def find(self, media, exclude=()):
        return self.wait_for(lambda: next((s for s in self.router.streams()
                                           if s.media == media and s.node_id not in exclude), None))

    def linked_to_sink(self, node_id):
        out = subprocess.run(["pw-link", "-l"], capture_output=True, text=True).stdout
        return live.SINK in out and any(f":output_" in ln and ln.strip().startswith(("pw-play", "Spotify", "Chromium"))
                                         for ln in out.splitlines())

    def test_a_new_stream_of_a_followed_app_is_moved_without_any_manual_step(self):
        self.router.create_sink()
        a = self.play(self.SPOTIFY_NAME)
        first = self.find("Spotify")
        self.assertIsNotNone(first, "the first stream never appeared")
        self.router.send(first)
        self.assertIn(first.node_id, self.router.moved)

        # 'next track': a different stream, calling itself Chromium, from the same app
        t0 = time.time()
        self.play(self.SPOTIFY_CHROMIUM)
        second = self.find("Playback", exclude={first.node_id})
        self.assertIsNotNone(second, "the second stream never appeared")
        moved = self.wait_for(lambda: second.node_id in self.router.moved, timeout=3.0)
        delay = time.time() - t0
        self.assertTrue(moved, "the watcher did not move the new stream of the followed app")
        print(f"\n   new stream followed {delay:.2f}s after it started")
        self.assertLess(delay, 2.5)

        # and a third: the old ones end, a new one starts, as on every track change
        a.terminate()
        a.wait()
        self.play(self.SPOTIFY_CHROMIUM)
        third = self.find("Playback", exclude={first.node_id, second.node_id})
        self.assertIsNotNone(third)
        self.assertTrue(self.wait_for(lambda: third.node_id in self.router.moved, timeout=3.0))

    def test_an_unrelated_app_is_left_alone(self):
        self.router.create_sink()
        self.play(self.SPOTIFY_NAME)
        first = self.find("Spotify")
        self.router.send(first)
        self.play(self.OTHER)
        other = self.find("Other")
        self.assertIsNotNone(other)
        time.sleep(1.0)
        self.assertNotIn(other.node_id, self.router.moved)

    def test_our_own_player_is_never_offered_or_captured(self):
        from bifrost import engine as eng_mod
        self.router.create_sink()
        chain = dsp.Chain(fade_in_s=0.0)
        eng = live.LiveEngine(chain, self.router)         # the real pw-play and pw-record
        try:
            eng.start()
            time.sleep(1.5)
            listed = [s.app for s in self.router.streams()]
            self.assertNotIn("pw-play", listed)
            self.assertNotIn("Bifrost", listed)
            out = subprocess.run(["pw-dump"], capture_output=True, text=True).stdout
            names = {o["info"]["props"].get("node.name") for o in json.loads(out)
                     if o.get("type") == "PipeWire:Interface:Node" and o.get("info")}
            self.assertIn(eng_mod.OWN_NODE_NAME, names, "our player should be running under its own name")
        finally:
            eng.stop()


if __name__ == "__main__":
    unittest.main()
