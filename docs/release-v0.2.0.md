# v0.2.0 — Reliable uploads, broader discovery and a clearer interface

Songfinder now waits for file confirmation before uploading, keeps optional clues beside
that confirmation, and explains match, no-match and incomplete searches separately.
The permanent public example, clearer speed/start cards, folded recent searches,
feedback and collapsed retry form complete the new phone/desktop flow.

## Reliability and search changes

- A1: provider trouble is structured data emitted at caught errors and budget gates;
  routine progress text and song titles cannot turn a clean no-match into an outage.
  Admin source-issue metrics ignore legacy log-derived strings.
- A2: broad cache scanning follows original-speed, speed-corrected and fallback
  recognition, so catalogue growth cannot starve those stages.
- A3: spaced generic title expansions reach more full-length Bandcamp recordings.
  Title-only searches allow up to 150 Bandcamp candidates plus 30 other candidates
  within the existing deadline; the title filter remains in place.
- A4: query activity selects disjoint discovery/confirmation passages instead of
  dropping quiet slots. Near-identical independent detail and attacks can confirm
  short previews whose small null sample gives a broad statistical spread. General
  match thresholds remain unchanged; this extra path requires similarity and hold-out
  similarity ≥0.90, detail ≥0.85, onset ≥0.80, and the existing probable Z threshold.
- A5–A7: uploads time out on stalled progress, resume at committed offsets, keep their
  completion when the queue fills, and reserve outstanding disk space atomically.
- A8: throttle waits stop when they exceed the remaining budget; network waits are bounded.
- A9: disk writes/fsync run off the event loop; locks clean up after holders and waiters;
  gateway errors have a plain retry message; clue retries have separate limits; index
  pruning uses current file existence within its write transaction.

The origin/header checks, signed cookies, Turnstile verification, ordered idempotent
parts, networkless sandbox decode and admin-only audio boundary remain in place.

## Measured before deployment

Both the private implementation and reviewed public export must pass the same suite:
65 Python tests and eight frontend tests. Regressions cover A1–A8, independent retry
limits, resume ownership, index pruning and the new confirmation/error flow.

- [Cold title-only discovery](../benchmarks/cold-title-v0.2.0.json): public Pacific Six
  sample at 92.6% speed, empty cache, no performer clue; exact recording in **79.64 s**
  on a local Mac. Bandcamp returned 125 candidates; the winner was the 62nd completed
  comparison. No recognition fingerprint/excerpt was needed. One other reference
  failed to load and was recorded as a structured issue without replacing the exact match.
- [Public preview pairs](../benchmarks/public-preview-pairs-v0.2.0.json): **9/9** positives
  exact, including Hugh Laurie clean/slowed; **0/18** cross-recording false exacts.
- [Synthetic benchmark](../benchmarks/synthetic-v0.2.0.json): **11/12** expected outcomes;
  the known `6079-alternate` false exact remains. No new false exacts in these evaluations.
- [Throttled actual HTTP upload](../benchmarks/upload-transport-v0.2.0.json): 8 MiB in
  **128.52 s at 0.522 Mbit/s**, preceded by an interrupted-part rollback, then successful
  resume and completion on the local HTTP server. Frontend tests separately simulate a
  lost response after server commit and confirm resume from the next part.

These checks use public samples or generated data. The 40 private known-reference
verification results supplied by the owner were not rerun and are not cold-search evidence.
The preview positives use transformations of the same audio; this is not a blind field set.

## Live deployment verification

Pending the idle-queue deployment and public-site browser checks; this section will be
updated with observed results before the v0.2.0 release is published.

## Remaining work

The optional A9 per-hash posting cap is deferred until a threshold can be measured
against retrieval recall; arbitrary common-hash removal risks hiding rare recordings.
Actual Android background suspension/carrier changes and broader human-labelled
recording-level accuracy remain unverified. Provider ranking and availability can change.

The owner has not selected a reuse license; no license or license metadata was added.
Private GitHub vulnerability reporting is enabled; see [SECURITY.md](../SECURITY.md).
The website has a share-preview image. Setting the repository's social preview is a
manual owner step because the installed `gh repo edit` does not expose that setting:
upload `songfinder/static/share.png` in repository Settings → General → Social preview.
