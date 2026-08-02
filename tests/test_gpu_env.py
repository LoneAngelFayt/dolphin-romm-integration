"""GPU environment forwarding: what survives the sudo hop into Dolphin.

sudo's env_reset wipes the container environment, so a renderer setting the
operator put in docker-compose only reaches Dolphin if the broker names it on
the `env` command line. Everything unnamed is silently dropped, which reads
from the outside like the container ignoring its own configuration.
"""

import os
import pathlib
import tempfile
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


class GlvndPin(unittest.TestCase):
    """Issue #7: the gamepad interposer's fake libudev hides the NVIDIA device
    from GLVND's udev lookup, dropping the renderer to llvmpipe. Naming the
    vendor outright is the fix, but only on NVIDIA and never over an operator's
    own choice."""

    def test_nothing_is_pinned_off_nvidia(self):
        # Forcing the nvidia vendor on an AMD/Intel host would break a working
        # Mesa driver, which is exactly the platform where the fake libudev is
        # already harmless.
        with mock.patch.object(broker, "_nvidia_present", lambda: False):
            self.assertEqual(broker._glvnd_env({}), {})

    def test_the_glx_vendor_is_pinned_on_nvidia(self):
        with mock.patch.object(broker, "_nvidia_present", lambda: True), \
             mock.patch.object(broker, "_nvidia_glx_present", lambda: True), \
             mock.patch.object(broker, "_NVIDIA_EGL_ICD", pathlib.Path("/nope.json")):
            self.assertEqual(
                broker._glvnd_env({}), {"__GLX_VENDOR_LIBRARY_NAME": "nvidia"}
            )

    def test_the_glx_vendor_is_not_pinned_without_its_vendor_library(self):
        # A device node says nothing about the GL userspace: nvidia-smi works on
        # NVIDIA_DRIVER_CAPABILITIES=utility, where libGLX_nvidia.so.0 was never
        # mounted. Naming a vendor GLVND cannot load breaks GLX for every client,
        # which on a container whose only GL client is the emulator means the Qt
        # menu survives and every game dies on context creation.
        with mock.patch.object(broker, "_nvidia_present", lambda: True), \
             mock.patch.object(broker, "_nvidia_glx_present", lambda: False), \
             mock.patch.object(broker, "_NVIDIA_EGL_ICD", pathlib.Path("/nope.json")):
            self.assertEqual(broker._glvnd_env({}), {})

    def test_an_operator_vendor_choice_is_never_overridden(self):
        with mock.patch.object(broker, "_nvidia_present", lambda: True), \
             mock.patch.object(broker, "_nvidia_glx_present", lambda: True), \
             mock.patch.object(broker, "_NVIDIA_EGL_ICD", pathlib.Path("/nope.json")):
            self.assertEqual(
                broker._glvnd_env({"__GLX_VENDOR_LIBRARY_NAME": "mesa"}), {}
            )

    def test_the_vendor_library_is_looked_for_in_every_lib_dir(self):
        # The container runtime places the driver libraries wherever the image's
        # ldconfig points, which differs between the multiarch and lib64 layouts.
        for lib_dir in broker._NVIDIA_LIB_DIRS:
            with tempfile.TemporaryDirectory() as tmp:
                (pathlib.Path(tmp) / broker._NVIDIA_GLX_LIB).touch()
                dirs = tuple(
                    tmp if d == lib_dir else "/nonexistent"
                    for d in broker._NVIDIA_LIB_DIRS
                )
                with mock.patch.object(broker, "_NVIDIA_LIB_DIRS", dirs):
                    self.assertTrue(broker._nvidia_glx_present())

    def test_no_vendor_library_anywhere_reads_as_absent(self):
        with mock.patch.object(broker, "_NVIDIA_LIB_DIRS", ("/nonexistent",)):
            self.assertFalse(broker._nvidia_glx_present())

    def test_the_egl_icd_is_pinned_only_when_its_file_exists(self):
        # Pointing the loader at a missing filename disables every other ICD and
        # breaks EGL outright, so the pin is conditional on the file being there.
        with mock.patch.object(broker, "_nvidia_present", lambda: True):
            with tempfile.NamedTemporaryFile(suffix=".json") as f:
                with mock.patch.object(broker, "_NVIDIA_EGL_ICD", pathlib.Path(f.name)):
                    self.assertEqual(
                        broker._glvnd_env({}).get("__EGL_VENDOR_LIBRARY_FILENAMES"),
                        f.name,
                    )


if __name__ == "__main__":
    unittest.main()
