# -*- coding: utf-8 -*-
"""Windows ConPTY와 POSIX PTY 백엔드."""
import os

from shells import build_posix_exec_argv


class WinPty:
    def __init__(self, shell, cwd, env, cols, rows):
        from winpty import PtyProcess
        self._p = PtyProcess.spawn(shell, cwd=cwd, env=env, dimensions=(rows, cols))

    def read(self):
        try:
            return self._p.read(65536)
        except EOFError:
            return ""

    def write(self, data: str):
        self._p.write(data)

    def resize(self, cols, rows):
        try:
            self._p.setwinsize(rows, cols)
        except Exception:
            pass

    def alive(self):
        try:
            return self._p.isalive()
        except Exception:
            return False

    def kill(self):
        try:
            self._p.terminate(force=True)
        except Exception:
            pass


class PosixPty:
    def __init__(self, shell, cwd, env, cols, rows):
        import codecs
        import fcntl
        import pty
        import struct
        import termios

        self._struct = struct
        self._fcntl = fcntl
        self._termios = termios
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(cwd)
            except Exception:
                pass
            for k, v in (env or {}).items():
                if k not in ["TERM_SESSION_ID", "TERM_PROGRAM", "TERM_PROGRAM_VERSION"]:
                    os.environ[k] = v
            os.environ.setdefault("TERM", "xterm-256color")
            shell_path, argv = build_posix_exec_argv(shell)
            os.execv(shell_path, argv)
            os._exit(1)
        self._pid = pid
        self._fd = fd
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.resize(cols, rows)

    def read(self):
        try:
            data = os.read(self._fd, 65536)
        except OSError:
            return ""
        if not data:
            return ""
        return self._dec.decode(data)

    def write(self, data: str):
        try:
            os.write(self._fd, data.encode("utf-8"))
        except OSError:
            pass

    def resize(self, cols, rows):
        try:
            winsize = self._struct.pack("HHHH", rows, cols, 0, 0)
            self._fcntl.ioctl(self._fd, self._termios.TIOCSWINSZ, winsize)
        except Exception:
            pass

    def alive(self):
        try:
            pid, _ = os.waitpid(self._pid, os.WNOHANG)
            return pid == 0
        except Exception:
            return False

    def kill(self):
        try:
            import signal
            os.kill(self._pid, signal.SIGKILL)
        except Exception:
            pass
