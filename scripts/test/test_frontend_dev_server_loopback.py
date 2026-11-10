"""The local frontend servers bind loopback only (#740).

Next.js listens on all interfaces unless it is given a hostname. Local
development runs with the dev-only ``NEXT_PUBLIC_SKIP_AUTH`` bypass, whose chat
proxy forwards unauthenticated requests to the loopback backend, so an
all-interfaces frontend would expose the local agent to the network. These
checks keep every ``next dev`` and ``next start`` launch in ``ui/`` on
127.0.0.1, and keep Playwright probing the address the server actually binds.
"""

# Standard library
import json
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
UI_DIR = PROJECT_ROOT / "ui"
PACKAGE_JSON = UI_DIR / "package.json"
PLAYWRIGHT_CONFIGS = sorted(UI_DIR.glob("playwright*.config.ts"))

LOOPBACK_HOST = re.compile(r"(?:--hostname|-H)[ =]127\.0\.0\.1(?:\s|$)")
NEXT_SERVER = re.compile(r"\bnext (?:dev|start)\b")
WEB_SERVER_BLOCK = re.compile(r"^\s*webServer\s*:", re.MULTILINE)
WEB_SERVER_COMMAND = re.compile(r"\bcommand:\s*'([^']*)'")
WEB_SERVER_URL = re.compile(r"\burl:\s*'([^']*)'")


class FrontendDevServerLoopbackTests(unittest.TestCase):
    def setUp(self):
        self.scripts = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["scripts"]

    def test_package_scripts_that_start_next_bind_loopback(self):
        launches = {name: command for name, command in self.scripts.items() if NEXT_SERVER.search(command)}
        self.assertIn("dev", launches)
        self.assertIn("start", launches)
        for name, command in launches.items():
            with self.subTest(script=name):
                self.assertRegex(command, LOOPBACK_HOST, f"`npm run {name}` must pass --hostname 127.0.0.1")

    def test_playwright_web_servers_bind_and_probe_loopback(self):
        self.assertTrue(PLAYWRIGHT_CONFIGS, "expected Playwright configs under ui/")
        for config in PLAYWRIGHT_CONFIGS:
            text = config.read_text(encoding="utf-8")
            if not WEB_SERVER_BLOCK.search(text):
                continue
            commands = WEB_SERVER_COMMAND.findall(text)
            urls = WEB_SERVER_URL.findall(text)
            with self.subTest(config=config.name):
                self.assertTrue(commands, "webServer.command must be a single-quoted string literal")
                self.assertTrue(urls, "webServer.url must be a single-quoted string literal")
            for command in commands:
                with self.subTest(config=config.name, command=command):
                    script = re.fullmatch(r"npm run (\S+)", command)
                    resolved = self.scripts[script.group(1)] if script else command
                    self.assertRegex(resolved, LOOPBACK_HOST)
            for url in urls:
                with self.subTest(config=config.name, url=url):
                    self.assertTrue(url.startswith("http://127.0.0.1:"), f"{url} must target the loopback bind")


if __name__ == "__main__":
    unittest.main()
