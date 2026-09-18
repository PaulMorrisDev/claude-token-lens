# claude-token-lens

Config-aware token and prompt-cache analytics for Claude Code transcripts:
where your tokens go, what your caching configuration costs or saves, and
which configuration changes to make.

**Status: pre-release, v0.1 in progress.**

## What it measures (and what it cannot)

Placeholder — see the project plan for the full scope; filled in as the
parsing and pricing work packages land.

## The two token totals

Placeholder — the "usage tokens" vs "new tokens" distinction, with the
worked "5k Read counted 100 times" example.

## How caching works in Claude Code

Placeholder — prefix layers, the 5-minute default, the 1-hour opt-in, and
what invalidates a cached prefix.

## RE-CACHE detection

Placeholder — the two RE-CACHE signatures (full-expiry,
prefix-invalidated) and how they are attributed to a cause.

## Reading the report

Placeholder — how to read each report section, with a scrubbed example.

## TTL simulation assumptions

Placeholder — the TTL break-even assumptions, stated verbatim as the
report prints them.

## Installing the SessionStart hook

Placeholder — Windows and POSIX installation fragments.

## Running the Docker service

Placeholder — what the container can and cannot touch.

## Profiles and apply

Placeholder — profiles, `apply`, and how to revert.

## Windows notes

Placeholder — project slug case-folding and MSYS path quirks.

## Prior art and credits

Placeholder — credit to token-dashboard, cache-ttl-analyzer and ccusage,
the closest prior art surveyed while planning this tool.

## Development

```
pip install -e .[test]
pytest -q
```

## Licence

MIT — see [LICENSE](LICENSE).
