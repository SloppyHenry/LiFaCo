"""Start-up code of every plugin process. Run as:  python3 -I bootstrap.py '<json>'

Uses only the standard library. It runs as root only for the few lines that drop privileges, then it becomes the
unprivileged plugin user and can never get root back:

  1. drop group and user  2. no_new_privs (setuid programs can no longer raise privileges)
  3. resource limits      4. import and run the plugin

stdout is reserved for the protocol: fd 1 is moved to a private descriptor and print() goes to stderr.
"""

import json
import os
import resource
import runpy
import sys


def _no_new_privs():
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(38, 1, 0, 0, 0) != 0:   # PR_SET_NO_NEW_PRIVS
            raise OSError(ctypes.get_errno(), "prctl failed")
    except (OSError, AttributeError, ImportError) as e:
        sys.stderr.write(f"warning: could not set no_new_privs: {e}\n")


def main():
    cfg = json.loads(sys.argv[1])
    if cfg.get("uid") is not None and os.geteuid() == 0:
        os.setgroups(cfg["groups"])
        os.setgid(cfg["gid"])
        os.setuid(cfg["uid"])
        try:
            os.setuid(0)
        except PermissionError:
            pass
        else:
            sys.stderr.write("fatal: privileges could not be dropped\n")
            os._exit(70)
    _no_new_privs()
    for name, value in (("RLIMIT_CORE", 0), ("RLIMIT_NOFILE", 256), ("RLIMIT_NPROC", 128)):
        try:
            resource.setrlimit(getattr(resource, name), (value, value))
        except (ValueError, OSError):
            pass

    proto = os.dup(1)
    os.dup2(2, 1)
    os.environ["LIFACO_PROTO_FD"] = str(proto)

    plugin_dir = cfg["plugin_dir"]
    os.chdir(plugin_dir)
    # Plugins must not be able to import LiFaCo's own modules that live next to this file.
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if p and os.path.abspath(p) != here]
    sys.path[:0] = [cfg["sdk_dir"], plugin_dir, os.path.join(plugin_dir, "lib")]
    sys.argv = [cfg["entry"]] + cfg.get("args", [])
    runpy.run_path(os.path.join(plugin_dir, cfg["entry"]), run_name="__main__")


if __name__ == "__main__":
    main()
