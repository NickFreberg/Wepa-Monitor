"""Rolling deploys: a new copy waits for the old copy's collector lock instead of never collecting."""
import threading
import time

from wepa_monitor import wsgi


def test_new_copy_takes_over_when_old_copy_exits(tmp_path):
    old = open(tmp_path / "collector.lock", "w")
    new = open(tmp_path / "collector.lock", "w")
    assert wsgi._take_lock(old, wait=False)
    assert not wsgi._take_lock(new, wait=False)          # old copy is collecting

    got = []
    t = threading.Thread(target=lambda: got.append(wsgi._take_lock(new, wait=True)))
    t.start()
    time.sleep(0.3)
    assert not got                                       # still waiting
    old.close()                                          # old copy exits
    t.join(5)
    assert got == [True]
