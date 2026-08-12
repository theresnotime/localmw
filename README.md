# localmw

A CLI for keeping a local MediaWiki development install tidy: pull core, extensions and skins,
find out what state everything is in, get everything back onto master/main, and bin the
`review/*` branches whose changes have already landed in Gerrit.

```console
$ localmw status
$ localmw list --extensions
$ localmw pull --extensions
$ localmw pull --interactive
$ localmw switch
$ localmw cleanup
```

## Install

```console
pipx install localmw
```

From a checkout:

```console
pipx install --editable .
# or
python3 -m pip install -e '.[dev]'
```

Requires Python 3.11+ and a working `git` on `PATH`.

## Seeing what it is doing

Repositories are processed in parallel, so by default you get a progress bar. `-v`/`--verbose`
replaces it with one line per repository, logged as each finishes — useful when something is slow
or hanging and you want to know which repository is to blame. It works before or after the
subcommand:

```console
$ localmw status --verbose          # or: localmw --verbose status
/home/you/git/mediawiki · 9 repositories
Fetching 9 repositories (2 at a time)
✓ vendor                   master · up to date · clean
✓ extensions/AbuseFilter   master · up to date · clean
! extensions/CentralAuth   review/T376215 · no upstream · clean
! extensions/VisualEditor  detached @ 3e5e3d0 · no upstream · clean
✓ extensions/Echo          master · 2 behind · clean
...
```

Lines appear in completion order, not the order they are listed in, and are marked `✓` fine,
`!` needs a look, `✗` broken. For `pull`, `switch` and `cleanup`, `-v` additionally lists every
repository in the results table rather than only the interesting ones.

Verbose lines go to **stderr**, so `localmw status --json --verbose` still gives you parseable
JSON on stdout with the log alongside it. `-q`/`--quiet` goes the other way and drops progress
output entirely. `--no-color` (or setting `NO_COLOR` in the environment) turns off styling. Unlike
`--verbose`, `--quiet` and `--no-color` are top-level flags, so they go before the subcommand:
`localmw --no-color status`, `localmw -q pull`.

## Which install does it operate on?

In order of precedence:

1. `--mw /path/to/mediawiki`
2. the current directory, or the nearest parent that looks like a MediaWiki install
3. `mediawiki_dir` from the config file (or `LOCALMW_MEDIAWIKI_DIR`)

Every run prints the resolved path, so it is never ambiguous. A directory counts as an install
if it has `includes/` and `maintenance/` plus one of `includes/Setup.php`, `mw-config/`,
`extensions/` + `skins/`, or a `RELEASE-NOTES-*` file.

Repositories are discovered as: core (the root itself), `vendor/`, every git checkout directly
inside `extensions/`, and every git checkout directly inside `skins/`. Directories without a
`.git` are ignored and reported.

## Selecting which repositories

By default every command works on the whole install. The same selection flags narrow that down,
and they work the same way on `status`, `list`, `pull`, `switch` and `cleanup`:

| Flag | What it does |
| --- | --- |
| `--core` | MediaWiki core (the root checkout) |
| `--extensions` | everything in `extensions/` |
| `--skins` | everything in `skins/` |
| `--vendor` | the `vendor/` checkout |
| `--no-vendor` | everything except `vendor/`, which is otherwise included |
| `-o, --only PATTERN` | only repositories matching PATTERN (glob, case-insensitive, repeatable) |
| `-x, --exclude PATTERN` | skip repositories matching PATTERN (glob, case-insensitive, repeatable) |

The `--core`/`--extensions`/`--skins`/`--vendor` flags are additive: pass several to combine them,
or none to get everything. `--only` and `--exclude` then filter whatever is in play, matching a
repository's name, kind, or path. Patterns from the config file's `exclude` are always applied on
top.

```console
localmw status --extensions        # just extensions/*
localmw pull --core --skins        # core and skins/*
localmw list --no-vendor           # everything except vendor/
localmw switch -o Wikibase         # a single repository, by name
localmw cleanup -x 'Wiki*'         # everything but the Wiki* repositories (glob)
```

## Commands

### `localmw status`

Reports, for every repository: the checked-out branch, how far ahead/behind its upstream it is,
uncommitted changes, and the age of the last commit.

```console
$ localmw status
/home/you/git/mediawiki · 9 repositories
Repository               Branch              Upstream          Working tree  Last commit    Notes
core                     master              3 behind          clean         2 hours ago
vendor                   master              up to date        clean         5 days ago
extensions/AbuseFilter   master              up to date        clean         3 days ago
extensions/CentralAuth   review/T376215      no upstream       clean         1 hour ago
extensions/Echo          master              1 behind          clean         6 hours ago
extensions/VisualEditor  detached @ 3e5e3d0  no upstream       clean         2 days ago
extensions/Wikibase      master              2 behind 1 ahead  clean         4 hours ago    diverged from upstream
skins/MinervaNeue        master              up to date        clean         3 days ago
skins/Vector             master              up to date        ~1 ?1         1 day ago

9 repositories · 3 behind · 1 with local changes · 1 on another branch · 1 detached
behind: core, extensions/Echo, extensions/Wikibase — run 'localmw pull' to update
```

The working tree column reads `+N` staged, `~N` modified, `!N` conflicted, `?N` untracked.

It fetches from remotes first (that is what makes "behind" meaningful); pass `--no-fetch` to work
offline. Useful flags:

| Flag | What it does |
| --- | --- |
| `--attention` | only list repositories that are behind, dirty, detached, or off master/main |
| `--json` | machine-readable output, including a summary block |
| `--no-fetch` | do not touch the network (behind counts may be stale) |
| `--prune` | prune deleted remote-tracking branches while fetching |
| `-j, --jobs N` | how many repositories to process at once (default 2) |
| `-v, --verbose` | log each repository as it is read instead of a progress bar |

Exits non-zero if a repository could not be read.

### `localmw list`

The same report as `status`, but nothing touches the network, so it is quick and works offline.
The `behind` counts are whatever your last fetch knew about.

```console
localmw list                  # everything
localmw list --extensions     # just extensions/*
localmw list --skins          # just skins/*
localmw list --attention      # only what is off master/main, dirty, or behind
localmw list --json           # same payload as 'status --json', with "fetched": false
```

| Flag | What it does |
| --- | --- |
| `--attention` | only list repositories that are behind, dirty, detached, or off master/main |
| `--json` | machine-readable output, including a summary block |
| `-j, --jobs N` | how many repositories to process at once (default 2) |
| `-v, --verbose` | log each repository as it is read instead of a progress bar |

It is exactly `localmw status --no-fetch`, kept as its own command because reaching for it is the
common case: "what have I got checked out?" rather than "what has moved upstream?"

### `localmw pull`

Fetches and then fast-forwards each repository — but only where that is unambiguously safe.
A repository is **skipped**, with the reason reported, when it has:

- uncommitted changes to tracked files (`--allow-dirty` to override)
- something other than master/main checked out (`--any-branch` to override)
- a detached HEAD, or no upstream branch configured
- diverged from its upstream, i.e. local commits that are not pushed (`--strategy rebase` to
  replay them on top instead)

Untracked files never block a pull.

```console
localmw pull                     # everything that is safe to fast-forward
localmw pull --core              # just core
localmw pull --extensions        # just extensions/*
localmw pull --core --skins      # core and skins/*
localmw pull --no-vendor         # everything except vendor/
localmw pull -o Vector -o 'Wiki*'  # selective, glob, repeatable
localmw pull -x Wikibase         # everything except Wikibase
localmw pull --dry-run           # report what would change
localmw pull --interactive       # ask about each one before pulling it
```

With `-i, --interactive`, localmw checks everything first and then asks about each repository that
actually has upstream commits — the ones it would otherwise pull. Enter (or `p`) pulls it, `s`
skips it:

```console
$ localmw pull --interactive
/home/you/git/mediawiki · 9 repositories
3 repositories have upstream commits:
  extensions/CentralAuth   1 commit behind   [P]ull / [s]kip p
  extensions/Echo          4 commits behind  [P]ull / [s]kip
  extensions/Wikibase      2 commits behind  [P]ull / [s]kip s

Repository               Branch  Result               Detail
extensions/Wikibase      master  skipped              2 commits behind · skipped at the prompt
extensions/CentralAuth   master  updated (1 commit)
extensions/Echo          master  updated (4 commits)

9 repositories · 2 updated · 6 up to date · 1 skipped
```

Repositories that were never candidates — dirty, off master/main, diverged — are not asked about,
just reported as usual. Nothing is pulled until you have answered for all of them, so Ctrl-C at any
prompt leaves everything as it was. The repositories you say yes to are pulled together, using the
fetch from the checking pass rather than fetching twice.

| Flag | What it does |
| --- | --- |
| `--strategy ff-only\|rebase\|merge` | how to integrate upstream commits (default `ff-only`) |
| `--allow-dirty` | pull despite uncommitted changes |
| `--any-branch` | pull despite a non-default branch being checked out |
| `--submodules` | run `git submodule update --init --recursive` afterwards |
| `--prune` | prune deleted remote-tracking branches while fetching |
| `-i, --interactive` | ask Pull/Skip for each repository that has upstream commits |
| `-j, --jobs N` | how many repositories to process at once (default 2) |
| `-n, --dry-run` | change nothing (cannot be combined with `--interactive`) |
| `-v, --verbose` | log each repository as it is pulled, and list every one in the results |

Exits non-zero if any repository failed (skips are not failures).

### `localmw switch`

Puts repositories that are off master/main back onto it — the usual state after a week of
`git review -d`. Anything already on its default branch is left alone.

```console
localmw switch                     # every repository that is off master/main
localmw switch --extensions        # just extensions/*
localmw switch -o Wikibase         # just that one
localmw switch --dry-run           # show what would move
localmw switch --discard-changes   # move even where there is uncommitted work
```

```console
$ localmw switch
/home/you/git/mediawiki · 9 repositories
Repository               From                To      Result         Detail
skins/Vector             review/1109334      master  skipped        1 uncommitted change ·
                                                                    --discard-changes to throw them away
extensions/CentralAuth   review/T376215      master  switched       2 behind
extensions/VisualEditor  detached @ 3e5e3d0  master  switched
extensions/Wikibase      review/1104782      master  switched

9 repositories · 3 switched · 5 already there · 1 skipped
1 repository now behind — run 'localmw pull' to update
```

A repository with **uncommitted changes to tracked files is skipped**, not switched. Passing
`--discard-changes` throws that work away instead — so you are shown what will be lost and asked
to confirm first (`-y` skips the prompt, `--dry-run` never changes anything). Untracked files are
never touched, and the branch you were on is left in place, so switching back is just
`git checkout -`.

Detached HEADs count as "off master/main" and are switched too. If the default branch only exists
as `origin/master`, it is created as a local branch tracking it.

| Flag | What it does |
| --- | --- |
| `--discard-changes` | throw away uncommitted changes to tracked files instead of skipping |
| `-n, --dry-run` | report what would move, changing nothing |
| `-y, --yes` | do not ask before discarding changes |
| `-j, --jobs N` | how many repositories to process at once (default 2) |
| `-v, --verbose` | log each repository as it is processed, and list every one in the results |

Exits non-zero if any repository failed (skips are not failures).

### `localmw cleanup`

Deletes the scratch branches `git review -d` leaves behind, once their changes have merged.

For each local branch starting with `review/` it reads the `Change-Id` trailers from the commits
that branch carries (i.e. those not on master/main), asks Gerrit about them, and deletes the
branch only if **every** change on it is merged. Anything still open, unrecognised, or currently
checked out is left alone and reported. Nothing is deleted without confirmation.

```console
$ localmw cleanup
/home/you/git/mediawiki · 9 repositories
querying Gerrit anonymously (set gerrit.username/gerrit.http_password for private changes)
Repository       Branch          Last commit  Change   Action  Reason
core             review/1104782  3 days ago   1104782  delete  merged in Gerrit
extensions/Echo  review/T376215  1 hour ago   1109334  keep    still open in Gerrit
skins/Vector     review/1101019  2 weeks ago  1101019  delete  merged in Gerrit

Delete 2 branches? [y/N]:
```

Pass `-v` to see the kept branches too; the change numbers are clickable links to Gerrit.

| Flag | What it does |
| --- | --- |
| `--prefix PREFIX` | branch prefix to consider (default `review/`) |
| `--include-abandoned` | also delete branches whose change was abandoned |
| `--no-gerrit` | offline mode: only delete branches already merged into master/main locally |
| `-n, --dry-run` | report what would be deleted |
| `-y, --yes` | skip the confirmation prompt |
| `-j, --jobs N` | how many repositories to scan at once (default 2) |
| `-v, --verbose` | log each repository as it is scanned, and also list the branches being kept |

Branches merged locally are removed with `git branch -d`; branches whose change merged in Gerrit
(usually rebased or cherry-picked on the way in, so not an ancestor of master) need `-D`, which
localmw only uses after Gerrit has confirmed the change is `MERGED`. Your default branch is never
a candidate, and neither is the branch you currently have checked out.

## Configuration

`~/.config/localmw/config.json`, or `$LOCALMW_CONFIG_DIR/config.json`, or
`$XDG_CONFIG_HOME/localmw/config.json`. Pass `--config /path/to/config.json` (before the
subcommand) to use a specific file instead. Create it with:

```console
localmw config init
localmw config set mediawiki_dir ~/git/mediawiki
localmw config set gerrit.username sammy
localmw config set gerrit.http_password        # prompts, so it stays out of your shell history
localmw config check                           # validate, and ping Gerrit
```

```json
{
  "mediawiki_dir": "/home/you/git/mediawiki",
  "gerrit": {
    "url": "https://gerrit.wikimedia.org/r",
    "username": "sammy",
    "http_password": "..."
  },
  "pull": {
    "strategy": "ff-only",
    "submodules": false
  },
  "jobs": 2,
  "default_branches": ["master", "main"],
  "exclude": ["Wikibase"],
  "review_branch_prefix": "review/"
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `mediawiki_dir` | – | fallback install when the current directory is not one |
| `gerrit.url` | `https://gerrit.wikimedia.org/r` | Gerrit base URL |
| `gerrit.username` | – | Gerrit username, only needed for authenticated calls |
| `gerrit.http_password` | – | Gerrit HTTP password (Settings → HTTP Credentials) |
| `pull.strategy` | `ff-only` | `ff-only`, `rebase`, or `merge` |
| `pull.submodules` | `false` | update submodules after pulling |
| `jobs` | `2` | repositories processed concurrently (raise it if your network is the bottleneck) |
| `default_branches` | `["master", "main"]` | branches considered "the default branch", and what `switch` moves to |
| `exclude` | `[]` | globs of repositories to always skip |
| `review_branch_prefix` | `review/` | prefix `localmw cleanup` looks for |

The config file is written with `0600` permissions because it can hold your Gerrit password.

| Subcommand | What it does |
| --- | --- |
| `localmw config path` | print the config file location |
| `localmw config keys` | list every setting, its default, and what it does |
| `localmw config show` | show effective values and where each came from (`--reveal`, `--json`) |
| `localmw config get KEY` | print one effective value |
| `localmw config set KEY [VALUE]` | set a value; omit VALUE to be prompted (hidden for secrets) |
| `localmw config unset KEY` | drop a value back to its default |
| `localmw config init` | write a starter file (`--force` to overwrite) |
| `localmw config check` | validate the config and ping Gerrit |

Every setting can also come from the environment, which is handy for secrets you would rather not
write to disk: upper-case the key and replace dots with underscores, e.g.
`LOCALMW_GERRIT_HTTP_PASSWORD`, `LOCALMW_JOBS`, `LOCALMW_MEDIAWIKI_DIR` (also accepted as the
shorthands `LOCALMW_MW_DIR` and `LOCALMW_MW`). Environment values win over the file. `localmw
config show` tells you where each effective value came from.

Gerrit access is anonymous unless a username **and** HTTP password are set, which is enough for
public changes on gerrit.wikimedia.org.

## Notes

- git is invoked with `GIT_TERMINAL_PROMPT=0` and, unless you set your own `GIT_SSH_COMMAND`,
  `ssh -oBatchMode=yes`. Repositories are processed in parallel with piped output, so a password
  or host-key prompt would otherwise hang invisibly; this way it fails with a clear message.
- Nothing is ever pushed, reset, or committed on your behalf, and no remote is modified. Two
  things can lose work, and both ask before doing so: `localmw cleanup` deletes local branches,
  and `localmw switch --discard-changes` throws away uncommitted changes. Without that flag,
  `switch` only ever moves a repository whose working tree is clean, and `pull` only ever
  fast-forwards one.

## Development

```console
python3 -m pip install -e '.[dev]'
pytest          # ~11s, spread across cores
pytest -n0      # one process, for a debugger or clearer output
ruff check .
ruff format .
```

The test suite builds throwaway git repositories on disk, so it exercises the real `git` plumbing
rather than mocks; Gerrit is stubbed out. That makes it subprocess-bound, so it is kept quick in
three ways: the install and single-repository layouts are built once per session and copied per
test (`fixtures.clone_snapshot`), repository settings come from `GIT_CONFIG_*` in the environment
rather than extra `git config` calls, and `pytest` runs under `-n auto` by default.

## Licence

MIT
