# Login layout QA — 5 November 2026

Captured from the integrated working tree based on fork `d04ebc2` and upstream
`7e8c992`, after the authentication merge and its focused tests. The screenshots
show the actual templates rendered by the login/error renderers under an
enforcing nonce CSP, using disposable synthetic settings and the fictional
provider “Example SSO”. They do not show a live provider or production account.

Chromium headless, 1280 × 900, reduced motion. No browser address bar is included.
The captures were personally inspected: no private data, credentials, real
contacts, financial data, note content or local paths. PNG chunks are only IHDR,
IDAT and IEND: no textual or EXIF metadata. Browser page/console error collection
was empty on both pages.

- `native-login.png`: upstream's full-width SSO action and secondary password
  form, in native OIDC mode with local login enabled.
- `pocketid-error.png`: the existing PocketID retry screen retained by the fork.

These are layout evidence, not proof of successful authentication. Protocol and
mode-isolation behavior is covered by automated tests. Captures are from the
integrated UI revision; later deployment/test-only changes do not alter these
templates. No full live-provider login or exhaustive panel walkthrough is claimed.
