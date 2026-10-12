import pytest
from fastapi import HTTPException

from app.routers.broadcasts import BroadcastIn, _validate


def test_accept_interval():
    _validate(BroadcastIn(message="hi", interval_minutes=60))


def test_accept_time():
    _validate(BroadcastIn(message="hi", time_hhmm="12:30"))


def test_reject_no_schedule():
    with pytest.raises(HTTPException):
        _validate(BroadcastIn(message="hi"))


def test_reject_empty_message():
    with pytest.raises(HTTPException):
        _validate(BroadcastIn(message="   ", interval_minutes=60))


def test_reject_bad_time():
    with pytest.raises(HTTPException):
        _validate(BroadcastIn(message="hi", time_hhmm="25:99"))
