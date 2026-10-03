"""Focused tests for web-notification icon resolution and durability.

The scenarios are synthetic and offline: a temp desktop-entry tree and icon
theme stand in for the installed web apps. The regression that started this
work - proton.me coming back wearing X's web-app icon - cannot return without
one of these failing.
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))


def load_icon_module():
    loader = importlib.machinery.SourceFileLoader("omapager_icon_web", str(ROOT / "bin/omapager-icon"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def png_bytes(image_module, size=(64, 64), colour=(30, 90, 160, 255)):
    buffer = io.BytesIO()
    image_module.new("RGBA", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def require_pillow():
    try:
        from PIL import Image
    except ImportError:
        raise unittest.SkipTest("Pillow not installed; run locked scanner/test environment")
    return Image


class WebDesktopMatch(unittest.TestCase):
    """An installed web app is matched on the site's labels, and the browser
    that runs every one of them must never select one."""

    def setUp(self):
        self.Image = require_pillow()
        self.temp = tempfile.TemporaryDirectory(prefix="omapager-web-desktop-")
        self.root = Path(self.temp.name)
        self.apps = self.root / "applications"
        self.icons = self.root / "icons" / "hicolor" / "512x512" / "apps"
        self.apps.mkdir(parents=True)
        self.icons.mkdir(parents=True)
        for name in ("proton", "x", "whatsapp", "helium", "unknown", "maps"):
            (self.icons / (name + ".png")).write_bytes(png_bytes(self.Image))
        self.entry("01-helium", "Helium", "/opt/helium-browser-bin/helium-wrapper %U", "helium")
        self.entry(
            "02-x", "X",
            '/opt/helium-browser-bin/helium-wrapper --app-id=xapp '
            '"--app-launch-url-for-shortcuts-menu-item=https://x.com/home"',
            "x",
            extra=("Actions=Direct-Messages;\n\n[Desktop Action Direct-Messages]\n"
                   "Name=Direct Messages\n"
                   'Exec=/opt/helium-browser-bin/helium-wrapper '
                   '"--app-launch-url-for-shortcuts-menu-item=https://x.com/messages"\n'))
        self.entry("03-proton", "Proton Mail", "/opt/helium-browser-bin/helium-wrapper --app-id=protonapp", "proton")
        self.entry("04-whatsapp", "WhatsApp Web", "/opt/helium-browser-bin/helium-wrapper --app-id=wapp", "whatsapp")
        # A URL-scheme handler whose brand happens to be a host label. It is
        # not an installed web app and must never answer for a site.
        self.entry("05-maps", "Google Maps",
                   '/usr/bin/kde-geo-uri-handler "https://www.google.com/maps/" %u', "maps")
        self.icon = load_icon_module()
        patcher = mock.patch.dict(self.icon.__dict__, {
            "APP_DIRS": [str(self.apps)],
            "ICON_DIRS": [str(self.root / "icons")],
            "CONFIG": str(self.root / "config"),
            "CACHE": str(self.root / "cache"),
            "INDEX": str(self.root / "cache" / "index.json"),
            "load_index": lambda: {},
            "save_index": lambda data: None,
            "fetch_site": lambda *args, **kwargs: self.fail("fetching must not happen in this test"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temp.cleanup()

    def entry(self, stem, name, exec_line, icon, extra=""):
        text = ("[Desktop Entry]\nVersion=1.0\nType=Application\n"
                "Name=%s\nExec=%s\nIcon=%s\nStartupWMClass=crx_%s\n%s" %
                (name, exec_line, self.icons / (icon + ".png"), icon, extra))
        (self.apps / (stem + ".desktop")).write_text(text)

    def resolve(self, source, image=""):
        args = argparse.Namespace(key="web:" + source, app="Helium", app_icon="helium",
                                  source=source, scheme="dark", fetch=False, image=image)
        return self.icon.resolve(args)

    def test_proton_picks_proton_never_x(self):
        hit, how = self.resolve("proton.me")
        self.assertEqual(how, "from_desktop_entries:web")
        self.assertEqual(Path(hit).name, "proton.png")

    def test_whatsapp_picks_whatsapp_never_another_app(self):
        hit, how = self.resolve("web.whatsapp.com")
        self.assertEqual(how, "from_desktop_entries:web")
        self.assertEqual(Path(hit).name, "whatsapp.png")

    def test_short_brand_matches_its_shortcut_url(self):
        # "x" is too short a label to match, and the first Exec never mentions
        # the host; only the shortcut action URL identifies the X web app.
        hit, how = self.resolve("x.com")
        self.assertEqual(how, "from_desktop_entries:web")
        self.assertEqual(Path(hit).name, "x.png")

    def test_browser_name_cannot_pick_a_web_app(self):
        # No site matches, and the browser name runs every entry: it must not
        # select the alphabetically first PWA as a fallback.
        self.assertEqual(self.resolve("example.com"), (None, "none"))

    def test_action_names_do_not_select_an_entry(self):
        # The X entry carries an action called "Direct Messages"; a site
        # labelled "messages" must not match on that generic word.
        self.assertIsNone(self.icon.from_desktop_entries(["messages"]))

    def test_generic_host_label_does_not_pick_another_apps_brand(self):
        # "mail" is what Gmail does, not who it is: it must not match "Proton
        # Mail" (brand "proton"). The Google Maps entry is a URL-scheme
        # handler, not an installed web app, so it cannot answer either.
        self.assertEqual(self.resolve("mail.google.com"), (None, "none"))

    def test_generic_host_label_falls_through_to_the_notification_image(self):
        artwork = self.root / "gmail-artwork.png"
        artwork.write_bytes(png_bytes(self.Image))
        hit, how = self.resolve("mail.google.com", str(artwork))
        self.assertEqual(how, "from_image")
        self.assertNotIn("proton", Path(hit).name)

    def test_maps_handler_is_not_an_installed_web_app(self):
        # A scheme handler with a host-shaped brand must not answer for that
        # host; only entries Chromium installed for a site ([--app-id=]) can.
        self.assertEqual(self.resolve("google.com"), (None, "none"))

    def test_bare_web_label_never_falls_back_to_the_browser(self):
        # The key says this is a web row. With no installed web app for
        # "google", neither the browser's own entry nor the maps handler gets
        # a turn through the app fallback.
        self.assertEqual(self.resolve("google"), (None, "none"))

    def test_installed_web_app_beats_the_notification_image(self):
        artwork = self.root / "site-artwork.png"
        artwork.write_bytes(png_bytes(self.Image))
        hit, how = self.resolve("proton.me", str(artwork))
        self.assertEqual(how, "from_desktop_entries:web")
        self.assertEqual(Path(hit).name, "proton.png")

    def test_notification_image_beats_the_theme_guess(self):
        artwork = self.root / "site-artwork.png"
        artwork.write_bytes(png_bytes(self.Image))
        # "unknown" exists in the icon theme, so only the ordering keeps the
        # site's own artwork ahead of the theme's guess.
        self.assertEqual(self.resolve("unknown.com")[1], "from_icon_theme:web")
        self.assertEqual(self.resolve("unknown.com", str(artwork))[1], "from_image")


class WebNotificationImage(unittest.TestCase):
    """--image is the notification's own artwork: untrusted, bounded, and
    copied into the cache before the browser's temp file can disappear."""

    def setUp(self):
        self.Image = require_pillow()
        self.temp = tempfile.TemporaryDirectory(prefix="omapager-web-image-")
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.icon = load_icon_module()
        patcher = mock.patch.dict(self.icon.__dict__, {"CACHE": str(self.cache)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temp.cleanup()

    def test_image_is_copied_into_the_cache_and_outlives_its_source(self):
        source = self.root / "icon.png"
        source.write_bytes(png_bytes(self.Image, (96, 96)))
        copied = self.icon.from_image(str(source))
        self.assertIsNotNone(copied)
        self.assertTrue(os.path.realpath(copied).startswith(
            os.path.realpath(str(self.cache)) + os.sep))
        source.unlink()
        self.assertTrue(os.path.isfile(copied), "cache copy must outlive the browser's temp file")
        with self.Image.open(copied) as image:
            self.assertEqual(image.format, "PNG")
            self.assertLessEqual(max(image.size), 128)

    def test_identical_pixels_dedupe_and_different_sites_do_not_collide(self):
        # Every Chromium site's artwork is called icon.png, so the cache copy
        # must be keyed by pixels, not by basename.
        one = self.root / "one" / "icon.png"
        two = self.root / "two" / "icon.png"
        one.parent.mkdir()
        two.parent.mkdir()
        one.write_bytes(png_bytes(self.Image, colour=(200, 30, 30, 255)))
        two.write_bytes(png_bytes(self.Image, colour=(30, 200, 30, 255)))
        first = self.icon.from_image(str(one))
        second = self.icon.from_image(str(two))
        self.assertNotEqual(first, second, "two sites' icon.png must not share a cache entry")
        self.assertEqual(first, self.icon.from_image(str(one)), "identical pixels are one cache entry")

    def test_cli_snapshots_the_image_argument(self):
        source = self.root / "artwork.png"
        source.write_bytes(png_bytes(self.Image))
        output = io.StringIO()
        with mock.patch.dict(self.icon.__dict__, {"APP_DIRS": [], "ICON_DIRS": []}), \
                contextlib.redirect_stdout(output):
            code = self.icon.main([
                "--key=web:example.com", "--source=example.com", "--app=Helium",
                "--image=" + str(source), "--scheme=dark", "--why"])
        self.assertEqual(code, 0)
        path, how = output.getvalue().split("\t")
        self.assertEqual(how, "from_image")
        self.assertTrue(os.path.isfile(path))
        source.unlink()
        self.assertTrue(os.path.isfile(path))

    def test_fifo_is_rejected_without_blocking(self):
        fifo = self.root / "icon.png"
        os.mkfifo(fifo)
        self.assertIsNone(self.icon.from_image(str(fifo)))
        self.assertIsNone(self.icon.from_image(str(fifo)))

    def test_symlink_oversize_directory_relative_and_missing_are_rejected(self):
        good = self.root / "good.png"
        good.write_bytes(png_bytes(self.Image))
        link = self.root / "link.png"
        link.symlink_to(good)
        big = self.root / "big.png"
        big.write_bytes(b"x" * (self.icon.REMOTE_ICON_BYTES + 1))
        directory = self.root / "dir.png"
        directory.mkdir()
        for path in (str(link), str(big), str(directory), "relative.png", "",
                     None, str(self.root / "missing.png")):
            with self.subTest(path=path):
                self.assertIsNone(self.icon.from_image(path))

    def test_non_raster_bytes_are_rejected(self):
        bad = self.root / "icon.png"
        bad.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")
        self.assertIsNone(self.icon.from_image(str(bad)))


class StoreIcon(unittest.TestCase):
    """The store records an icon after the card is gone: the live file while
    it exists, otherwise the newest history entry, without a migration
    stripping it back out."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="omapager-store-icon-")
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1")

    def tearDown(self):
        self.temp.cleanup()

    def run_store(self, *args, payload=None):
        return subprocess.run(
            [sys.executable, str(ROOT / "bin/omapager-store"), *args],
            input=json.dumps(payload) if payload is not None else "",
            text=True, capture_output=True, env=self.env)

    def test_icon_updates_live_then_newest_history_and_is_idempotent(self):
        state = self.home / ".local/state/omarchy/omapager"
        key = "web-proton-me"
        path = str(self.home / "icons" / "norm-proton.png")
        second = str(self.home / "icons" / "norm-proton-2.png")
        self.assertEqual(self.run_store("put", payload={"key": key, "app": "Proton Mail"}).returncode, 0)
        self.assertEqual(self.run_store("icon", key, path).returncode, 0)
        live = state / "live" / (key + ".json")
        self.assertEqual(json.loads(live.read_text())["stored_image"], path)
        # Idempotent: an unchanged answer rewrites nothing, and the migration
        # ensure() runs on every verb must keep the stored path.
        before = live.stat().st_mtime_ns
        self.assertEqual(self.run_store("icon", key, path).returncode, 0)
        self.assertEqual(live.stat().st_mtime_ns, before)
        self.assertEqual(self.run_store("close", key, "done").returncode, 0)
        history = sorted((state / "history").glob("*-" + key + ".json"))
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0].read_text())["stored_image"], path)
        # A late answer lands on the newest history entry for the key.
        self.assertEqual(self.run_store("icon", key, second).returncode, 0)
        self.assertEqual(json.loads(history[0].read_text())["stored_image"], second)
        listed = json.loads(self.run_store("history").stdout)
        self.assertEqual([row for row in listed if row["key"] == key][0]["stored_image"], second)

    def test_icon_rejects_bad_key_and_relative_path(self):
        self.assertEqual(self.run_store("restore").returncode, 0)
        self.assertNotEqual(self.run_store("icon", "../escape", "/tmp/icon.png").returncode, 0)
        self.assertNotEqual(self.run_store("icon", "key", "relative/icon.png").returncode, 0)
        self.assertEqual(self.run_store("icon", "key").returncode, 2)

    def test_icon_unknown_key_is_a_noop(self):
        self.assertEqual(self.run_store("restore").returncode, 0)
        self.assertEqual(self.run_store("icon", "missing", "/tmp/icon.png").returncode, 0)


if __name__ == "__main__":
    unittest.main()
