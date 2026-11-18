# Upstream

This tree starts from the public Cairn snapshot:

- Project: [yes999zc/cairn-public](https://github.com/yes999zc/cairn-public)
- Commit: `231768005259937f3425280b2f1ae3f6ecb959e3`
- Description: Publish curated Cairn source snapshot
- Date: 2026-11-04 20:58:34 +0800
- License: Apache License 2.0 (`LICENSE`)
- Third-party notices: `NOTICE`

Cairn's own code stays under Apache-2.0. The family profile, material ledger, and import command are derivative work under the same license. Upstream behavior is unchanged unless `CAIRN_FAMILY_PROFILE=jojo`.

The public snapshot does not include Cairn's private sample photos (`docs/samples/`, `CAIRN_SAMPLE_DIR`) or textbook word lists (`data/wordbank/`). Tests that need those fixtures skip or fail until the files are supplied locally. This derivative does not add those private Cairn fixtures.

What this snapshot does not include, and this tree also does not claim to deliver: a plugin marketplace, dynamic plugin loading, branches, loops, or an Android client.
