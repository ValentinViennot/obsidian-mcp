# Tasks settings UI QA — 2026-11-12

These final-revision captures use a disposable PostgreSQL database, an empty
synthetic vault and the non-admin `qa-user` account. Desktop is 1280 × 1000;
mobile is 390 × 844. Full-page captures illustrate responsive layout. They do
not prove task-tool execution or authorization.

The browser exercise saved a task-details/tag rule, synthetic `review` tag,
`Europe/Paris` timezone and `Archive/` exclusion, then reloaded the page. An
invalid timezone was refused and the previous settings remained. Browser
warning/error/CSP logs were empty. The panel ran in registry sandbox mode to
avoid all embedding/indexer calls; task behavior is tested separately against
synthetic PostgreSQL and native filesystem fixtures.

Both the implementing agent and lead personally inspected the included
pixels. There are no real contacts, credentials, financial data, documents,
browser address bars or local paths. Blank password inputs are existing page
controls. JPEG inspection found only the 14-byte APP0 JFIF segment, with no
EXIF, XMP or comment metadata. An animation-timing validation capture was
discarded and is not included.

- `tasks-desktop.jpg`: saved scoped task preferences in the native account page.
- `tasks-mobile.jpg`: the same preferences and save control at mobile width.

## Source SHA-256 at capture

- `src/control_panel/templates/_task_settings_card.html`: `aee94b23ee95159a8cb7c10379bc8950a528868976325e6d3255eb6a75ddee84`
- `src/control_panel/templates/account.html`: `15ee31d3c212612b100b28061af45316e1cb2e69d61e2cacfabfbe64f35707f6`
- `src/control_panel/templates/settings.html`: `b8e2b2f7d48822d9f616f17389c0c425f35babc756093b2ad18cb4d067bc76a7`
- `src/control_panel/routes.py`: `6fc14f00d1ae1e10ac2748fa3cec4dba39e289a4c03a63f7b2c3bfdb04cb5b10`
