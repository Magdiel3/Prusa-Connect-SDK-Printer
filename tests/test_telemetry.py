import pytest  # type: ignore
import requests  # noqa

from prusa.connect.printer import Telemetry, const
from prusa.connect.printer.connection import Connection

FINGERPRINT = "__fingerprint__"
SERVER = "http://server"


@pytest.fixture()
def connection():
    return Connection(SERVER, FINGERPRINT)


def test_telemetry(requests_mock, connection):
    requests_mock.post(SERVER + "/p/telemetry", status_code=204)

    Telemetry(const.State.READY)(connection)
    Telemetry(const.State.READY, 1)(connection)
    Telemetry(const.State.BUSY, axis_x=3.1)(connection)
