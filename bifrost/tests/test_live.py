#!/usr/bin/env python3
"""
Live mode. Graph parsing runs against synthetic pw-dump data shaped like the real
thing (the real dump contains device serial numbers, so none of it is stored);
the router's command sequences run through fakes; LiveEngine runs a real timed
ffmpeg tone in place of the capture. An opt-in test (BIFROST_PW_TEST=1) drives
the real PipeWire graph with a quiet tone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bifrost import dsp, engine, live  # noqa: E402

SR = dsp.SAMPLE_RATE


def node(i, cls, **props):
    return {"id": i, "type": "PipeWire:Interface:Node", "info": {"props": {"media.class": cls, **props}}}


def metadata(default_sink):
    return {"id": 41, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"},
            "metadata": [{"subject": 0, "key": "default.configured.audio.sink", "type": "Spa:String:JSON",
                          "value": {"name": "configured-but-unplugged"}},
                         {"subject": 0, "key": "default.audio.sink", "type": "Spa:String:JSON",
                          "value": {"name": default_sink}}]}


def graph(streams=(), sinks=("speakers",), default="speakers", with_live=False):
    d = [node(10 + i, "Audio/Sink", **{"node.name": n}) for i, n in enumerate(sinks)]
    if with_live:
        d.append(node(87, "Audio/Sink", **{"node.name": live.SINK}))
    d += list(streams)
    d.append(metadata(default))
    d.append({"id": 99, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "settings"}, "metadata": []})
    return d


SPOTIFY = node(91, "Stream/Output/Audio", **{"application.name": "Spotify", "application.process.binary": "spotify",
                                              "application.process.id": 4242, "media.name": "Some Song"})
BROWSER = node(92, "Stream/Output/Audio", **{"application.name": "LibreWolf", "media.name": "Playback",
                                              "application.process.id": 5000})
OURS = node(93, "Stream/Output/Audio", **{"application.name": "pw-play", "application.process.id": 7777})


class TestParsing(unittest.TestCase):
    def test_lists_playing_apps_with_details(self):
        s = live.parse_streams(graph([SPOTIFY, BROWSER]))
        self.assertEqual([(x.node_id, x.app) for x in s], [(91, "Spotify"), (92, "LibreWolf")])
        self.assertEqual(s[0].binary, "spotify")
        self.assertEqual(s[0].media, "Some Song")

    def test_excludes_our_own_player_by_pid(self):
        s = live.parse_streams(graph([SPOTIFY, OURS]), exclude_pids=(7777,))
        self.assertEqual([x.app for x in s], ["Spotify"])

    def test_ignores_sinks_inputs_and_unrelated_nodes(self):
        mic = node(50, "Stream/Input/Audio", **{"application.name": "recorder"})
        s = live.parse_streams(graph([mic, SPOTIFY]))
        self.assertEqual([x.app for x in s], ["Spotify"])

    def test_missing_properties_do_not_crash(self):
        bare = node(60, "Stream/Output/Audio")
        s = live.parse_streams(graph([bare]))
        self.assertEqual(s[0].app, "unknown app")

    def test_sinks_and_default(self):
        d = graph(sinks=("a", "b"), default="b")
        self.assertEqual(live.parse_sinks(d), ["a", "b"])
        self.assertEqual(live.parse_default_sink(d), "b")

    def test_default_ignores_the_configured_but_absent_device(self):
        self.assertEqual(live.parse_default_sink(graph(default="actual")), "actual")

    def test_no_metadata_means_no_default(self):
        self.assertIsNone(live.parse_default_sink([node(1, "Audio/Sink", **{"node.name": "x"})]))

    def test_missing_tools(self):
        self.assertEqual(live.missing_tools(lambda t: "/bin/x"), [])
        self.assertEqual(live.missing_tools(lambda t: None if t == "pw-cli" else "/bin/x"), ["pw-cli"])


class FakePipeWire:
    """Stands in for runner + popen: records commands and simulates the graph."""

    def __init__(self, streams=(), default="speakers", sinks=("speakers",), create_works=True):
        self.cmds, self.streams, self.default, self.sinks = [], list(streams), default, list(sinks)
        self.live_exists, self.create_works, self.cli = False, create_works, None

    def runner(self, cmd, **kw):
        self.cmds.append(cmd)
        if cmd[0] == "pw-dump":
            return subprocess.CompletedProcess(cmd, 0, json.dumps(
                graph(self.streams, self.sinks, self.default, with_live=self.live_exists)), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def popen(self, cmd, **kw):
        fake = self

        class Cli:
            def __init__(self):
                self.written, self.terminated = [], False
                self.stdin = self
                fake.cli = self

            def write(self, text):
                self.written.append(text)
                if fake.create_works and "create-node" in text:
                    fake.live_exists = True

            def flush(self): pass
            def close(self): pass
            def poll(self): return 0 if self.terminated else None

            def terminate(self):
                self.terminated = True
                fake.live_exists = False

            def wait(self, timeout=None): return 0
            def kill(self): self.terminate()
        return Cli()

    def router(self):
        return live.LiveRouter(self.runner, self.popen, sleep=lambda s: None, timeout=1.0)


class TestRouter(unittest.TestCase):
    def test_create_sink_sends_the_expected_node_definition_and_waits_for_it(self):
        fp = FakePipeWire()
        r = fp.router()
        r.create_sink()
        text = fp.cli.written[0]
        for want in ("support.null-audio-sink", f"node.name={live.SINK}", "media.class=Audio/Sink",
                     "object.linger=false"):
            self.assertIn(want, text)
        self.assertTrue(r.active)

    def test_object_linger_false_is_what_ties_the_sink_to_our_process(self):
        # without it the sink would outlive a crash and strand the user's audio
        self.assertIn("object.linger=false", live._CREATE_SINK)

    def test_creation_failure_cleans_up_and_reports(self):
        fp = FakePipeWire(create_works=False)
        r = fp.router()
        with self.assertRaisesRegex(live.LiveError, "didn't create"):
            r.create_sink()
        self.assertFalse(r.active)
        self.assertTrue(fp.cli.terminated)

    def test_refuses_when_a_sink_already_exists(self):
        fp = FakePipeWire()
        fp.live_exists = True
        with self.assertRaisesRegex(live.LiveError, "already exists"):
            fp.router().create_sink()

    def test_move_and_restore_use_pw_metadata(self):
        fp = FakePipeWire([SPOTIFY])
        r = fp.router()
        r.create_sink()
        r.move(91)
        self.assertIn(["pw-metadata", "91", "target.object", live.SINK, "Spa:String"], fp.cmds)
        self.assertTrue(r.streams()[0].routed)
        r.restore(91)
        self.assertIn(["pw-metadata", "-d", "91", "target.object"], fp.cmds)
        self.assertFalse(r.streams()[0].routed)

    def test_cannot_move_before_the_sink_exists(self):
        with self.assertRaisesRegex(live.LiveError, "Start live mode"):
            FakePipeWire([SPOTIFY]).router().move(91)

    def test_destroy_returns_every_moved_stream_before_removing_the_sink(self):
        fp = FakePipeWire([SPOTIFY, BROWSER])
        r = fp.router()
        r.create_sink()
        r.move(91)
        r.move(92)
        fp.cmds.clear()
        r.destroy_sink()
        restores = [c for c in fp.cmds if c[:2] == ["pw-metadata", "-d"]]
        self.assertEqual(sorted(c[2] for c in restores), ["91", "92"])
        self.assertTrue(fp.cli.terminated)
        self.assertFalse(r.active)
        self.assertEqual(r.moved, set())

    def test_restore_all_is_safe_when_a_stream_has_gone(self):
        fp = FakePipeWire([SPOTIFY])
        r = fp.router()
        r.create_sink()
        r.moved = {91, 404}

        def boom(cmd, **kw):
            if cmd[:2] == ["pw-metadata", "-d"] and cmd[2] == "404":
                raise subprocess.SubprocessError("gone")
            return fp.runner(cmd, **kw)
        r._run = boom
        r.restore_all()
        self.assertEqual(r.moved, set())

    def test_output_is_the_real_default_never_our_own_sink(self):
        fp = FakePipeWire(default=live.SINK, sinks=("speakers", "hdmi"))
        fp.live_exists = True
        self.assertEqual(fp.router().output_sink(), "speakers")

    def test_output_uses_the_default_when_it_is_real(self):
        self.assertEqual(FakePipeWire(default="hdmi", sinks=("speakers", "hdmi")).router().output_sink(), "hdmi")

    def test_no_output_device_is_a_clean_error(self):
        fp = FakePipeWire(default=live.SINK, sinks=())
        fp.live_exists = True
        with self.assertRaisesRegex(live.LiveError, "No audio output"):
            fp.router().output_sink()

    def test_graph_read_failure_is_reported(self):
        def boom(*a, **k):
            raise FileNotFoundError("pw-dump")
        with self.assertRaisesRegex(live.LiveError, "audio graph"):
            live.LiveRouter(boom).dump()


def tone_source(seconds=30):
    """A real-time 440 Hz f32 stereo stream, like a capture of a playing app."""
    return subprocess.Popen(
        ["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i",
         f"sine=frequency=440:sample_rate={SR}:duration={seconds}", "-ac", "2", "-f", "f32le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)


class ToneLive(live.LiveEngine):
    def _spawn_source(self):
        return tone_source()


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
class TestLiveEngine(unittest.TestCase):
    def make(self):
        chain = dsp.Chain(fade_in_s=0.0)
        chain.pan_enabled = False
        chain.am.freq, chain.am.depth = 16.0, 0.5
        sink = engine.MemorySink()
        eng = ToneLive(chain, live.LiveRouter(), sink_factory=lambda: sink)
        return eng, sink

    def test_captured_audio_is_processed_in_real_time(self):
        eng, sink = self.make()
        eng.start()
        time.sleep(1.6)
        eng.stop()
        audio = sink.audio()
        self.assertGreater(len(audio) / SR, 1.0)
        left = np.abs(audio[:, 0])
        env = left[: len(left) // 441 * 441].reshape(-1, 441).max(axis=1)   # 10 ms peaks
        self.assertGreater(env.max() / max(env.min(), 1e-6), 1.3, "16 Hz pulse should modulate the level")

    def test_stop_is_prompt_and_leaves_no_children(self):
        eng, _ = self.make()
        eng.start()
        time.sleep(0.5)
        t0 = time.time()
        eng.stop()
        self.assertLess(time.time() - t0, 2.5)
        self.assertEqual(eng.state, "stopped")
        out = subprocess.run(["pgrep", "-P", str(os.getpid()), "-x", "ffmpeg"], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "", "the capture child must not outlive stop()")

    def test_seek_is_a_no_op(self):
        eng, _ = self.make()
        eng.start()
        time.sleep(0.3)
        eng.seek(30)
        self.assertEqual(eng.state, "playing")
        eng.stop()

    def test_own_pids_comes_from_the_sink(self):
        eng, _ = self.make()
        self.assertEqual(eng.own_pids, ())

        class S(engine.MemorySink):
            pid = 4321
        eng.sink = S()
        self.assertEqual(eng.own_pids, (4321,))


@unittest.skipUnless(os.environ.get("BIFROST_PW_TEST") == "1" and not live.missing_tools(),
                     "set BIFROST_PW_TEST=1 (plays a very quiet tone through PipeWire)")
class TestRealPipeWire(unittest.TestCase):
    """The real graph: sink -> real app stream -> move -> capture -> process -> restore -> cleanup."""

    def test_end_to_end(self):
        tmp = tempfile.mkdtemp(prefix="bifrost-pw-")
        wav = os.path.join(tmp, "tone.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=20", "-ac", "2", wav], check=True)
        router = live.LiveRouter()
        chain = dsp.Chain(fade_in_s=0.0)
        chain.pan_enabled = False
        chain.am.depth = 0.5
        sink = engine.MemorySink()
        eng = live.LiveEngine(chain, router, sink_factory=lambda: sink)
        player = None
        try:
            router.create_sink()
            self.assertTrue(router.sink_exists())
            player = subprocess.Popen(["pw-play", "--volume", "0.05", wav])
            deadline, stream = time.time() + 6, None
            while time.time() < deadline and stream is None:
                time.sleep(0.3)
                stream = next((s for s in router.streams() if s.app == "pw-play"), None)
            self.assertIsNotNone(stream, "the test tone's stream never appeared")
            router.move(stream.node_id)
            eng.start()
            time.sleep(2.5)
            eng.stop()
            audio = sink.audio()
            self.assertGreater(len(audio) / SR, 1.5)
            self.assertGreater(float(np.abs(audio).max()), 0.001, "captured silence")
            # the signal must be the app's 440 Hz tone, not noise or a stray stream
            seg = audio[SR // 2:, 0].astype(np.float64)
            spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
            peak_hz = np.fft.rfftfreq(len(seg), 1.0 / SR)[spec.argmax()]
            self.assertAlmostEqual(peak_hz, 440.0, delta=8.0)
            self.assertTrue(any(s.routed for s in router.streams()))
            router.restore(stream.node_id)
            router.destroy_sink()
            time.sleep(1.0)
            self.assertFalse(router.sink_exists(), "the virtual sink must be gone")
        finally:
            eng.stop()
            if player:
                player.terminate()
                player.wait()
            router.destroy_sink()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
