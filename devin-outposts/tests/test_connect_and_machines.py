from __future__ import annotations

import base64
import hashlib
import threading
import time
import urllib.parse
import urllib.request

import pytest
from fake_outposts import TOKEN

from prized_outposts.cli import machines_main, minutes
from prized_outposts.devin import connect as c
from prized_outposts.machines import Machines, MachineSpec, label_pairs


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_pkce_pair_is_s256():
    verifier, challenge = c.pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert (
        challenge
        == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


def test_connect_url_carries_the_challenge_and_callback():
    url = c.connect_url(
        "abc", callback_url="http://localhost:8977/prized/devin/callback", outpost_name="prized"
    )
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert url.startswith("https://app.devin.ai/outposts/connect?")
    assert q["code_challenge"] == ["abc"] and q["platform"] == ["linux"] and q["outpost_name"] == ["prized"]
    assert q["callback_url"] == ["http://localhost:8977/prized/devin/callback"]


def test_the_whole_flow_against_the_fake_token_endpoint(fake):
    verifier, challenge = c.pkce_pair()
    fake.codes["one-time"] = challenge
    port = free_port()
    callback = f"http://localhost:{port}/prized/devin/callback"
    got: dict[str, str] = {}
    listener = threading.Thread(target=lambda: got.update(code=c.wait_for_code(callback, timeout=10)))
    listener.start()
    for _ in range(50):  # the admin's browser follows Devin's redirect
        try:
            urllib.request.urlopen(f"{callback}?code=one-time", timeout=2).read()
            break
        except OSError:
            time.sleep(0.1)
    listener.join(10)
    conn = c.exchange(got["code"], verifier, token_url=f"{fake.url}/outposts/connection-token")
    assert conn.outpost_id == "outpost_env-connected" and conn.access_token == TOKEN
    assert TOKEN not in repr(conn)
    with pytest.raises(c.ConnectError):  # single use
        c.exchange("one-time", verifier, token_url=f"{fake.url}/outposts/connection-token")


def test_a_code_without_the_matching_verifier_is_refused(fake):
    _, challenge = c.pkce_pair()
    fake.codes["stolen"] = challenge
    with pytest.raises(c.ConnectError, match="refused"):
        c.exchange(
            "stolen", "not-the-verifier-" + "x" * 40, token_url=f"{fake.url}/outposts/connection-token"
        )


def test_the_listener_only_binds_loopback():
    with pytest.raises(c.ConnectError):
        c.wait_for_code("https://example.com/callback", timeout=0.1)


def test_machines_resume_the_session_they_paused(world):
    m = Machines(MachineSpec(labels={"task": "t"}), owner="o", sandbox_api=world.api())
    made = m.ensure({"cursor-worker": "w1"})
    assert made.how == "created"
    assert m.hibernate({"cursor-worker": "w1"}).state == "paused"
    again = m.ensure({"cursor-worker": "w1"})
    assert (again.how, again.sandbox.id) == ("resumed", made.sandbox.id)
    assert m.ensure({"cursor-worker": "w2"}).sandbox.id != made.sandbox.id
    assert m.destroy({"cursor-worker": "w1"}) == [made.sandbox.id]
    assert m.find({"cursor-worker": "w1"}) is None
    with pytest.raises(ValueError):
        m.destroy({})


def test_sweep_never_touches_warm_parents(world):
    m = Machines(MachineSpec(), owner="o", sandbox_api=world.api())
    parent = m.make_parent(lambda sb: None)
    m.ensure({"k": "1"})
    assert len(m.sweep(lambda sb: False)) == 1
    assert world.sandboxes[parent.id].state == "paused"


def test_a_failed_warm_up_deletes_the_parent(world):
    m = Machines(MachineSpec(), owner="o", sandbox_api=world.api())

    def broken(sb):
        raise RuntimeError("setup failed")

    with pytest.raises(RuntimeError):
        m.make_parent(broken)
    assert all(sb.state == "deleted" for sb in world.sandboxes.values())


def test_prized_machines_cli(world, monkeypatch, capsys):

    monkeypatch.setenv("PRIZED_API_KEY", "dcp_000fake")
    original = Machines.__init__

    def init(self, spec, *, owner, **kw):
        original(self, spec, owner=owner, sandbox_api=world.api())

    monkeypatch.setattr(Machines, "__init__", init)
    assert machines_main(["ensure", "--key", "cursor-worker=w9", "--label", "cursor-pool=prized"]) == 0
    assert '"how": "created"' in capsys.readouterr().out
    assert machines_main(["hibernate", "--key", "cursor-worker=w9"]) == 0
    assert '"state": "paused"' in capsys.readouterr().out
    assert machines_main(["ensure", "--key", "cursor-worker=w9"]) == 0
    assert '"how": "resumed"' in capsys.readouterr().out


def test_durations_and_labels():
    assert minutes("7d", None) == 10080 and minutes("2h", None) == 120 and minutes("90", None) == 90
    assert minutes("600s", None) == 10 and minutes(None, 15) == 15 and minutes("0", 5) == 0
    assert label_pairs(["a=b", "c=d=e"]) == {"a": "b", "c": "d=e"}
    with pytest.raises(ValueError):
        label_pairs(["nope"])
