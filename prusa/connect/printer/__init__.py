"""Python printer library for Prusa Connect."""

import configparser
import os
import re
from logging import getLogger
from queue import Queue, Empty
from time import time, sleep
from typing import Optional, List, Any, Callable, Dict, Union

from requests import Session, RequestException
# pylint: disable=redefined-builtin
from requests.exceptions import ConnectionError

from . import const, errors
from .command import Command
from .files import Filesystem, InotifyHandler, delete
from .metadata import get_metadata
from .models import Event, Telemetry
from .clock import ClockWatcher
from .download import DownloadMgr
from .util import RetryingSession

__version__ = "0.6.0.dev0"
__date__ = "14 Jul 2021"  # version date
__copyright__ = "(c) 2021 Prusa 3D"
__author_name__ = "Ondřej Tůma"
__author_email__ = "ondrej.tuma@prusa3d.cz"
__author__ = f"{__author_name__} <{__author_email__}>"
__description__ = "Python printer library for Prusa Connect"

__credits__ = "Ondřej Tůma, Martin Užák, Michal Zoubek, Tomáš Jozífek"
__url__ = "https://github.com/prusa3d/Prusa-Connect-SDK-Printer"

# pylint: disable=invalid-name
# pylint: disable=too-few-public-methods
# pylint: disable=too-many-arguments
# pylint: disable=too-many-instance-attributes
# NOTE: Temporary for pylint with python3.9
# pylint: disable=unsubscriptable-object

CODE_TIMEOUT = 60 * 30  # 30 min

log = getLogger("connect-printer")
re_conn_reason = re.compile(r"] (.*)")

__all__ = ["Printer"]

CommandArgs = Optional[List[Any]]


class Register:
    """Item for get_token action."""
    def __init__(self, code):
        self.code = code
        self.timeout = int(time()) + CODE_TIMEOUT


def default_register_handler(token):
    """Default register handler.

    It blocks communication with Connect in loop method!
    """
    assert token


class Printer:
    """Printer representation object.

    To process inotify_handler, please create your own thread
    calling printer.inotify_handler() in a loop.
    """
    # pylint: disable=too-many-public-methods

    queue: "Queue[Union[Event, Telemetry, Register]]"
    server: Optional[str] = None
    token: Optional[str] = None
    conn: Session

    NOT_INITIALISED_MSG = "Printer has not been initialized properly"

    def __init__(self,
                 type_: const.PrinterType = None,
                 sn: str = None,
                 fingerprint: str = None,
                 max_retries: int = 1):
        self.__type = type_
        self.__sn = sn
        self.__fingerprint = fingerprint
        self.firmware = None
        self.network_info = {
            "lan_mac": None,
            "lan_ipv4": None,
            "lan_ipv6": None,
            "wifi_mac": None,
            "wifi_ipv4": None,
            "wifi_ipv6": None,
            "wifi_ssid": None,
            "hostname": None,
            "username": None,
            "digest": None
        }
        self.api_key = None

        self.__checked = False
        self.__state = const.State.BUSY
        self.job_id = None

        if max_retries > 1:
            self.conn = RetryingSession(max_retries=max_retries)
        else:
            self.conn = Session()

        self.queue = Queue()

        self.command = Command(self.event_cb)
        self.set_handler(const.Command.SEND_INFO, self.send_info)
        self.set_handler(const.Command.SEND_FILE_INFO, self.get_file_info)
        self.set_handler(const.Command.CREATE_DIRECTORY, self.create_directory)
        self.set_handler(const.Command.DELETE_FILE, self.delete_file)
        self.set_handler(const.Command.DELETE_DIRECTORY, self.delete_directory)
        self.set_handler(const.Command.START_DOWNLOAD, self.download_start)
        self.set_handler(const.Command.STOP_DOWNLOAD, self.download_stop)
        self.set_handler(const.Command.SEND_DOWNLOAD_INFO, self.download_info)
        self.set_handler(const.Command.SET_PRINTER_PREPARED,
                         self.set_printer_prepared)

        self.fs = Filesystem(sep=os.sep, event_cb=self.event_cb)
        self.inotify_handler = InotifyHandler(self.fs)
        # Handler blocks communication with Connect in loop method!
        self.register_handler = default_register_handler

        self.clock_watcher = ClockWatcher()

        if self.token and not self.is_initialised():
            log.warning(self.NOT_INITIALISED_MSG)

        self.download_mgr = DownloadMgr(self.fs, self.get_connection_details,
                                        self.event_cb, self.printed_file_cb)

        self.__running_loop = False

    @staticmethod
    def connect_url(host: str, tls: bool, port: int = 0):
        """Format url from settings value.

        >>> Printer.connect_url('connect', True)
        'https://connect'
        >>> Printer.connect_url('connect', False)
        'http://connect'
        >>> Printer.connect_url('connect', False, 8000)
        'http://connect:8000'
        """
        protocol = 'https' if tls else 'http'
        if port:
            return f"{protocol}://{host}:{port}"
        return f"{protocol}://{host}"

    @property
    def checked(self):
        """Return checked flag.

        Checked flag can be set with set_state method. It is additional
        flag for READY state, which has info about user confirmation
        *ready to print*.
        """
        return self.__checked

    @property
    def state(self):
        """Return printer state."""
        return self.__state

    @property
    def fingerprint(self):
        """Return printer fingerprint."""
        return self.__fingerprint

    @fingerprint.setter
    def fingerprint(self, value):
        """Set fingerprint if is not set."""
        if self.__fingerprint is not None:
            raise RuntimeError("Fingerprint is already set.")
        self.__fingerprint = value

    @property
    def sn(self):
        """Return printer serial number"""
        return self.__sn

    @sn.setter
    def sn(self, value):
        """Set serial number if is not set."""
        if self.__sn is not None:
            raise RuntimeError("Serial number is already set.")
        self.__sn = value

    @property
    def type(self):
        """Return printer type"""
        return self.__type

    @type.setter
    def type(self, value):
        """Set the printer type if is not set."""
        if self.__type is not None:
            raise RuntimeError("Printer type is already set.")
        self.__type = value

    def is_initialised(self):
        """Return True if the printer is initialised"""
        initialised = bool(self.__sn and self.__fingerprint
                           and self.__type is not None)
        if not initialised:
            errors.API.ok = False
        return initialised

    def make_headers(self, timestamp: float = None) -> dict:
        """Return request headers from connection variables."""
        timestamp = timestamp or int(time() * 10) * const.TIMESTAMP_PRECISION

        headers = {
            "Fingerprint": self.fingerprint,
            "Timestamp": str(timestamp)
        }
        if self.token:
            headers['Token'] = self.token

        if self.clock_watcher.clock_adjusted():
            log.debug("Clock adjustment detected. Resetting watcher")
            headers['Clock-Adjusted'] = "1"
            self.clock_watcher.reset()

        return headers

    def set_state(self,
                  state: const.State,
                  source: const.Source,
                  checked: bool = None,
                  **kwargs):
        """Set printer state and push event about that to queue.

        :source: the initiator of printer state
        :checked: If state is PRINTING, checked argument is ignored,
            and flag is set to False.
        """
        if state == const.State.PRINTING:
            self.__checked = False
        elif checked is not None:
            self.__checked = checked
        self.__state = state
        self.event_cb(const.Event.STATE_CHANGED,
                      source,
                      state=state,
                      checked=self.__checked,
                      **kwargs)

    def event_cb(self,
                 event: const.Event,
                 source: const.Source,
                 timestamp: float = None,
                 command_id: int = None,
                 **kwargs) -> None:
        """Create event and push it to queue."""
        if not self.token:
            log.debug("Skipping event, no token: %s", event.value)
            return
        if self.job_id:
            kwargs['job_id'] = self.job_id
        event_ = Event(event, source, timestamp, command_id, **kwargs)
        log.debug("Putting event to queue: %s", event_)
        if not self.is_initialised():
            log.warning("Printer fingerprint and/or SN is not set")
        self.queue.put(event_)

    def telemetry(self,
                  state: const.State,
                  timestamp: float = None,
                  **kwargs) -> None:
        """Create telemetry end push it to queue."""
        if not self.token:
            log.debug("Skipping telemetry, no token.")
            return
        if self.job_id:
            kwargs['job_id'] = self.job_id
        if self.download_mgr.current:
            download = self.download_mgr.current
            kwargs['download_progress'] = download.progress
            kwargs['download_time_remaining'] = download.time_remaining()
            kwargs['download_bytes'] = download.downloaded
        if self.is_initialised():
            telemetry = Telemetry(state, timestamp, **kwargs)
        else:
            telemetry = Telemetry(state, timestamp)
            log.warning("Printer fingerprint and/or SN is not set")
        self.queue.put(telemetry)

    def set_connection(self, path: str):
        """Set connection from ini config."""
        if not os.path.exists(path):
            raise FileNotFoundError(f"ini file: `{path}` doesn't exist")
        config = configparser.ConfigParser()
        config.read(path)

        host = config['connect']['address']
        tls = config['connect'].getboolean('tls')
        port = config['connect'].getint('port', fallback=0)
        self.server = Printer.connect_url(host, tls, port)
        self.token = config['connect']['token']
        errors.TOKEN.ok = True

    def get_connection_details(self):
        """Return currently set server and token"""
        return (self.server, self.token)

    def get_info(self) -> Dict[str, Any]:
        """Return kwargs for Command.finish method as reaction to SEND_INFO."""
        # pylint: disable=unused-argument
        if self.__type is not None:
            type_, ver, sub = self.__type.value
        else:
            type_, ver, sub = (None, None, None)
        return dict(source=const.Source.CONNECT,
                    event=const.Event.INFO,
                    state=self.__state,
                    checked=self.__checked,
                    type=type_,
                    version=ver,
                    subversion=sub,
                    firmware=self.firmware,
                    sdk=__version__,
                    network_info=self.network_info,
                    api_key=self.api_key,
                    files=self.fs.to_dict(),
                    sn=self.sn,
                    fingerprint=self.fingerprint)

    def send_info(self, caller: Command) -> Dict[str, Any]:
        """Accept command arguments and adapt the call for the getter"""
        # pylint: disable=unused-argument
        return self.get_info()

    def download_start(self, caller: Command) -> Dict[str, Any]:
        """Download an URL specified by url, to_select and to_print flags
        in `caller`"""
        if not caller.args or len(caller.args) != 4:
            raise ValueError(f"{const.Command.START_DOWNLOAD} requires "
                             f"four args (url, dst, select, print)")

        url, destination, to_select, to_print = caller.args
        self.download_mgr.start(url,
                                destination,
                                to_select=to_select,
                                to_print=to_print)

        return dict(source=const.Source.CONNECT)

    def download_stop(self, caller: Command) -> Dict[str, Any]:
        """Stop current download, if any"""
        # pylint: disable=unused-argument
        self.download_mgr.stop()
        return dict(source=const.Source.CONNECT)

    def download_info(self, caller: Command) -> Dict[str, Any]:
        """Provide info on the running download"""
        # pylint: disable=unused-argument
        info = self.download_mgr.info()
        info['source'] = const.Source.CONNECT
        info['event'] = const.Event.DOWNLOAD_INFO
        return info

    def set_printer_prepared(self, caller: Command) -> Dict[str, Any]:
        """Set PREPARED state"""
        # pylint: disable=unused-argument
        self.__state = const.State.PREPARED
        return dict(source=const.Source.CONNECT,
                    event=const.Event.STATE_CHANGED,
                    state=self.__state,
                    checked=self.__checked)

    def get_file_info(self, caller: Command) -> Dict[str, Any]:
        """Return file info for a given file, if it exists."""
        # pylint: disable=unused-argument
        if not caller.args:
            raise ValueError("SEND_FILE_INFO requires args")

        path = caller.args[0]
        node = self.fs.get(path)
        if node is None:
            raise ValueError(f"File does not exist: {path}")

        if node.is_dir:
            raise ValueError("FILE_INFO doesn't work for directories")

        info = dict(
            source=const.Source.CONNECT,
            event=const.Event.FILE_INFO,
            path=path,
        )

        try:
            path_ = os.path.split(self.fs.get_os_path(path))
            if not path_[1].startswith("."):
                meta = get_metadata(self.fs.get_os_path(path))
                info.update(node.attrs)
                info.update(meta.data)

                # include the biggest thumbnail, if available
                if meta.thumbnails:
                    biggest = b""
                    for _, data in meta.thumbnails.items():
                        if len(data) > len(biggest):
                            biggest = data
                    info['preview'] = biggest.decode()
        except FileNotFoundError:
            log.debug("File not found: %s", path)

        return info

    def delete_file(self, caller: Command) -> Dict[str, Any]:
        """Handler for delete file."""
        if not caller.args:
            raise ValueError(f"{caller.command} requires args")

        abs_path = self.inotify_handler.get_abs_os_path(caller.args[0])

        delete(abs_path, False)

        return dict(source=const.Source.CONNECT)

    def delete_directory(self, caller: Command) -> Dict[str, Any]:
        """Handler for delete directory."""
        if not caller.args:
            raise ValueError(f"{caller.command} requires args")

        abs_path = self.inotify_handler.get_abs_os_path(caller.args[0])

        delete(abs_path, True)

        return dict(source=const.Source.CONNECT)

    def create_directory(self, caller: Command) -> Dict[str, Any]:
        """Handler for create directory."""
        if not caller.args:
            raise ValueError(f"{caller.command} requires args")

        relative_path_parameter = caller.args[0]
        abs_path = self.inotify_handler.get_abs_os_path(
            relative_path_parameter)

        os.makedirs(abs_path)
        return dict(source=const.Source.CONNECT)

    def set_handler(self, command: const.Command,
                    handler: Callable[[Command], Dict[str, Any]]):
        """Set handler for command.

        Handler must return **kwargs dictionary for Command.finish method,
        which means that source must be set at least.
        """
        self.command.handlers[command] = handler

    def handler(self, command: const.Command):
        """Wrap function to handle command.

        Handler must return **kwargs dictionary for Command.finish method,
        which means that source must be set at least.

        .. code:: python

            @printer.command(const.GCODE)
            def gcode(prn, gcode):
                ...
        """
        def wrapper(handler: Callable[[Command], Dict[str, Any]]):
            self.set_handler(command, handler)
            return handler

        return wrapper

    def parse_command(self, res):
        """Parse telemetry response.

        When response from connect is command (HTTP Status: 200 OK), it
        will set command object, if the printer is initialized properly.
        """
        if res.status_code == 200:
            command_id: Optional[int] = None
            try:
                command_id = int(res.headers.get("Command-Id"))
            except (TypeError, ValueError):
                log.error("Invalid Command-Id header: %s",
                          res.headers.get("Command-Id"))
                self.event_cb(const.Event.REJECTED,
                              const.Source.CONNECT,
                              reason="Invalid Command-Id header")
                return res

            if not self.is_initialised():
                self.event_cb(const.Event.REJECTED,
                              const.Source.WUI,
                              command_id=command_id,
                              reason=self.NOT_INITIALISED_MSG)
                return res

            content_type = res.headers.get("content-type")
            log.debug("parse_command res: %s", res.text)
            try:
                if content_type.startswith("application/json"):
                    data = res.json()
                    if self.command.check_state(command_id):
                        self.command.accept(command_id,
                                            data.get("command",
                                                     ""), data.get("args"),
                                            data.get('kwargs'))
                elif content_type == "text/x.gcode":
                    if self.command.check_state(command_id):
                        force = ("Force" in res.headers
                                 and res.headers["Force"] == "1")
                        self.command.accept(command_id,
                                            const.Command.GCODE.value,
                                            [res.text],
                                            force=force)
                else:
                    raise ValueError("Invalid command content type")
            except Exception as e:  # pylint: disable=broad-except
                log.exception("")
                self.event_cb(const.Event.REJECTED,
                              const.Source.CONNECT,
                              command_id=command_id,
                              reason=str(e))
        elif res.status_code == 204:  # no cmd in telemetry
            pass
        else:
            log.info("Got unexpected telemetry response (%s): %s",
                     res.status_code, res.text)
        return res

    def register(self):
        """Register the printer with Connect and return a registration
        temporary code, or fail with a RuntimeError."""
        if not self.server:
            raise RuntimeError("Server is not set")

        data = {
            "sn": self.sn,
            "fingerprint": self.fingerprint,
            "type": self.__type.value[0],
            "version": self.__type.value[1],
            "subversion": self.__type.value[2],
            "firmware": self.firmware
        }
        res = self.conn.post(self.server + "/p/register",
                             headers=self.make_headers(),
                             json=data,
                             timeout=const.CONNECTION_TIMEOUT)
        if res.status_code == 200:
            code = res.headers['Temporary-Code']
            self.queue.put(Register(code))
            errors.API.ok = True
            return code

        errors.HTTP.ok = True
        errors.API.ok = False
        if res.status_code >= 500:
            errors.HTTP.ok = False
        log.debug("Status code: {res.status_code}")
        raise RuntimeError(res.text)

    def get_token(self, tmp_code):
        """Prepare request and return response for GET /p/register."""
        if not self.server:
            raise RuntimeError("Server is not set")

        headers = self.make_headers()
        headers["Temporary-Code"] = tmp_code
        return self.conn.get(self.server + "/p/register",
                             headers=headers,
                             timeout=const.CONNECTION_TIMEOUT)

    def loop(self):
        """This method is responsible for communication with Connect.

        In a loop it gets an item (Event or Telemetry) from queue and sets
        Printer.command object, when the command is in the answer to telemetry.
        """
        # pylint: disable=too-many-branches
        # pylint: disable=too-many-statements
        self.__running_loop = True
        while self.__running_loop:
            try:
                item = self.queue.get(timeout=const.TIMESTAMP_PRECISION)
                if not self.server:
                    log.warning("Server is not set, skipping item from queue")
                    continue

                if isinstance(item, Telemetry) and self.token:
                    headers = self.make_headers(item.timestamp)
                    log.debug("Sending telemetry: %s", item)
                    res = self.conn.post(self.server + '/p/telemetry',
                                         headers=headers,
                                         json=item.to_payload(),
                                         timeout=const.CONNECTION_TIMEOUT)
                    log.debug("Telemetry response: %s", res.text)
                    self.parse_command(res)
                elif isinstance(item, Event) and self.token:
                    log.debug("Sending event: %s", item)
                    headers = self.make_headers(item.timestamp)
                    res = self.conn.post(self.server + '/p/events',
                                         headers=headers,
                                         json=item.to_payload(),
                                         timeout=const.CONNECTION_TIMEOUT)
                    log.debug("Event response: %s", res.text)
                elif isinstance(item, Register):
                    log.debug("Getting token")
                    res = self.get_token(item.code)
                    log.debug("Get register response: (%d) %s",
                              res.status_code, res.text)
                    if res.status_code == 200:
                        self.token = res.headers["Token"]
                        errors.TOKEN.ok = True
                        log.info("New token was set.")
                        self.register_handler(self.token)
                    elif res.status_code == 202 and item.timeout > time():
                        self.queue.put(item)
                        sleep(1)
                else:
                    log.debug("Item `%s` not sent, probably token isn't set.",
                              item)
                    continue  # No token - no communication

                errors.API.ok = True

                if res.status_code >= 400:
                    errors.API.ok = False
                    if res.status_code == 401:
                        errors.TOKEN.ok = False
            except Empty:
                continue
            except ConnectionError as err:
                errors.HTTP.ok = False
                log.error(err)
            except RequestException as err:
                errors.INTERNET.ok = False
                log.error(err)
            except Exception:  # pylint: disable=broad-except
                errors.INTERNET.ok = False
                log.exception('Unhandled error')

    def stop_loop(self):
        """Set internal variable, to stop the loop method."""
        self.__running_loop = False

    def mount(self, dirpath: str, mountpoint: str):
        """Create a listing of `dirpath` and mount it under `mountpoint`.

        This requires linux kernel with inotify support enabled to work.
        """
        self.fs.from_dir(dirpath, mountpoint)
        self.inotify_handler = InotifyHandler(self.fs)

    def unmount(self, mountpoint: str):
        """unmount `mountpoint`.

        This requires linux kernel with inotify support enabled to work.
        """
        self.fs.unmount(mountpoint)
        self.inotify_handler = InotifyHandler(self.fs)

    # pylint: disable=no-self-use
    def printed_file_cb(self):
        """Return the absolute path of the currently printed file
        This method shall be implemented by the clients that use SDK.
        """
        return None
