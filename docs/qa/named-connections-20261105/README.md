# Named connections UI QA — 2026-11-05

These captures show the current named-connections working-tree templates rendered in Chromium at 1280 × 1100 with synthetic fixtures. They illustrate layout, not successful authorization, token renewal, or revocation. Functional behavior is covered by the authentication and PostgreSQL tests.

- `consent.png`: mandatory editable connection name, with the requesting application and destination shown separately.
- `connections.png`: two named connections belonging to the same synthetic application, each with a synthetic active token row.

No production database, account, vault content, contact, credential or browser address bar was used. All visible names, IDs and dates are disposable QA fixtures; `example.invalid` is deliberately non-routable. Both images were personally inspected after final capture. PNG chunk inspection found only IHDR, IDAT and IEND, with no embedded textual or EXIF metadata.

Template SHA-256 at capture (identifies the exact uncommitted template revision):

- `authorize.html`: `6df36981fca4e421fd37b273a8e8aa9126d2e6f81d28facfd43dcb136dce0415`
- `oauth.html`: `c7c9c484164bceb424d85a98a577bfbacd3c73a4ffc81b45300048d172e05c3b`

Additional final capture on 2026-11-05: `keys.png` shows an expanded Edit name form for a synthetic key. The displayed prefix is the literal fixture `synthetic`, not a credential. Pixels and PNG chunks inspected under the same policy above.

- `keys.html`: `f873f1518fb70553a3399953b1ad6409929bb1e5df237a04d15e819e270c5916`
