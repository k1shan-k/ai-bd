import asyncio

import app.worker as worker
import pytest
from app.worker import cycle_has_activity


def idle_result() -> dict:
    return {
        "expired_offer_reservations": 0,
        "enqueue": {
            "queued": 0,
            "cancelled": 0,
            "rescheduled": 0,
            "quota_deferred": 0,
            "llm_review_required": 0,
            "llm_failed": 0,
            "generation_recovered": 0,
            "generation_discarded": 0,
        },
        "dispatch": {"sent": 0, "cancelled": 0, "failed": 0},
    }


def test_idle_worker_cycle_has_no_activity():
    assert cycle_has_activity(idle_result()) is False


def test_worker_cycle_detects_each_activity_section():
    expired = idle_result()
    expired["expired_offer_reservations"] = 1
    assert cycle_has_activity(expired) is True

    enqueue = idle_result()
    enqueue["enqueue"]["queued"] = 1
    assert cycle_has_activity(enqueue) is True

    dispatch = idle_result()
    dispatch["dispatch"]["failed"] = 1
    assert cycle_has_activity(dispatch) is True


class StopServe(Exception):
    pass


async def blocked_listener():
    await asyncio.Future()


def test_serve_suppresses_idle_cycle_output(monkeypatch, capsys):
    async def idle_once(_limit):
        return idle_result()

    async def stop_after_cycle(_interval):
        raise StopServe

    monkeypatch.setattr(worker, "telegram_listener", blocked_listener)
    monkeypatch.setattr(worker, "run_once", idle_once)
    monkeypatch.setattr(worker.asyncio, "sleep", stop_after_cycle)
    monkeypatch.setattr(worker, "monotonic", iter([0, 1]).__next__)

    with pytest.raises(StopServe):
        asyncio.run(worker.serve(interval=15, limit=100))

    lines = capsys.readouterr().out.splitlines()
    assert lines == ["{'worker_started': True, 'interval_seconds': 15, 'limit': 100}"]


def test_serve_logs_active_cycle(monkeypatch, capsys):
    active = idle_result()
    active["enqueue"]["queued"] = 1

    async def active_once(_limit):
        return active

    async def stop_after_cycle(_interval):
        raise StopServe

    monkeypatch.setattr(worker, "telegram_listener", blocked_listener)
    monkeypatch.setattr(worker, "run_once", active_once)
    monkeypatch.setattr(worker.asyncio, "sleep", stop_after_cycle)
    monkeypatch.setattr(worker, "monotonic", iter([0, 1]).__next__)

    with pytest.raises(StopServe):
        asyncio.run(worker.serve(interval=15, limit=100))

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert "'worker_started': True" in lines[0]
    assert "'queued': 1" in lines[1]
