"""Drive state hand-off logic against an in-memory fake Drive (no network)."""
from __future__ import annotations

import sqlite3

import pytest

import catalog.__main__ as main_mod
from catalog import gdrive


class FakeDrive(gdrive.Drive):
    def __init__(self):  # no credentials / HTTP
        self.files: dict[str, dict] = {}
        self.blobs: dict[str, bytes] = {}
        self.n = 0
        self.clock = 0

    def close(self):
        pass

    def _new(self, name, props):
        self.n += 1
        self.clock += 1
        fid = f"f{self.n}"
        self.files[fid] = {"id": fid, "name": name, "createdTime": f"2026-01-01T00:00:{self.clock:02d}Z",
                           "appProperties": {k: str(v) for k, v in props.items()}}
        return fid

    def list(self, name_q):
        if "lock.json" in name_q:
            return [dict(f) for f in self.files.values() if f["name"] == "lock.json"]
        return [dict(f) for f in self.files.values() if f["name"].startswith("state-g")]

    def delete(self, file_id):
        self.files.pop(file_id, None)
        self.blobs.pop(file_id, None)

    def download(self, f, dest):
        dest.write_bytes(self.blobs[f["id"]])

    def upload(self, src, name, props):
        fid = self._new(name, props)
        self.blobs[fid] = src.read_bytes()
        self.files[fid]["size"] = str(len(self.blobs[fid]))
        return self.files[fid]

    # lock creation goes through req() in the real class
    def acquire_lock(self, owner):
        import time
        now = int(time.time())
        for f in list(self.list("name = 'lock.json'")):
            if int(f["appProperties"]["expires"]) < now:
                self.delete(f["id"])
        others = [f for f in self.list("name = 'lock.json'") if f["appProperties"]["owner"] != owner]
        if others:
            raise gdrive.LockedError(others[0]["appProperties"]["owner"])
        return self._new("lock.json", {"owner": owner, "since": now, "expires": now + gdrive.LOCK_TTL})


def _make_db(path, version, marker):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row  # like catalog.db.connect()
    c.executescript("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
                    "CREATE TABLE catalog_version(version INTEGER);"
                    "CREATE TABLE file(path TEXT);")
    c.execute("INSERT INTO catalog_version VALUES (?)", (version,))
    c.execute("INSERT INTO file VALUES (?)", (marker,))
    c.commit()
    return c


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(gdrive, "DATA", tmp_path)
    monkeypatch.setattr(main_mod, "DATA", tmp_path)
    return tmp_path


def _marker(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT path FROM file").fetchone()[0], c.execute(
            "SELECT value FROM meta WHERE key='state_generation'").fetchone()
    finally:
        c.close()


def test_push_then_pull_on_a_fresh_device(data, tmp_path_factory):
    d = FakeDrive()
    pc = _make_db(tmp_path_factory.mktemp("pc") / "state.db", 8, "from-pc")
    up = gdrive.push(d, pc, "pc")
    assert up == {"generation": 1, "catalog_version": 8, "removed": 0}
    assert d.head()["name"] == "state-g000001-v8.db.gz"

    info = gdrive.pull_if_newer(d)  # data/ has no state.db yet
    assert info["pulled"] and info["catalog_version"] == "8"
    assert _marker(data / "state.db") == ("from-pc", ("1",))


def test_no_pull_when_local_is_current_and_pull_when_behind(data, tmp_path_factory):
    d = FakeDrive()
    phone = _make_db(data / "state.db", 8, "phone-copy")
    gdrive.push(d, phone, "phone")          # generation 1, phone is current
    phone.close()
    assert gdrive.pull_if_newer(d)["pulled"] is False

    pc = _make_db(tmp_path_factory.mktemp("pc") / "state.db", 9, "pc-newer")
    pc.execute("INSERT INTO meta VALUES ('state_generation', '1')")
    pc.commit()
    assert gdrive.push(d, pc, "pc")["generation"] == 2
    info = gdrive.pull_if_newer(d)
    assert info["pulled"] and info["local_generation"] == 1 and info["drive_generation"] == 2
    assert _marker(data / "state.db") == ("pc-newer", ("2",))
    assert len(list(data.glob("state.db.bak-*"))) == 1   # old state kept, but only one backup


def test_keeps_only_recent_generations(data, tmp_path_factory, monkeypatch):
    monkeypatch.setattr(gdrive, "KEEP", 3)
    d = FakeDrive()
    c = _make_db(tmp_path_factory.mktemp("pc") / "state.db", 8, "x")
    for _ in range(5):
        gdrive.push(d, c, "pc")
    assert [s["generation"] for s in d.states()] == [5, 4, 3]


def test_checksum_mismatch_leaves_local_state(data, tmp_path_factory):
    d = FakeDrive()
    c = _make_db(tmp_path_factory.mktemp("pc") / "state.db", 8, "x")
    gdrive.push(d, c, "pc")
    d.head()  # tamper with the recorded checksum
    fid = next(iter(k for k, f in d.files.items() if f["name"].startswith("state-g")))
    d.files[fid]["appProperties"]["sha256"] = "0" * 64
    local = _make_db(data / "state.db", 7, "local")
    local.close()
    with pytest.raises(gdrive.DriveError):
        gdrive.pull_if_newer(d)
    assert _marker(data / "state.db")[0] == "local"


def test_lock_blocks_the_other_device(data):
    d = FakeDrive()
    lock = d.acquire_lock("phone")
    with pytest.raises(gdrive.LockedError):
        d.acquire_lock("pc")
    d.release_lock(lock)
    d.acquire_lock("pc")
