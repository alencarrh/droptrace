"""Only one sampler may write to a database at a time."""

from __future__ import annotations

from droptrace.guard import InstanceLock


def test_a_second_lock_on_the_same_database_is_refused(tmp_path):
    db = tmp_path / "droptrace.db"
    first = InstanceLock(db)
    assert first.acquire() is None          # we got it

    second = InstanceLock(db)
    holder = second.acquire()
    assert holder is not None, "a second sampler was allowed to start"
    assert holder.isdigit() or holder == "unknown"
    second.release()                        # must not steal the lock

    # Still held by the first.
    assert InstanceLock(db).acquire() is not None

    first.release()
    # Now it is free again.
    third = InstanceLock(db)
    assert third.acquire() is None
    third.release()


def test_different_databases_do_not_collide(tmp_path):
    a = InstanceLock(tmp_path / "a.db")
    b = InstanceLock(tmp_path / "b.db")
    assert a.acquire() is None
    assert b.acquire() is None
    a.release()
    b.release()


def test_release_is_idempotent(tmp_path):
    lock = InstanceLock(tmp_path / "droptrace.db")
    lock.acquire()
    lock.release()
    lock.release()
    assert InstanceLock(tmp_path / "droptrace.db").acquire() is None


def test_the_holder_is_named_so_the_message_can_say_who(tmp_path):
    import os

    db = tmp_path / "droptrace.db"
    held = InstanceLock(db)
    assert held.acquire() is None
    # Opening for the check must not wipe the holder's pid.
    assert InstanceLock(db).acquire() == str(os.getpid())
    assert InstanceLock(db).acquire() == str(os.getpid())
    held.release()
