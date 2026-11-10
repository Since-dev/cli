"""The real world behind the CLI: the network, the folder, git, the terminal. The tests swap all of it for a fixture."""

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request


def _git(cwd, args):
    try:
        done = subprocess.run(["git"] + args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.decode("utf-8", "replace").strip() or None if done.returncode == 0 else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The API never redirects, so a redirect is never followed: a key goes to the host it was meant for and nowhere else."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _program():
    """The command that runs this again: through uvx or pipx run there is no `since` on the PATH."""
    script = (sys.argv[0] if sys.argv else "").replace("\\", "/")
    if "/uv/" in script and "/archive-" in script:
        return "uvx since-dev"
    if "/pipx/" in script and "/.cache/" in script:
        return "pipx run since-dev"
    return "since"


class RealIo:
    runtime = "python"

    def __init__(self):
        self.env = os.environ
        self.cwd = os.getcwd()
        self.home = os.path.expanduser("~")
        self.platform = "win32" if sys.platform.startswith("win") else sys.platform
        self.tty = sys.stdout.isatty()
        self.program = _program()

    def out(self, text):
        sys.stdout.write(text)
        sys.stdout.flush()

    def err(self, text):
        sys.stderr.write(text)
        sys.stderr.flush()

    def now(self):
        return int(time.time() * 1000)

    def sleep(self, ms):
        time.sleep(ms / 1000)

    def fetch(self, method, url, headers, body):
        request = urllib.request.Request(url, data=None if body is None else body.encode("utf-8"), headers=headers, method=method)
        try:
            with _OPENER.open(request, timeout=120) as response:
                return {"status": response.status, "retryAfter": response.headers.get("Retry-After"), "text": response.read().decode("utf-8", "replace")}
        except urllib.error.HTTPError as error:
            return {"status": error.code, "retryAfter": error.headers.get("Retry-After"), "text": error.read().decode("utf-8", "replace")}

    def origin(self):
        return _git(self.cwd, ["remote", "get-url", "origin"])

    def root(self):
        return _git(self.cwd, ["rev-parse", "--show-toplevel"]) or self.cwd

    def open(self, url):
        if self.env.get("CI"):
            return
        try:
            if self.platform == "darwin":
                command = ["open", url]
            elif self.platform == "win32":
                command = ["rundll32", "url.dll,FileProtocolHandler", url]
            else:
                command = ["xdg-open", url]
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        except OSError:
            pass  # No browser to open: the link is printed, which is enough.

    def prompt(self, question):
        sys.stderr.write(question)
        sys.stderr.flush()
        return sys.stdin.readline()

    def read_file(self, file):
        try:
            with open(file, encoding="utf-8") as handle:
                return handle.read()
        except (OSError, UnicodeDecodeError):
            return None

    def write_file(self, file, text, mode):
        """mode is None for a file of the repository, or the permission bits for a private one (its folder is made private too)."""
        folder = os.path.dirname(file)
        if folder:
            if mode is None:
                os.makedirs(folder, exist_ok=True)
            else:
                os.makedirs(folder, mode=0o700, exist_ok=True)
        if mode is None:
            with open(file, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            return
        descriptor = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.chmod(file, mode)

    def remove(self, file):
        try:
            os.remove(file)
            return True
        except OSError:
            return False

    def list(self, folder):
        """A folder's entries by name; a link is never followed into a folder."""
        try:
            with os.scandir(folder) as entries:
                return sorted(({"name": entry.name, "dir": entry.is_dir(follow_symlinks=False)} for entry in entries), key=lambda entry: entry["name"])
        except OSError:
            return []
