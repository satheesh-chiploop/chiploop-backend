# ChipLoop SDK/API Versioning

## Current Version

The current public developer API is:

```text
/sdk/v1
```

The Python SDK defaults to `v1`.

## Endpoint Maturity

Stable:

- workflow listing
- workflow run submission
- workflow status polling
- artifact listing
- artifact download
- agent listing
- usage summary
- plan summary

Beta:

- Studio Agent Planner
- Studio Agent Factory dry-run

Internal:

- `/studio/*`
- `/settings/*`
- `/admin/*`
- legacy App workflow endpoints

## Compatibility

Existing `/sdk/*` routes are preserved for older clients. New integrations should use `/sdk/v1/*`.

## Breaking Changes

Rules for stable v1 endpoints:

- Additive response fields are allowed.
- Removing fields requires a deprecation notice.
- Breaking request or response changes should go to `/sdk/v2`.
- SDK major versions should align with API breaking changes.

## SDK Package Version

The SDK remains `0.x` while the public API is still being validated with users. Move to `1.0` after:

- quickstart examples are stable
- artifact download behavior is validated
- API key setup is proven for Pro, Pro Max, and Enterprise users
- error model is consistent
- at least one external automation user has tested the flow

