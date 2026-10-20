#!/usr/bin/env python3
"""Generic file transfer tool (Python rewrite of file_tran.pl).

Copies or moves files from a local or SFTP source to one or more target
directories, with optional health checks (file date, stable size, control
file), gzip/gunzip, archiving, logging and e-mail notification on errors.

Usage:
    file-transfer.py /path/to/job.param
    file-transfer.py /path/to/job.param --check      # show expanded FileMask/FileCreationDate and exit
    file-transfer.py /path/to/job.param --now "2026-05-01 08:00:00"   # pretend it is another time
"""
from __future__ import annotations

import argparse
import calendar
import fcntl
import gzip
import hashlib
import html
import logging
import os
import posixpath
import re
import shutil
import smtplib
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional, Set, Tuple

VERSION = "1.2.0"
PARAM_SUFFIX = ".param"
TMP_PREFIX = "tmp_"  # downstream readers must ignore files with this prefix
RANGE_SEPARATOR = re.compile(r"&date", re.IGNORECASE)


class ConfigError(Exception):
    """Invalid or missing job configuration."""


class TransferError(Exception):
    """A transfer step failed."""


# --------------------------------------------------------------------------
# Date mini-language:  <DD-5>  <MM+1>  <YYYY>  ...
# --------------------------------------------------------------------------

_PART = r"(?:YYYY|YY|MM|DD|HH|MI|SS)"
# Block form: <FORMAT[+-N unit]>, e.g. <YYYYMMDD-5d>, <YYYY-MM-DD HH-2h>. The whole date is shifted once.
_BLOCK_RE = re.compile(
    rf"<\s*({_PART}(?:[-_ :./T]*{_PART})*)\s*(?:([+-])\s*(\d+)\s*(y|mo|d|h|mi|s))?\s*>", re.IGNORECASE
)
_BLOCK_UNITS = {"y": "YYYY", "mo": "MM", "d": "DD", "h": "HH", "mi": "MI", "s": "SS"}
_PART_RE = re.compile(_PART, re.IGNORECASE)
# Single-token form: <DD-5> shifts the date by 5 DAYS and prints only the day part.
_TOKEN_RE = re.compile(r"<\s*(YYYY|YY|MM|DD|HH|MI|SS)\s*(?:([+-])\s*(\d+))?\s*>", re.IGNORECASE)
_TOKEN_LIKE_RE = re.compile(r"<\s*(?:YYYY|YY|MM|DD|HH|MI|SS)[^>]*>", re.IGNORECASE)


def shift_months(moment: datetime, months: int) -> datetime:
    total = moment.year * 12 + (moment.month - 1) + months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def shift(moment: datetime, unit: str, amount: int) -> datetime:
    """Shift the whole date by `amount` of `unit`; month ends are respected."""
    if unit in ("YYYY", "YY"):
        return shift_months(moment, amount * 12)
    if unit == "MM":
        return shift_months(moment, amount)
    deltas = {
        "DD": timedelta(days=amount),
        "HH": timedelta(hours=amount),
        "MI": timedelta(minutes=amount),
        "SS": timedelta(seconds=amount),
    }
    return moment + deltas[unit]


def render_part(moved: datetime, unit: str) -> str:
    if unit == "YYYY":
        return f"{moved.year:04d}"
    if unit == "YY":
        return f"{moved.year % 100:02d}"
    part = {"MM": moved.month, "DD": moved.day, "HH": moved.hour, "MI": moved.minute, "SS": moved.second}[unit]
    return f"{part:02d}"


def _shifted(now: datetime, unit: str, amount: int, token: str) -> datetime:
    try:
        return shift(now, unit, amount)
    except (ValueError, OverflowError) as exc:
        raise ConfigError(f"Date token '{token}' is out of range: {exc}")


def expand_date_tokens(text: str, now: datetime) -> str:
    """Expand date tokens.

    Block form  <FORMAT-Nu>  shifts the whole date once, then formats it: <YYYYMMDD-5d>, <YYYY-MM-DD+1mo>.
    Single form <UNIT-N>     shifts by N of that unit and prints only that part: <DD-5>.
    """

    def replace_block(match: "re.Match[str]") -> str:
        moved = now
        if match.group(2):
            amount = int(match.group(2) + match.group(3))
            moved = _shifted(now, _BLOCK_UNITS[match.group(4).lower()], amount, match.group(0))
        return _PART_RE.sub(lambda part: render_part(moved, part.group(0).upper()), match.group(1))

    def replace_token(match: "re.Match[str]") -> str:
        unit = match.group(1).upper()
        amount = int(match.group(2) + match.group(3)) if match.group(2) else 0
        return render_part(_shifted(now, unit, amount, match.group(0)), unit)

    expanded = _TOKEN_RE.sub(replace_token, _BLOCK_RE.sub(replace_block, text))
    leftover = _TOKEN_LIKE_RE.search(expanded)
    if leftover:
        raise ConfigError(
            f"Invalid date token '{leftover.group(0)}' (expected <YYYY|YY|MM|DD|HH|MI|SS> with optional +N/-N, "
            "or a block like <YYYYMMDD-5d> with unit y, mo, d, h, mi or s)"
        )
    return expanded


# --------------------------------------------------------------------------
# File date filter (FileDateCheck / FileCreationDate)
# --------------------------------------------------------------------------

_DATE_RE = re.compile(
    r"^(\d{4})(?:-(\d{2})(?:-(\d{2})(?:[ T](\d{2})(?:[-:](\d{2})(?:[-:](\d{2}))?)?)?)?)?$"
)
_LEVEL_DEFAULTS = [None, 1, 1, 0, 0, 0]  # year, month, day, hour, minute, second


def parse_date_spec(text: str) -> Tuple[datetime, int]:
    """Return (start of the period, level); level 0=year ... 5=second."""
    match = _DATE_RE.match(text.strip())
    if not match:
        raise ConfigError(
            f"Invalid date '{text}' (expected YYYY[-MM[-DD[ HH[-MI[-SS]]]]])"
        )
    parts = [int(group) for group in match.groups() if group is not None]
    values = parts + _LEVEL_DEFAULTS[len(parts):]
    try:
        return datetime(*values), len(parts) - 1
    except ValueError as exc:
        raise ConfigError(f"Invalid date '{text}': {exc}")


def truncate(moment: datetime, level: int) -> datetime:
    kept = [moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second]
    return datetime(*(kept[: level + 1] + _LEVEL_DEFAULTS[level + 1:]))


@dataclass
class DateFilter:
    start: datetime
    start_level: int
    end: Optional[datetime] = None
    end_level: int = 0

    def matches(self, moment: datetime) -> bool:
        if self.end is None:
            return truncate(moment, self.start_level) == self.start
        return truncate(moment, self.start_level) >= self.start and truncate(moment, self.end_level) <= self.end


def build_date_filter(expanded_spec: str) -> DateFilter:
    pieces = RANGE_SEPARATOR.split(expanded_spec)
    if len(pieces) == 1:
        start, level = parse_date_spec(pieces[0])
        return DateFilter(start, level)
    if len(pieces) == 2:
        start, start_level = parse_date_spec(pieces[0])
        end, end_level = parse_date_spec(pieces[1])
        if end < start:
            raise ConfigError(f"FileCreationDate range ends before it starts: '{expanded_spec}'")
        return DateFilter(start, start_level, end, end_level)
    raise ConfigError(f"FileCreationDate must hold one date or two dates separated by &date: '{expanded_spec}'")


# --------------------------------------------------------------------------
# Configuration (.param file)
# --------------------------------------------------------------------------

def load_params(path: str) -> Dict[str, str]:
    params: Dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition(":")  # values may contain ':'
            if not separator:
                raise ConfigError(f"{path} line {number}: expected key:value")
            params[key.strip()] = value.strip()
    return params


def _text(params: Dict[str, str], key: str, default: Optional[str] = None) -> str:
    value = params.get(key, "")
    if value == "":
        if default is None:
            raise ConfigError(f"Missing parameter {key}")
        return default
    return value


def _flag(params: Dict[str, str], key: str, default: Optional[str] = None) -> bool:
    value = _text(params, key, default).upper()
    if value not in ("Y", "N"):
        raise ConfigError(f"Parameter {key} must be Y or N, got '{params.get(key)}'")
    return value == "Y"


def _split_list(value: str) -> List[str]:
    return [item.strip() for item in re.split(r"[,;]", value) if item.strip()]


@dataclass
class Config:
    param_file: str
    input_dir: str
    output_dirs: List[str]
    file_mask_raw: str
    file_mask: "re.Pattern[str]"
    file_mask_expanded: str
    keep_input: bool = True
    sftp: bool = False
    sftp_type: str = ""
    sftp_host: str = ""
    sftp_port: int = 22
    sftp_user: str = ""
    sftp_pass_file: str = ""
    sftp_key: str = ""
    sftp_strict_host_key: bool = True
    date_check: bool = False
    date_spec_raw: str = ""
    date_spec_expanded: str = ""
    date_filter: Optional[DateFilter] = None
    sub_dir_check: bool = False
    size_check: bool = False
    size_check_delay: int = 20
    ctl_check: bool = False
    ctl_extension: str = ""
    gzip_output: bool = False
    gzip_input: bool = False
    archive: bool = False
    archive_dir: str = ""
    log_mode: str = "Full"
    log_dir: str = ""
    log_file: str = ""
    notification: bool = False
    notification_sender: str = ""
    notification_to: List[str] = field(default_factory=list)
    notification_cc: List[str] = field(default_factory=list)
    notification_server: str = ""
    notification_port: int = 25

    @classmethod
    def from_params(cls, param_file: str, params: Dict[str, str], now: datetime) -> "Config":
        mask_raw = _text(params, "FileMask")
        mask_expanded = expand_date_tokens(mask_raw, now)
        try:
            mask = re.compile(mask_expanded)
        except re.error as exc:
            raise ConfigError(f"FileMask '{mask_expanded}' is not a valid regular expression: {exc}")

        cfg = cls(
            param_file=param_file,
            input_dir=_text(params, "InputDir"),
            output_dirs=_split_list(_text(params, "OutputDir")),
            file_mask_raw=mask_raw,
            file_mask=mask,
            file_mask_expanded=mask_expanded,
            keep_input=_flag(params, "KeepInput", "Y"),
            sftp=_flag(params, "SftpCheck", "N"),
            sub_dir_check=_flag(params, "SubDirCheck", "N"),
            size_check=_flag(params, "SizeCheck", "N"),
            ctl_check=_flag(params, "CtlFileCheck", "N"),
            gzip_output=_flag(params, "GzipOutput", "N"),
            gzip_input=_flag(params, "GzipInput", "N"),
            archive=_flag(params, "ArchiveCheck", "N"),
            date_check=_flag(params, "FileDateCheck", "N"),
            notification=_flag(params, "Notification", "N"),
        )
        if not cfg.output_dirs:
            raise ConfigError("OutputDir has no directory")
        if cfg.gzip_input and cfg.gzip_output:
            raise ConfigError("GzipInput and GzipOutput cannot both be Y")

        if cfg.sftp:
            cfg.sftp_type = _text(params, "SftpType").capitalize()
            cfg.sftp_host = _text(params, "SftpHost")
            cfg.sftp_user = _text(params, "SftpUser")
            cfg.sftp_port = int(_text(params, "SftpPort", "22"))
            cfg.sftp_strict_host_key = _flag(params, "SftpStrictHostKey", "Y")
            if cfg.sftp_type == "Clear":
                cfg.sftp_pass_file = _text(params, "SftpPassFile")
            elif cfg.sftp_type == "Key":
                cfg.sftp_key = _text(params, "SftpKey")
            else:
                raise ConfigError(f"SftpType must be Clear or Key, got '{cfg.sftp_type}'")

        if cfg.date_check:
            cfg.date_spec_raw = _text(params, "FileCreationDate")
            cfg.date_spec_expanded = expand_date_tokens(cfg.date_spec_raw, now)
            cfg.date_filter = build_date_filter(cfg.date_spec_expanded)

        if cfg.size_check:
            cfg.size_check_delay = int(_text(params, "SizeCheckDelay", "20"))
        if cfg.ctl_check:
            cfg.ctl_extension = _text(params, "CtlFileExtension").lstrip(".")
        if cfg.archive:
            cfg.archive_dir = _text(params, "ArchiveDir")

        cfg.log_mode = _text(params, "LogMode", "Full").capitalize()
        if cfg.log_mode not in ("Full", "Basic", "None"):
            raise ConfigError(f"LogMode must be Full, Basic or None, got '{cfg.log_mode}'")
        if cfg.log_mode != "None":
            cfg.log_dir = _text(params, "LogDir")
            cfg.log_file = _text(params, "LogFile")

        if cfg.notification:
            method = _text(params, "NotificationMethod", "smtp").lower()
            if method != "smtp":
                raise ConfigError(f"NotificationMethod must be smtp, got '{method}'")
            cfg.notification_sender = _text(params, "NotificationSender")
            cfg.notification_to = _split_list(_text(params, "NotificationRe"))
            cfg.notification_cc = _split_list(_text(params, "NotificationCc", " ").strip())
            cfg.notification_server = _text(params, "NotificationServer")
            cfg.notification_port = int(_text(params, "NotificationPort", "25"))
            if not cfg.notification_to:
                raise ConfigError("NotificationRe has no recipient")
        return cfg


def validate_paths(cfg: Config) -> None:
    """Check local directories once logging (and error notification) is available."""
    for directory in cfg.output_dirs + ([cfg.archive_dir] if cfg.archive else []):
        if not os.path.isdir(directory):
            raise ConfigError(f"Target directory does not exist: {directory}")
        if not os.access(directory, os.W_OK):
            raise ConfigError(f"Target directory is not writable: {directory}")
    if not cfg.sftp:
        if not os.path.isdir(cfg.input_dir):
            raise ConfigError(f"InputDir does not exist: {cfg.input_dir}")
        source_real = os.path.realpath(cfg.input_dir)
        for directory in cfg.output_dirs:
            if os.path.realpath(directory) == source_real:
                raise ConfigError(f"OutputDir {directory} is the same as InputDir")


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

class LogFormatter(logging.Formatter):
    """`pid: dd/mm/yy HH:MM:SS [ message ]` - same layout as the Perl tool."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.levelno >= logging.WARNING:
            message = f"{record.levelname}: {message}"
        return f"{record.process}: {self.formatTime(record, '%d/%m/%y %H:%M:%S')} [ {message} ]"


class ErrorCollector(logging.Handler):
    """Remembers error messages so they can be sent in the notification e-mail."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def setup_logging(cfg: Config) -> Tuple[logging.Logger, ErrorCollector]:
    log = logging.getLogger("file_transfer")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()

    if cfg.log_mode != "None":
        if not os.path.isdir(cfg.log_dir):
            raise ConfigError(f"LogDir does not exist: {cfg.log_dir}")
        file_handler = logging.FileHandler(os.path.join(cfg.log_dir, cfg.log_file), encoding="utf-8")
        file_handler.setLevel(logging.DEBUG if cfg.log_mode == "Full" else logging.INFO)
        file_handler.setFormatter(LogFormatter())
        log.addHandler(file_handler)

    stderr_handler = logging.StreamHandler(sys.stderr)  # cron mails stderr
    stderr_handler.setLevel(logging.ERROR)
    stderr_handler.setFormatter(LogFormatter())
    log.addHandler(stderr_handler)

    collector = ErrorCollector()
    log.addHandler(collector)
    return log, collector


# --------------------------------------------------------------------------
# Sources: local directory or SFTP server
# --------------------------------------------------------------------------

@dataclass
class FileInfo:
    name: str
    path: str
    size: int
    mtime: float


class LocalSource:
    pathmod = os.path

    def list_files(self, root: str, recursive: bool) -> List[FileInfo]:
        walker = os.walk(root) if recursive else [next(os.walk(root))]
        found: List[FileInfo] = []
        for dirpath, dirnames, filenames in walker:
            dirnames.sort()
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                if not os.path.isfile(path):
                    continue
                try:
                    info = os.stat(path)
                except OSError:
                    continue  # vanished between listing and stat
                found.append(FileInfo(name, path, info.st_size, info.st_mtime))
        return found

    def stat(self, path: str) -> Optional[Tuple[int, float]]:
        try:
            info = os.stat(path)
        except OSError:
            return None
        return info.st_size, info.st_mtime

    def materialize(self, info: FileInfo, staging_dir: str) -> Tuple[str, bool]:
        return info.path, False

    def remove(self, path: str) -> None:
        os.unlink(path)

    def close(self) -> None:
        pass


class SftpSource:
    pathmod = posixpath

    def __init__(self, cfg: Config, log: logging.Logger) -> None:
        try:
            import paramiko  # imported here so local jobs do not need it
        except ImportError:
            raise TransferError("SFTP needs the 'paramiko' package (pip install paramiko)")
        self._counter = 0
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(
            paramiko.RejectPolicy() if cfg.sftp_strict_host_key else paramiko.AutoAddPolicy()
        )
        options = dict(
            hostname=cfg.sftp_host,
            port=cfg.sftp_port,
            username=cfg.sftp_user,
            timeout=30,
            allow_agent=False,
            look_for_keys=False,
        )
        if cfg.sftp_type == "Clear":
            log.debug("SFTP Clear connection, reading password file %s", cfg.sftp_pass_file)
            options["password"] = self._read_password(cfg.sftp_pass_file, log)
        else:
            if not os.path.isfile(cfg.sftp_key):
                raise TransferError(f"SFTP key file not found: {cfg.sftp_key}")
            log.debug("SFTP Key connection, using key file %s", cfg.sftp_key)
            options["key_filename"] = cfg.sftp_key
        try:
            client.connect(**options)
            self._sftp = client.open_sftp()
        except Exception as exc:
            raise TransferError(f"Error in SFTP connection to {cfg.sftp_host}: {exc}")
        self._client = client
        log.info("SFTP connection established to %s", cfg.sftp_host)

    @staticmethod
    def _read_password(path: str, log: logging.Logger) -> str:
        if not os.path.isfile(path):
            raise TransferError(f"SFTP pass file not found: {path}")
        if os.stat(path).st_mode & 0o077:
            log.warning("SFTP pass file %s is readable by other users; run chmod 600 on it", path)
        with open(path, encoding="utf-8") as handle:
            lines = [line.rstrip("\r\n") for line in handle if line.strip()]
        if not lines:
            raise TransferError(f"SFTP pass file is empty: {path}")
        if len(lines) > 1:
            log.warning("SFTP pass file %s has multiple lines, only the first is used", path)
        return lines[0]

    def list_files(self, root: str, recursive: bool) -> List[FileInfo]:
        found: List[FileInfo] = []
        pending = [root]
        while pending:
            directory = pending.pop(0)
            try:
                entries = self._sftp.listdir_attr(directory)
            except IOError as exc:
                raise TransferError(f"Cannot list {directory} on SFTP: {exc}")
            for entry in sorted(entries, key=lambda item: item.filename):
                path = posixpath.join(directory, entry.filename)
                if stat.S_ISDIR(entry.st_mode or 0):
                    if recursive:
                        pending.append(path)
                elif stat.S_ISREG(entry.st_mode or 0):
                    found.append(FileInfo(entry.filename, path, entry.st_size or 0, float(entry.st_mtime or 0)))
        return found

    def stat(self, path: str) -> Optional[Tuple[int, float]]:
        try:
            info = self._sftp.stat(path)
        except IOError:
            return None
        return info.st_size or 0, float(info.st_mtime or 0)

    def materialize(self, info: FileInfo, staging_dir: str) -> Tuple[str, bool]:
        self._counter += 1
        local_path = os.path.join(staging_dir, f"{self._counter}_{info.name}")
        try:
            self._sftp.get(info.path, local_path)
        except Exception as exc:
            raise TransferError(f"Can't download {info.path} from SFTP: {exc}")
        return local_path, True

    def remove(self, path: str) -> None:
        self._sftp.remove(path)

    def close(self) -> None:
        try:
            self._sftp.close()
            self._client.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Transfer
# --------------------------------------------------------------------------

def format_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def deliver(source_path: str, name: str, target_dir: str, log: logging.Logger,
            gzip_input: bool = False, gzip_output: bool = False) -> str:
    """Write the file to target_dir as tmp_<name>, then rename it, so readers never see partial files."""
    final_name = name
    if gzip_input:
        final_name = name + ".gz"
    elif gzip_output:
        if name.endswith(".gz"):
            final_name = name[:-3]
        else:
            log.warning("GzipOutput is Y but %s does not end with .gz, copying it unchanged", name)
            gzip_output = False
    tmp_path = os.path.join(target_dir, TMP_PREFIX + final_name)
    final_path = os.path.join(target_dir, final_name)
    try:
        if gzip_input:
            with open(source_path, "rb") as src, gzip.open(tmp_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
        elif gzip_output:
            with gzip.open(source_path, "rb") as src, open(tmp_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
        else:
            shutil.copyfile(source_path, tmp_path)
        log.info("File %s copied to %s", name, tmp_path)
        os.replace(tmp_path, final_path)
        log.info("File %s renamed to %s", tmp_path, final_path)
    except (OSError, EOFError) as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise TransferError(f"Can't write {name} to {target_dir}: {exc}")
    return final_path


def control_file_path(source, info: FileInfo, extension: str) -> str:
    pm = source.pathmod
    return pm.join(pm.dirname(info.path), pm.splitext(info.name)[0] + "." + extension)


def select_files(cfg: Config, source, log: logging.Logger) -> Tuple[List[FileInfo], Set[str]]:
    everything = source.list_files(cfg.input_dir, cfg.sub_dir_check)
    all_paths = {item.path for item in everything}
    log.debug("%d file(s) found in %s", len(everything), cfg.input_dir)

    selected: List[FileInfo] = []
    for item in everything:
        if cfg.ctl_check and source.pathmod.splitext(item.name)[1] == "." + cfg.ctl_extension:
            continue  # control files are never transferred as data files
        if not cfg.file_mask.search(item.name):
            continue
        if cfg.date_filter and not cfg.date_filter.matches(datetime.fromtimestamp(item.mtime)):
            log.debug("File %s skipped, date %s outside FileCreationDate", item.path, format_time(item.mtime))
            continue
        log.debug("File %s matches", item.path)
        selected.append(item)

    if selected and cfg.size_check:
        log.info("Size check enabled, waiting %d seconds", cfg.size_check_delay)
        time.sleep(cfg.size_check_delay)
        stable = []
        for item in selected:
            current = source.stat(item.path)
            if current and current[0] == item.size:
                stable.append(item)
            else:
                log.info("File %s skipped, size is still changing", item.path)
        selected = stable

    if cfg.ctl_check:
        with_control = []
        for item in selected:
            if control_file_path(source, item, cfg.ctl_extension) in all_paths:
                with_control.append(item)
            else:
                log.info("File %s skipped, control file .%s not found", item.path, cfg.ctl_extension)
        selected = with_control
    return selected, all_paths


def process_file(item: FileInfo, source, cfg: Config, all_paths: Set[str],
                 staging_dir: str, log: logging.Logger) -> None:
    # Mandatory: record when the file was created/modified BEFORE it is transferred.
    log.info(
        "File %s creation/modification date (before transfer): %s, size %d bytes -> transferring to %s",
        item.path, format_time(item.mtime), item.size, ", ".join(cfg.output_dirs),
    )
    local_path, is_temporary = source.materialize(item, staging_dir)
    try:
        for target_dir in cfg.output_dirs:
            deliver(local_path, item.name, target_dir, log, cfg.gzip_input, cfg.gzip_output)
        if cfg.archive:
            deliver(local_path, item.name, cfg.archive_dir, log)  # archive is always the original file
            log.info("File %s archived to %s", item.name, cfg.archive_dir)
    finally:
        if is_temporary:
            try:
                os.unlink(local_path)
            except OSError:
                pass

    if not cfg.keep_input:  # only reached when every delivery succeeded
        source.remove(item.path)
        log.info("File %s removed from input (KeepInput=N)", item.path)
        if cfg.ctl_check:
            control = control_file_path(source, item, cfg.ctl_extension)
            if control in all_paths:
                source.remove(control)
                log.info("Control file %s removed", control)


def run_transfer(cfg: Config, source, log: logging.Logger) -> Tuple[int, int]:
    log.info("****** START Transit files ******")
    log.info("InputDir = %s; OutputDir = %s; FileMask = %s", cfg.input_dir, ",".join(cfg.output_dirs),
             cfg.file_mask_expanded)
    if cfg.file_mask_raw != cfg.file_mask_expanded:
        log.info("FileMask '%s' expanded to '%s'", cfg.file_mask_raw, cfg.file_mask_expanded)
    if cfg.date_check:
        log.info("FileCreationDate '%s' expanded to '%s'", cfg.date_spec_raw, cfg.date_spec_expanded)
    log.debug("SizeCheck=%s CtlFileCheck=%s GzipInput=%s GzipOutput=%s KeepInput=%s ArchiveCheck=%s SubDirCheck=%s",
              cfg.size_check, cfg.ctl_check, cfg.gzip_input, cfg.gzip_output, cfg.keep_input, cfg.archive,
              cfg.sub_dir_check)

    selected, all_paths = select_files(cfg, source, log)
    if not selected:
        log.info("No files to transfer")

    transferred = failed = 0
    with tempfile.TemporaryDirectory(prefix="file-transfer-") as staging_dir:
        for item in selected:
            try:
                process_file(item, source, cfg, all_paths, staging_dir, log)
                transferred += 1
            except Exception as exc:  # one bad file must not stop the others
                failed += 1
                log.error("File %s was not fully processed and is kept in input: %s", item.path, exc)
    log.info("****** END Transit files: %d transferred, %d failed ******", transferred, failed)
    return transferred, failed


# --------------------------------------------------------------------------
# Notification and entry point
# --------------------------------------------------------------------------

def send_notification(cfg: Config, messages: List[str]) -> None:
    items = "<br>\n".join(html.escape(message) for message in messages)
    body = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Automatic File Transfer Notification</title>"
        "</head><body>Hi,<br>This is an automatic notification from file-transfer.py<br><br>"
        f"{items}<br><br>Please do not reply if not needed.<br>Cheers<br>file-transfer.py"
        f"<p style=\"font:7px arial,sans-serif\">v.{VERSION}</p></body></html>"
    )
    message = MIMEMultipart("mixed")
    message["Subject"] = f"Automatic Mail from : File transfer with param {cfg.param_file}"
    message["From"] = cfg.notification_sender
    message["To"] = ", ".join(cfg.notification_to)
    if cfg.notification_cc:
        message["Cc"] = ", ".join(cfg.notification_cc)
    message.attach(MIMEText(body, "html", "utf-8"))
    try:
        with smtplib.SMTP(cfg.notification_server, cfg.notification_port, timeout=30) as smtp:
            smtp.send_message(message, to_addrs=cfg.notification_to + cfg.notification_cc)
    except (smtplib.SMTPException, OSError) as exc:
        print(f"Notification e-mail could not be sent: {exc}", file=sys.stderr)


def acquire_lock(param_file: str):
    """One running instance per param file; returns the lock handle or None if already running."""
    digest = hashlib.sha1(os.path.abspath(param_file).encode("utf-8")).hexdigest()[:16]
    handle = open(os.path.join(tempfile.gettempdir(), f"file-transfer-{digest}.lock"), "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generic file transfer tool")
    parser.add_argument("param_file", nargs="?", help="path of the .param job file")
    parser.add_argument("--check", action="store_true", help="show the expanded FileMask/FileCreationDate and exit")
    parser.add_argument("--now", help='use this time instead of the current one, "YYYY-MM-DD HH:MM:SS"')
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if not args.param_file:
        print("No parameter file is provided, can't do jack!", file=sys.stderr)
        return 1
    if not os.path.isfile(args.param_file):
        print(f"File {args.param_file} is not loaded, not a file!", file=sys.stderr)
        return 1
    if not args.param_file.endswith(PARAM_SUFFIX):
        print(f"File {args.param_file} is not loaded, does not comply with {PARAM_SUFFIX} file type", file=sys.stderr)
        return 1

    try:
        now = datetime.strptime(args.now, "%Y-%m-%d %H:%M:%S") if args.now else datetime.now()
        cfg = Config.from_params(args.param_file, load_params(args.param_file), now)
    except (ConfigError, ValueError, OSError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    if args.check:
        print(f"Reference time   : {now:%Y-%m-%d %H:%M:%S}")
        print(f"FileMask         : {cfg.file_mask_raw}\n  expanded to    : {cfg.file_mask_expanded}")
        if cfg.date_check:
            print(f"FileCreationDate : {cfg.date_spec_raw}\n  expanded to    : {cfg.date_spec_expanded}")
        return 0

    lock = acquire_lock(args.param_file)
    if lock is None:
        print("We found an instance of the same script!", file=sys.stderr)
        return 1
    try:
        return execute(cfg)
    finally:
        lock.close()


def execute(cfg: Config) -> int:
    try:
        log, collector = setup_logging(cfg)
    except (ConfigError, OSError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    source = None
    try:
        validate_paths(cfg)
        source = SftpSource(cfg, log) if cfg.sftp else LocalSource()
        run_transfer(cfg, source, log)
    except (ConfigError, TransferError, OSError) as exc:
        log.error("%s", exc)
    except Exception:
        log.exception("Unexpected error")
    finally:
        if source is not None:
            source.close()
        for handler in list(log.handlers):
            handler.close()

    if collector.messages:
        if cfg.notification:
            send_notification(cfg, collector.messages)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
