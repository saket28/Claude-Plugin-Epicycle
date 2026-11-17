"""After the merge: remove the task's checkout and, if asked, its branch."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import PASS, config, reply, request  # noqa: E402


def main():
    req = request()
    root, s = Path(req["root"]), req["settings"]
    try:
        if s["cleanup_worktree"]:
            config.git("worktree", "remove", "--force", req["worktree"], cwd=root, check=False)
        if s["delete_branch"]:
            config.git("branch", "-D", req["branch"], cwd=root, check=False)   # after the worktree is gone
    except Exception:  # noqa: BLE001 - cleanup never fails a task
        pass
    reply(PASS, summary="cleaned up")


main()
