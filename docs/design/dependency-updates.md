# Dependency updates

## Problem

Nothing keeps the dependencies current.
The dev dependencies use `~=` pins, and the actions in the workflows use SHA pins.
Both go stale without a bot (#97).

## Choice

Renovate opens the update pull requests, and it reads `renovate.json`.
Dependabot sends alerts only.
A `dependabot.yml` for version updates would make two bots open the same pull request, so the repository has none.

## What Renovate manages

`enabledManagers` lists `pep621` and `github-actions`.
The `pep621` manager reads `pyproject.toml` and updates `uv.lock` with the new version.
The `github-actions` manager reads the workflows.
It pins each action to a commit digest (`pinDigests`), and it keeps the version comment next to the digest.

Renovate does not manage the `pre-commit` hooks or the pinned tool versions in the workflows.
The `zizmor` hook revision must equal `ZIZMOR_VERSION` in `ci.yml` (see `AGENTS.md`).
Two separate updates would break that rule, so a person changes both in one pull request.

## Delay and checks

`minimumReleaseAge` holds a release back for a number of days after its publication.
`internalChecksFilter` is `strict`, so Renovate creates no branch until the release is old enough.
The age gives the community time to report a bad release before this repository takes it.

## Major updates

A major update never merges by itself.
Renovate opens the pull request, and a person reviews it.
This holds for the dev dependencies as well.

## Automerge

The `automerge` switch is `false` in `renovate.json`, in the rule for the non-major updates.
Renovate merges a pull request when its checks pass.
Without a required check, a pull request would merge the moment it is mergeable.
So the switch stays `false` until these two steps are done:

1. #104 shows which checks the default branch requires, and the required checks include the CI jobs.
2. An admin turns on "Allow auto-merge" in the repository settings.

Then set `automerge` to `true` in the non-major rule, and change `tests/test_renovate_config.py` in the same commit.

## Settings that a file cannot change

An admin does these steps in the GitHub settings:

1. Install the Renovate app for this repository, and check that it can read and open pull requests.
2. Turn on Dependabot alerts under Settings, Code security.
   Leave Dependabot security updates and version updates off.

A representative update pull request then shows the dependency and `uv.lock` change together, or the digest and its comment.
It waits for the release age and for the required checks.
