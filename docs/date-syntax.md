# Date mini-language (FileMask, FileCreationDate)

Both `FileMask` and `FileCreationDate` may contain **date tokens** in `<...>`.
Before the job runs every token is replaced by a date part. Plain text and regular
expressions without tokens are used unchanged. There is no code execution.

Parts: `YYYY` (year), `YY` (2-digit year), `MM` (month), `DD` (day), `HH` (hour),
`MI` (minute), `SS` (second). Everything is case-insensitive and spaces inside
the brackets are allowed.

## Form 1 - block with shift (recommended): `<FORMAT-Nu>`

The **whole date is shifted once**, then formatted. Always a real date, also
across month and year ends.

`FORMAT` is any mix of the parts and the separators `- _ : . / T` and space.
`N` is a number with a sign (`-` past, `+` future), `u` is the unit:

| Unit | Meaning |
|---|---|
| `y` | years |
| `mo` | months |
| `d` | days |
| `h` | hours |
| `mi` | minutes |
| `s` | seconds |

| Token | On 2026-01-03 10:30:00 gives |
|---|---|
| `<YYYYMMDD>` | `20260103` (no shift = now) |
| `<YYYYMMDD-5d>` | `20251229` |
| `<YYYY-MM-DD+1d>` | `2026-01-04` |
| `<YYYYMM-1mo>` | `202512` |
| `<YYYY-1y>` | `2025` |
| `<YYYYMMDD HH-2h>` | `20260103 08` |
| `<YYYY-MM-DD HH-MI-SS-90mi>` | `2026-01-03 09-00-00` |

Calendar lengths and leap years are respected: on 1 May 2026 `<YYYY-MM-DD-5d>` is
`2026-04-26` (April has 30 days). A month shift keeps the day
when possible and otherwise uses the last day of the month (31 March `-1mo` is 28 or 29 February).

## Form 2 - single part: `<UNIT-N>`

Shifts the date by N of **that part's own unit** and prints only that part:
`<DD-5>` = day of (today - 5 days), `<MM-1>` = month of (today - 1 month),
`<YYYY-1>` = last year. On 1 May `<DD-5>` is `26`.

**Limitation:** the parts do not influence each other, so `<YYYY><MM><DD-5>` on
3 January gives `2026 01 29`, which is not a real date. Use form 2 only alone or
where the month cannot change. For complete dates use form 1: `<YYYYMMDD-5d>`.

## Errors

A token-like text that is not valid (`<YYYYMMDD-5>`, `<YYYYMMDD-5w>`, `<DD*5>`)
stops the job with an error instead of being silently treated as a regex.
Avoid regex named groups such as `(?P<DD>...)`; the `<DD>` part would be replaced.

## FileCreationDate

Format: `YYYY[-MM[-DD[ HH[-MI[-SS]]]]]`. One date matches files modified in that
period; two dates separated by `&date` match the whole range, both ends included.
The precision follows what you write: `2026-05` means the whole month,
`2026-05-01 10` the whole hour 10:00-10:59.

| Value | Matches |
|---|---|
| `2026-04-22` | files modified on 22 April 2026 |
| `<YYYY-MM-DD>` | files modified today |
| `<YYYY-MM-DD-1d>` | files modified yesterday |
| `2026-04-22&date2026-04-23` | 22 and 23 April |
| `<YYYY-MM-DD-1d>&date<YYYY-MM-DD>` | yesterday and today, also on the 1st of a month |
| `<YYYY-MM-1mo>&date<YYYY-MM>` | previous month and this month |
| `2026-05-01 08-00-00&date2026-05-01 09-30-00` | exact time window |

An invalid date or range stops the job (nothing is transferred). The Perl
version silently continued without any date filter in some cases.

The date compared is the file's modification time (Linux has no portable
creation time; SFTP only exposes the modification time).

## Check what a job will use

```sh
file-transfer.py job.param --check
file-transfer.py job.param --check --now "2026-05-01 08:00:00"
```
