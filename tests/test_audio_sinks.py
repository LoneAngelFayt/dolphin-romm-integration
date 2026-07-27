"""Repair of the PulseAudio null sinks selkies needs to stream any sound.

svc-selkies loads "output" and "input" once at container start and touches a
lock file so it never retries. When its first load-module loses the race with
PulseAudio's socket, "output" is gone for the life of the container and every
pcmflux start fails with "No such entity" — a silent stream with no symptom in
Dolphin. _ensure_audio_sinks is what puts it back, so it has to be right when
PulseAudio is late, when a sink is missing, and when it is not.
"""

import subprocess
import unittest
import unittest.mock

from support import broker


SHORT_SINKS = "\t".join(["1", "input", "module-null-sink.c", "s16le 2ch 48000Hz", "SUSPENDED"])
OUTPUT_LINE = "\t".join(["2", "output", "module-null-sink.c", "s16le 2ch 48000Hz", "IDLE"])


class FakePactl:
    """Stands in for _pactl, recording calls and replaying scripted output."""

    def __init__(self, sinks=SHORT_SINKS, default="input", fail_until=0):
        self.calls = []
        self.sinks = sinks
        self.default = default
        # Number of leading "list sinks" calls that fail, i.e. how long
        # PulseAudio refuses connections before coming up.
        self.fail_until = fail_until
        self.list_attempts = 0
        self.load_returncode = 0

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("list", "short"):
            self.list_attempts += 1
            if self.list_attempts <= self.fail_until:
                return self._result(1, "", "Connection refused")
            return self._result(0, self.sinks)
        if args[0] == "load-module":
            if self.load_returncode == 0:
                name = args[2].split("=", 1)[1]
                self.sinks = "\n".join(filter(None, [self.sinks, OUTPUT_LINE.replace("output", name)]))
            return self._result(self.load_returncode, "3\n", "Module initialization failed")
        if args[0] == "get-default-sink":
            return self._result(0, self.default + "\n")
        if args[0] == "set-default-sink":
            self.default = args[1]
            return self._result(0)
        return self._result(0)

    @staticmethod
    def _result(returncode, stdout="", stderr=""):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def loaded_sinks(self):
        return [c[2].split("=", 1)[1] for c in self.calls if c[0] == "load-module"]


class EnsureAudioSinks(unittest.TestCase):
    def run_with(self, pactl, **kwargs):
        with unittest.mock.patch.object(broker, "_pactl", pactl), \
             unittest.mock.patch.object(broker.time, "sleep", lambda _s: None):
            broker._ensure_audio_sinks(**kwargs)

    def test_a_missing_output_sink_is_recreated_and_made_default(self):
        pactl = FakePactl()
        self.run_with(pactl)
        self.assertEqual(pactl.loaded_sinks(), ["output"])
        self.assertEqual(pactl.default, "output")

    def test_both_sinks_are_recreated_when_pulseaudio_started_empty(self):
        pactl = FakePactl(sinks="")
        self.run_with(pactl)
        self.assertEqual(pactl.loaded_sinks(), ["output", "input"])

    def test_healthy_sinks_are_left_alone(self):
        pactl = FakePactl(sinks="\n".join([SHORT_SINKS, OUTPUT_LINE]), default="output")
        self.run_with(pactl)
        self.assertEqual(pactl.loaded_sinks(), [])
        self.assertNotIn(("set-default-sink", "output"), pactl.calls)

    def test_the_default_is_moved_even_when_both_sinks_exist(self):
        """A default left on the microphone loopback is silent on its own."""
        pactl = FakePactl(sinks="\n".join([SHORT_SINKS, OUTPUT_LINE]), default="input")
        self.run_with(pactl)
        self.assertEqual(pactl.loaded_sinks(), [])
        self.assertEqual(pactl.default, "output")

    def test_a_late_pulseaudio_is_waited_for(self):
        pactl = FakePactl(fail_until=3)
        self.run_with(pactl)
        self.assertEqual(pactl.loaded_sinks(), ["output"])

    def test_an_unreachable_pulseaudio_gives_up_instead_of_hanging(self):
        """Broker startup must reach serve_forever() even with audio broken."""
        pactl = FakePactl(fail_until=float("inf"))
        self.run_with(pactl, timeout=2.0)
        self.assertEqual(pactl.loaded_sinks(), [])

    def test_a_failed_load_does_not_repoint_the_default_at_a_missing_sink(self):
        pactl = FakePactl()
        pactl.load_returncode = 1
        self.run_with(pactl)
        self.assertEqual(pactl.default, "input")

    def test_sink_names_are_parsed_on_tabs_not_whitespace(self):
        """pactl pads columns with spaces; splitting on them yields "s16le"."""
        pactl = FakePactl(sinks="\t".join(["1", "my sink", "module-null-sink.c", "s16le 2ch"]))
        with unittest.mock.patch.object(broker, "_pactl", pactl):
            self.assertEqual(broker._pactl_sink_names(), ["my sink"])


if __name__ == "__main__":
    unittest.main()
