# Browser Header Sanitizer Boundary Design

Date: 2026-07-24

## Scope

Before generic assignment projection, gateway public text and runtime reasons
recognize `Cookie` and `Authorization` followed by `:` or `=`. The complete
remainder of that line is one atomic header value. Names such as `status`,
`reason`, `error`, `message`, and `stage` inside that value are never outer
diagnostic boundaries.

The atomic value is preserved only when trimming whitespace and one matching
pair of outer quotes yields exactly `missing`, `expired`, `invalid`, or
`not configured` (case-insensitive). Every other value becomes one
`[redacted]`.

This final boundary does not add any other header names or separators.

## Data flow

For each line:

1. Keep the existing URL and Bearer projection.
2. Find the first Cookie/Authorization `:`/`=` header.
3. Classify the complete value through end of line.
4. Preserve an exact safe status or replace the value with `[redacted]`.
5. Run the existing generic assignment projector on the resulting line.

## Tests

Gateway and runtime use matching matrices covering all four header forms,
the five diagnostic-looking nested parameter names, comma/semicolon,
quoted/unquoted values, and a random secret. Exact safe-status cases remain
unchanged; scheme-prefixed or compound values are redacted.
