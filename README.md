# filetransfer-py

Python rewrite of `file_tran.pl` (see `../filetransfer`). Copies or moves files
from a local directory or an SFTP server into one or more target directories,
with optional file-date filter, stable-size check, control-file check,
gzip/gunzip, archive, logging and e-mail notification on errors.

Same `.param` files and cron usage as the Perl tool. The difference: `FileMask`
and `FileCreationDate` use a small date language instead of Perl code, e.g.
`<YYYYMMDD-5d>` = the date 5 days ago (see `docs/date-syntax.md`).

## Requirements

- Python 3.9 or newer (RHEL9 default is fine)
- `paramiko` only for SFTP jobs: `pip install -r requirements.txt`

## Installation

```sh
mkdir -p /opt/filetransfer-py && cd /opt/filetransfer-py
# copy file-transfer.py here
chmod +x file-transfer.py
mkdir -p param pass
```

## Usage

```sh
file-transfer.py /opt/filetransfer-py/param/job.param
file-transfer.py job.param --check                         # show expanded FileMask / FileCreationDate
file-transfer.py job.param --now "2026-05-01 08:00:00"     # run as if it were another time
```

Crontab (every 5 minutes):

```
*/5 * * * * /opt/filetransfer-py/file-transfer.py /opt/filetransfer-py/param/job.param
```

## Parameters

Format: `Name:value`, one per line, `#` starts a comment. Y/N values must be `Y` or `N`.

| Parameter | Value | Description |
|---|---|---|
| `InputDir` | path | Source directory (remote path when SFTP) |
| `OutputDir` | path[,path...] | Target directory/directories (must exist) |
| `KeepInput` | Y/N | Y copy (default), N move |
| `SftpCheck` | Y/N | Y SFTP source, N local |
| `SftpType` | Clear/Key | Password file or private key |
| `SftpPassFile` | path | First line is the password (Clear) |
| `SftpKey` | path | Private key file (Key) |
| `SftpHost`, `SftpUser` | text | SFTP server and user |
| `FileMask` | regex with optional date tokens | Matched against the file name |
| `FileDateCheck` | Y/N | Filter by file modification date |
| `FileCreationDate` | date or `date&datedate` | Required if `FileDateCheck:Y`; may use date tokens |
| `SubDirCheck` | Y/N | Recurse into sub-directories |
| `SizeCheck` | Y/N | Skip files whose size changes during `SizeCheckDelay` seconds |
| `CtlFileCheck` | Y/N | Require `<name>.<CtlFileExtension>` next to the file |
| `CtlFileExtension` | text | Control file extension |
| `GzipInput` | Y/N | Gzip the file in the target (adds `.gz`) |
| `GzipOutput` | Y/N | Input is `.gz`, unpack it into the target |
| `ArchiveCheck`, `ArchiveDir` | Y/N, path | Copy the original file to the archive |
| `LogMode` | Full/Basic/None | Full = everything, Basic = transfers and errors, None = no log file |
| `LogDir`, `LogFile` | path, name | Log location |
| `Notification` | Y/N | E-mail the errors |
| `NotificationSender`, `NotificationRe`, `NotificationCc` | e-mail(s) | Comma separated lists |
| `NotificationMethod`, `NotificationServer` | `smtp`, host | SMTP relay |

Optional extras: `SftpPort`, `SftpStrictHostKey`, `SizeCheckDelay`, `NotificationPort` (see `docs/architecture.md`).
A complete example is in `examples/example-sftp.param`.

## Date tokens in short

```
FileMask:^OK_File_<YYYYMMDD-5d>.*zip$                 # file dated 5 days ago
FileCreationDate:<YYYY-MM-DD-1d>&date<YYYY-MM-DD>     # modified yesterday or today
FileMask:^Report_<YYYYMM-1mo>\.csv$                   # previous month
```
`<YYYYMMDD-5d>` shifts the whole date, so it is correct across month and year ends.
Units: `y mo d h mi s`. The single-part form `<DD-5>` also exists but is only safe
alone. Full syntax: `docs/date-syntax.md`.

## Converting old Perl .param files

`FileMask` and `FileCreationDate` in old `.param` files may contain Perl code. Convert them with:

```sh
convert-param.py /opt/filetransfer/param      # folder to check (default: current folder)
convert-param.py --dry-run /opt/filetransfer/param   # only show what would change
```

For every `.param` file that still holds Perl code the script shows the old and new
value and asks `[y]es / [n]o / [a]ll / [q]uit`. On yes the original is copied to
`<name>.param_perl` and then the file is converted; all other lines, comments and line
endings stay as they were. An existing `.param_perl` is never overwritten. `--yes` converts
without asking.

Understood: `localtime()`, `localtime(time - 86400*5)`, `sprintf("%02d", $mday-1)`,
`$year+1900`, `$mon+1` and string concatenation (as in the PDF examples). Parts that are
printed together share one shift, so a "yesterday" such as `$mday-1` becomes
`<YYYY-MM-DD-1d>`, which is also right on the 1st of a month. Any other Perl is reported as
"CANNOT convert automatically" and the file is left unchanged (also when only one of the two
values could not be converted). The result is checked with the same rules the transfer tool uses.

## Logging

Format: `pid: dd/mm/yy HH:MM:SS [ message ]`. For every file the creation/modification
date is logged **before** it is transferred:

```
4731: 20/10/26 20:55:23 [ File /in/DR_P11_a.dat creation/modification date (before transfer): 2026-10-18 09:15:30, size 12 bytes -> transferring to /out/ ]
```

## Tests

```sh
python3 tests/test-file-transfer.py -v
python3 tests/test-convert-param.py -v   # also compares with real Perl if installed
```
