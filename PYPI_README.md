# 1point3acres Toolkit

Local automation for the [1point3acres](https://www.1point3acres.com) forum that runs in your own Chrome on your own
machine. It completes the daily check-in and daily quiz and confirms the reward on the credits page, keeps an offline
library of interview-experience posts you can search and export, and offers read, post and reply tools. All of it is
available as a command line and as an MCP server over stdio for Claude Code, Codex and other MCP clients.

The full documentation, in Chinese, is in the repository: https://github.com/vivian-labs/1point3acres-toolkit

## Requirements

- macOS or Windows with Google Chrome installed
- Python 3.12
- A 1point3acres account

## Install

Run the MCP server straight from PyPI:

```sh
uvx 1point3acres-toolkit
```

Or install it into an environment of your own:

```sh
pip install 1point3acres-toolkit
```

That puts two commands on your PATH: `1point3acres-toolkit` starts the MCP server on stdio, and
`1point3acres-toolkit-cli` runs the same functions from the command line (`1point3acres-toolkit-cli status` to begin).

Register the server with an MCP client:

```sh
claude mcp add --scope user 1point3acres-local -e PYTHONUTF8=1 -- uvx 1point3acres-toolkit
codex mcp add 1point3acres-local --env PYTHONUTF8=1 -- uvx 1point3acres-toolkit
```

## Where it keeps things

An installed copy writes everything to one per-user directory: `~/Library/Application Support/1point3acres-toolkit`
on macOS, `%LOCALAPPDATA%\1point3acres-toolkit` on Windows. Set `ONEPOINT3ACRES_HOME` to move it. Inside it:

- `state/account.json`: your forum username and numeric uid, the only account facts stored in a file
- `chrome-profile/`: the dedicated Chrome profile that holds the login session
- `state/interviews.sqlite` and `Stripe面经资料/`: the interview library and its exports
- `mcp.config.json`: a ready-made client entry, written on first start

The forum password never goes in a file. Store it once with `1point3acres-toolkit-cli save-credentials`, which reads a
JSON object with `username` and `password` on standard input and keeps it in the macOS Keychain or Windows DPAPI.

## What it will not do

- It talks only to the forum's own hosts; there is no third-party server and no captcha service.
- Posting and replying preview by default; a real write to the site needs an explicit flag.
- It never buys unlocks, never mass-downloads and never marks notifications read.

## License

MIT. Source, issues and the Chinese manual: https://github.com/vivian-labs/1point3acres-toolkit

mcp-name: io.github.vivian-labs/1point3acres-toolkit
