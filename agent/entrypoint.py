"""Container start-up: make the data folder usable, give up root, start the server.

The container starts as root only long enough to hand the data volume (which Docker may have created
owned by root, for example by an earlier version of the image) to an unprivileged user. Then the process
becomes that user and starts uvicorn. If the container is already started as a normal user (for example
`docker run --user 1000`), it just starts the server.
"""
import os

UID = GID = 10001  # an unprivileged user that exists only as a number
PORT = "9000"


def data_dir() -> str:
    return os.path.dirname(os.getenv("DATABASE_PATH", "/data/agent.db")) or "."


def hand_over(directory: str, uid: int = UID, gid: int = GID) -> int:
    """Give the folder and everything in it to uid:gid. Returns how many items were changed."""
    os.makedirs(directory, exist_ok=True)
    changed = 0
    for root, dirs, files in os.walk(directory):
        for name in [".", *dirs, *files]:
            path = root if name == "." else os.path.join(root, name)
            os.chown(path, uid, gid)
            changed += 1
    return changed


def drop_privileges(uid: int = UID, gid: int = GID) -> None:
    os.setgroups([])  # no leftover group memberships from root
    os.setgid(gid)
    os.setuid(uid)


def main() -> None:
    if os.getuid() == 0:
        hand_over(data_dir())
        drop_privileges()
    os.execvp("uvicorn", ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", PORT])


if __name__ == "__main__":
    main()
