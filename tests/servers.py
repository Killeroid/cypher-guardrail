"""The FalkorDB the tests run against: a Docker container (the default), a server that is already running, or FalkorDBLite.

``CYPHER_GUARDRAIL_TEST_BACKEND`` chooses:

* ``docker``: start ``FALKORDB_IMAGE`` with testcontainers, and remove it at the end.
* ``server``: use the server at ``FALKORDB_HOST`` (and ``FALKORDB_PORT``, 6379 by default). The tests create and delete
  graphs on it, so it should be one that may be written to.
* ``lite``: the embedded FalkorDBLite (Python >= 3.12).
* ``auto`` (the default): ``server`` if ``FALKORDB_HOST`` is set, else ``docker`` if Docker answers, else ``lite``.
"""

import contextlib
import os
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import falkordb

BACKEND_VAR = "CYPHER_GUARDRAIL_TEST_BACKEND"
IMAGE = os.environ.get("FALKORDB_IMAGE", "falkordb/falkordb:6.0.3")
PORT = 6379
READY_WITHIN = 60  # seconds


class Unavailable(Exception):
    """No FalkorDB could be reached; the message says why."""


@dataclass
class Server:
    client: falkordb.FalkorDB
    # What ``falkordb.FalkorDB`` and ``falkordb.asyncio.FalkorDB`` take to connect to the same server
    connection: dict
    description: str


def major_version(client: falkordb.FalkorDB) -> str:
    """The major version of the FalkorDB behind ``client`` ("4", "6")."""
    return str(graph_version(client) // 10000)


def graph_version(client: falkordb.FalkorDB) -> int:
    """The version of the graph module of the FalkorDB behind ``client``: 41803 is 4.18.3, 60003 is 6.0.3."""
    for module in client.connection.execute_command("MODULE", "LIST"):
        # a list of names and values, or a dict of them (the protocol that FalkorDBLite speaks)
        fields = (
            module
            if isinstance(module, dict)
            else dict(zip(module[::2], module[1::2], strict=False))
        )
        if fields.get("name") in ("graph", b"graph"):
            return int(fields["ver"])
    raise Unavailable("the graph module of FalkorDB is not loaded")


def _wait_until_ready(connection: dict) -> falkordb.FalkorDB:
    deadline = time.monotonic() + READY_WITHIN
    while True:
        try:
            client = falkordb.FalkorDB(**connection)
            client.connection.ping()
        except Exception as error:
            if time.monotonic() > deadline:
                raise Unavailable(f"FalkorDB did not answer: {error}") from error
            time.sleep(0.5)
        else:
            return client


def _docker_answers() -> bool:
    from docker.errors import DockerException
    from testcontainers.core.container import DockerClient

    try:
        DockerClient().client.ping()
    except (DockerException, OSError):
        return False
    return True


@contextlib.contextmanager
def _docker() -> Generator[Server]:
    from testcontainers.core.container import DockerContainer

    with DockerContainer(IMAGE).with_exposed_ports(PORT) as container:
        connection = {
            "host": container.get_container_host_ip(),
            "port": int(container.get_exposed_port(PORT)),
        }
        client = _wait_until_ready(connection)
        try:
            yield Server(client, connection, f"Docker image {IMAGE}")
        finally:
            client.close()


@contextlib.contextmanager
def _server() -> Generator[Server]:
    connection = {
        "host": os.environ["FALKORDB_HOST"],
        "port": int(os.environ.get("FALKORDB_PORT", PORT)),
    }
    client = _wait_until_ready(connection)
    try:
        yield Server(client, connection, "{host}:{port}".format(**connection))
    finally:
        client.close()


@contextlib.contextmanager
def _lite(folder: Path) -> Generator[Server]:
    try:
        from redislite.falkordb_client import FalkorDB
    except ImportError as error:
        raise Unavailable("falkordblite (Python >= 3.12) is not installed") from error
    client = FalkorDB(str(folder / "cypher_guardrail.db"))
    try:
        connection = {"unix_socket_path": client.client.socket_file}
        yield Server(client, connection, "FalkorDBLite")
    finally:
        client.close()


def _chosen() -> str:
    backend = os.environ.get(BACKEND_VAR, "auto")
    if backend != "auto":
        return backend
    if os.environ.get("FALKORDB_HOST"):
        return "server"
    return "docker" if _docker_answers() else "lite"


@contextlib.contextmanager
def running_falkordb(folder: Path) -> Generator[Server]:
    """The FalkorDB chosen by the environment; ``folder`` is where an embedded one keeps its file."""
    backend = _chosen()
    opened = {
        "docker": _docker,
        "server": _server,
        "lite": lambda: _lite(folder),
    }.get(backend)
    if opened is None:
        raise Unavailable(
            f"{BACKEND_VAR} is {backend!r}: it is auto, docker, server or lite"
        )
    with contextlib.ExitStack() as stack:
        try:
            server = stack.enter_context(opened())
        except Unavailable:
            raise
        except Exception as error:
            if backend != "docker":
                raise
            raise Unavailable(f"Docker could not start {IMAGE}: {error}") from error
        yield server
