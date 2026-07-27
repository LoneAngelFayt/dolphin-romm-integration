"""GPU environment forwarding: what survives the sudo hop into Dolphin.

sudo's env_reset wipes the container environment, so a renderer setting the
operator put in docker-compose only reaches Dolphin if the broker names it on
the `env` command line. Everything unnamed is silently dropped, which reads
from the outside like the container ignoring its own configuration.
"""

import os
import unittest
import unittest.mock as mock

from support import broker


class GpuEnvTests(unittest.TestCase):

    def test_forwards_vendor_graphics_variables(self):
        with mock.patch.dict(os.environ, {
            "VK_DRIVER_FILES": "/usr/share/vulkan/icd.d/nvidia_icd.json",
            "__GLX_VENDOR_LIBRARY_NAME": "nvidia",
            "NVIDIA_DRIVER_CAPABILITIES": "all",
            "MESA_LOADER_DRIVER_OVERRIDE": "zink",
        }, clear=False):
            env = broker._gpu_env()
        self.assertEqual(
            env["VK_DRIVER_FILES"], "/usr/share/vulkan/icd.d/nvidia_icd.json"
        )
        self.assertEqual(env["__GLX_VENDOR_LIBRARY_NAME"], "nvidia")
        self.assertEqual(env["NVIDIA_DRIVER_CAPABILITIES"], "all")
        self.assertEqual(env["MESA_LOADER_DRIVER_OVERRIDE"], "zink")

    def test_forwards_the_base_image_render_node_selector(self):
        """DRINODE has no DRI_ prefix, so it needs naming explicitly."""
        with mock.patch.dict(os.environ, {"DRINODE": "/dev/dri/renderD130"}, clear=False):
            env = broker._gpu_env()
        self.assertEqual(env["DRINODE"], "/dev/dri/renderD130")

    def test_ignores_unrelated_variables(self):
        with mock.patch.dict(os.environ, {
            "BROKER_SECRET": "hunter2",
            "PATH": "/usr/bin",
        }, clear=False):
            env = broker._gpu_env()
        self.assertNotIn("BROKER_SECRET", env)
        self.assertNotIn("PATH", env)

    def test_unset_render_nodes_are_not_forwarded_as_empty(self):
        """Regression: DRI_NODE and DRINODE were forwarded unconditionally with
        an "" default, so every launch carried `DRI_NODE= DRINODE=` even when
        the operator had never set them. `env VAR=` is set-but-empty, not
        unset, and consumers disagree on what that means."""
        cleared = {k: "" for k in ("DRI_NODE", "DRINODE")}
        with mock.patch.dict(os.environ, cleared, clear=False):
            env = broker._gpu_env()
        self.assertNotIn("DRI_NODE", env)
        self.assertNotIn("DRINODE", env)
        self.assertNotIn("DRI_NODE", broker.ENV)
        self.assertNotIn("DRINODE", broker.ENV)

    def test_explicit_entries_win_over_inherited_ones(self):
        """Dolphin deliberately omits WAYLAND_DISPLAY and pins DISPLAY to :0;
        an inherited value must not undo either."""
        self.assertEqual(broker.ENV["DISPLAY"], ":0")
        self.assertNotIn("WAYLAND_DISPLAY", broker.ENV)


if __name__ == "__main__":
    unittest.main()
