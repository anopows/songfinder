# Improvement roadmap

Keep the deployed API and worker while testing recognition changes independently.
The highest priorities are trustworthy results and predictable search time. The original
sequence is retained below; the release status here takes precedence.

## Implemented in the November 13 follow-up

Confirmed labels and performer-aware link hints; an optional fingerprint shortlist with
full-scan fallback; disjoint confirmation with detail/attack checks; owner-authorized
retained-audio retries, artist hints, cancellation and session renewal; distinct incomplete
result states; indexable homepage with sharing metadata; private aggregate metrics and
SQLite backup/restore validation. See [release evidence](release-2026-11-13.md).

The evaluation harness and fresh public-preview comparisons are implemented. A larger
human-labelled field evaluation, speech detection/edit-aware passage selection, actual
Android network-interruption tests and additional providers remain follow-up work.
The frozen synthetic benchmark still contains a false exact match; do not claim perfect
performance-level identification or interpret the earlier investigated 40 clips as blind accuracy.

## 1. Preserve the identity of the confirmed recording

The former web formatter could replace a confirmed candidate's artist/title with an
unverified recognizer hint when the song title matches. A constructed report with a
confirmed Billy May performance and an earlier Charlie Barnet hint reproduces the
wrong displayed artist, even though the reference link still belongs to the winner.
This was a result-label bug; the reproduction does not establish that either artist
was incorrectly matched by the audio engine in that test.

Bind displayed identity to the audio-confirmed candidate. Only adopt recognizer
metadata after establishing that it describes the same recording. Add regression
cases where a known song has multiple performers and the first recognition lead
fails verification.

## 2. Build an independent evaluation set

Freeze the engine and reference catalogue, then evaluate recordings that were not used
to tune thresholds. Separate empty-cache discovery from warm-cache recognition. Include
different performances of the same arrangement, short previews, speech, edits, deliberate
speed changes and music absent from the available catalogue.

Measure false exact matches, recall, median and tail latency, source outages, requests
per clip and resource use. Retain the existing cases as regression tests. Establish
labels by listening rather than accepting another engine's verdict as ground truth.

## 3. Make known recordings faster to retrieve

The current cache scan grows with the catalogue. Introduce a fingerprint index that
returns a small candidate shortlist before the full matcher runs. Prioritize previously
confirmed recordings while retaining broader title lookup. Preserve the full audio
verification gate; retrieval scores alone must not produce an exact result.

Acceptance: shortlist recall and end-to-end accuracy do not regress as the catalogue
grows, and tail latency is measured under the hosted service's CPU and memory limits.

## 4. Strengthen performance-level verification

Reserve disjoint passages for verification that cannot choose or refit the initial
speed/offset estimate. Evaluate fine spectral detail and transient alignment in addition
to the existing timing-spread and wrong-offset checks. Select clean musical passages,
avoid speech where possible, and keep passages on one side of detected edits.

Acceptance: reject convincing alternate performances without losing true matches in
noisy or short recordings. Run each proposed gate as an isolated comparison first;
more thresholds are not automatically better.

## 5. Help users recover from an incomplete search

Expose distinct states for a related arrangement, an unconfirmed title, provider
unavailability, timeout and no useful lead. Offer an owner-authorized retry with an
edited title or artist using retained audio, so visitors need not upload a large video
again. Add cancellation and reliable session renewal for long-lived tabs.

The frontend currently records Yes/No feedback but does not turn a rejected match into
a guided correction flow. Improve that flow before adding more input methods.

## 6. Improve service reliability and phone use

Measure recognition failures, queue latency, resource use and source availability.
Exercise backup restoration, worker recovery, concurrent load alongside other hosted
apps, and real Android upload behavior on interrupted connections. Consider an additional
recognition provider only after measuring which misses it can recover and its cost.

Keep unavailable reference sources optional. A home fetcher is a separate reliability
and access-control project, not a prerequisite for improving the current matcher.

After these checks, consider microphone capture, client-side audio extraction, batch
uploads and a practice handoff to five·six. For discoverability, allow indexing of the
homepage and add link-preview metadata while keeping result and admin URLs unindexed.

## Project maintenance

Keep the public source export synchronized with production, add regression cases for
confirmed failures, document releases, and choose an explicit reuse license before
presenting the project as open source.
