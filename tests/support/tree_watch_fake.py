"""An in-memory Nextcloud tree for the tree watcher's consumers (#939).

`FakeTree` answers PROPFIND the way Nextcloud does for the bot's own storage:
a folder's ETag changes whenever anything below it changes, which
`tests/full/test_tree_watch.py` measured against Nextcloud 30.
`install` puts it behind `tree_watch._propfind` and replaces the rc's two
entry points, which record what they were asked.
"""

from __future__ import annotations

import itertools

from istota.nextcloud import rclone_rc, tree_watch
from istota.nextcloud.rclone_rc import RcResult

_ids = itertools.count(100)


class FakeTree:
    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.calls: list[tuple[str, int]] = []
        self.fail = False
        self.root_mount_type = ""
        self.mkdir("/")

    def _etag(self) -> str:
        return f"e{next(_ids)}"

    def _propagate(self, path: str) -> None:
        while True:
            self.nodes[path]["etag"] = self._etag()
            if path == "/":
                return
            path = path.rsplit("/", 1)[0] or "/"

    def mkdir(self, path: str) -> None:
        self.nodes[path] = {"is_dir": True, "fileid": str(next(_ids)), "etag": self._etag()}
        if path != "/":
            self._propagate(path.rsplit("/", 1)[0] or "/")

    def write(self, path: str) -> None:
        if path not in self.nodes:
            self.nodes[path] = {"is_dir": False, "fileid": str(next(_ids)), "etag": ""}
        self._propagate(path)

    def remove(self, path: str) -> None:
        for p in [p for p in self.nodes if p == path or p.startswith(path + "/")]:
            del self.nodes[p]
        self._propagate(path.rsplit("/", 1)[0] or "/")

    def propfind(self, config, path, depth):
        self.calls.append((path or "/", depth))
        if self.fail:
            raise OSError("nextcloud down")
        path = tree_watch._norm(path)
        if path not in self.nodes:
            raise FileNotFoundError(path)

        def entry(p):
            node = self.nodes[p]
            return {
                "path": p, "name": p.rsplit("/", 1)[-1], "is_dir": node["is_dir"],
                "etag": node["etag"], "fileid": node["fileid"],
                "owner_id": node.get("owner", "istota"),
                "mount_type": self.root_mount_type if p == "/" else "",
            }

        out = [entry(path)]
        if depth == 1:
            for p in sorted(self.nodes):
                if p != path and (p.rsplit("/", 1)[0] or "/") == path:
                    out.append(entry(p))
        return out



def install(monkeypatch, users=("alice", "bob"), bot_dir="istota") -> FakeTree:
    tree_watch.reset()
    fake = FakeTree()
    fake.mkdir("/Users")
    for user in users:
        for p in (f"/Users/{user}", f"/Users/{user}/{bot_dir}", f"/Users/{user}/{bot_dir}/config",
                  f"/Users/{user}/{bot_dir}/scripts", f"/Users/{user}/{bot_dir}/memories"):
            fake.mkdir(p)
        fake.write(f"/Users/{user}/{bot_dir}/config/TASKS.md")
    fake.mkdir("/Channels")
    monkeypatch.setattr(tree_watch, "_propfind", fake.propfind)
    fake.forgotten = []
    fake.refreshed = []
    fake.rc_ok = True

    def forget(paths):
        fake.forgotten.append(list(paths))
        return RcResult(fake.rc_ok, "" if fake.rc_ok else "unreachable")

    def refresh(paths):
        fake.refreshed.append(list(paths))
        return RcResult(fake.rc_ok, "" if fake.rc_ok else "unreachable")

    def forget_all():
        fake.forgotten.append(["*"])
        return RcResult(fake.rc_ok, "" if fake.rc_ok else "unreachable")

    monkeypatch.setattr(rclone_rc, "forget_dirs", forget)
    monkeypatch.setattr(rclone_rc, "forget_all", forget_all)
    monkeypatch.setattr(rclone_rc, "refresh_dirs", refresh)
    return fake
