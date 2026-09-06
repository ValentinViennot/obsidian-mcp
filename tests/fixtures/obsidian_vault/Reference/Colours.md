---
tags:
  - reference
  - "#design"
---

# Things that are not tags

A diagram's palette, written inline the way a draw.io or mermaid export writes
it: #ffe6cc fill, #d5e8d4 fill, #82b366 stroke, #b85450 stroke. Greys: #fff
#ccc #000 #eeeeee. None of those is a tag.

Issue references are not tags either: #42, #1234, #7.

A URL fragment is not a tag: https://example.test/handbook#onboarding and
http://example.test/#top.

Indented code is not prose, so nothing in this block is a tag:

    #include <stdio.h>
    #define WIDTH 80
    #region helpers
    # a shell comment
    #endregion

A nested list, however, is not code, and its tags are real:

- parent
    - child #reference/nested
        - grandchild #reference/deep

These ARE tags: #design-system, #3d-printing, #1password, #projekt/größe,
#日本語, #a-b/c.
