# Roadmap and shipped work

## Shipped

- Confirmed recording identity and performer-aware links; recognizer hints cannot replace the winning performer.
- Persistent relative-pitch/time fingerprint shortlist, followed by audio verification. Full-cache fallback runs after normal and speed-corrected recognition and the fallback recognizer.
- Separate discovery and active-audio hold-out passages, detail/attack checks, and a tightly constrained near-identity path for short previews.
- Expanded, spaced Bandcamp title-only queries with a separate full-length candidate allowance. No individual song is hard-coded.
- Structured provider-issue events, distinct incomplete/no-match states, and recognition waits bounded by the remaining job budget.
- Expiring upload reservations, stable ownership across session renewal, stall-only upload timeouts, committed-offset resume, asynchronous disk writes, and lock cleanup.
- Disk admission accounts for outstanding uploads. Admitted completed uploads keep their place even if the search queue fills.
- Owner-authorized retries with separate limits, cancellation, Yes/No feedback, automatic clue opening after No, and one recent-search row per original clip.
- Upload-first confirmation flow, phone/desktop layout, permanent public example, sharing image, favicon, private admin metrics and deletion, and SQLite backup/restore validation.
- Seeded/confirmed reference protection and index pruning that checks current files while holding its write transaction.

See [v0.2.0 evidence](release-v0.2.0.md) and [initial public release](release-2026-11-13.md).

## Remaining

1. **Independent field evaluation.** Broader human-labelled positives and alternate performances, speech, edits, short excerpts and absent recordings. Separate cold discovery from warm verification. The earlier 40 private clips were known-reference verification, not end-to-end discovery or a blind benchmark.
2. **Known synthetic false exact.** `6079-alternate` remains documented. The current task did not request tuning this case; avoid claims of perfect performance identification.
3. **Index frequency limits (A9, deferred).** A per-hash posting cap needs a measured threshold and retrieval-recall evaluation on a larger catalogue. Arbitrarily dropping common hashes could remove rare recordings from the shortlist. Current shortlist size is bounded; full verification remains authoritative, but postings can still grow.
4. **Real Android network interruption.** The current release tests actual throttled HTTP transfer, mocked response loss and desktop Chrome at phone width. This does not establish behavior through Android background suspension or a mobile carrier change.
5. **Speech/edit-aware selection and additional sources.** Activity-aware windows now handle quiet passages; speech/music separation and edit boundaries require further evaluation. Catalogue coverage and provider availability remain limiting factors.
6. **Repository social preview (manual owner step).** The installed `gh repo edit` has no social-preview image option. In GitHub repository Settings → General → Social preview, upload `songfinder/static/share.png`. The website already serves that image in its sharing metadata.
