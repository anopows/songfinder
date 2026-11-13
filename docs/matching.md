# How matching works

Songfinder looks for a recorded performance, not just a composition title. Another
band playing the same arrangement is a particularly difficult negative example.

## Candidate discovery

An optional SQLite landmark index proposes up to 12 cached references first. Relative
pitch and time triplets tolerate speed and pitch changes; the shortlist never supplies a
verdict. A miss falls back to the full cached catalogue and online discovery.
A supplied title searches cached references before online catalogues. Without a title,
optional recognizers propose leads; cached recordings and available public references
are then compared. A recognizer response or matching title is not sufficient evidence
for an exact match. Provider requests, candidate expansion and processing time are bounded.

Reference sources include Bandcamp, Internet Archive, SoundCloud, iTunes, Deezer and
optionally YouTube. The hosted site disables online YouTube fetching because of server-side
bot checks. Reference features are cached locally; audio is not stored in that feature cache.

## Audio verification

The matcher represents audio as normalized log-frequency spectrograms. It searches
approximately 70–140% playback speed, both with pitch preserved and with pitch changing
along with speed. Multiple passages must agree with one time mapping:

`reference time = offset + rate × clip time`

Discovery and confirmation passages are disjoint, with a guard interval. Only discovery
passages can refine the speed, pitch and offset. Confirmation uses the fixed mapping to
measure spectral similarity, adjacent-band detail and positive spectral changes (attacks).
Whole-clip wrong-offset separation, coverage and timing stability remain additional gates.
These tests reduce ambiguity but cannot guarantee rejection of every alternate performance.
The website distinguishes exact, probable, related, unconfirmed, sources unavailable,
timeout, cancelled and no match. Displayed identity always comes from the matched candidate.

The displayed speed is relative to the selected reference. Transfers of an old recording
can differ slightly in speed even when they contain the same performance. The start
position is omitted when only a preview was available for comparison.

## Listening links

Spotify is preferred when the resolver confirms the recording identity. Links can be
supported by a preview comparison, the directly matched audio, or recording metadata
such as an ISRC. Different releases and masters of the same performance are not always
distinguishable. The availability of a link is not a guarantee that the full track plays
in every country.

## Validation status

Automated tests cover matcher behavior, disjoint confirmation, retrieval fallback, link
handling, label provenance, ownership renewal, chunk retries, cancellation, rate budgets,
retained-audio expiry, private routes and database restoration. Recorded results and
limitations are in the [release validation](release-2026-11-13.md).

Development also investigated a small collection of real clips, finding problems with
same-arrangement false positives, artist credits, previews and provider throttling. That
collection was used while improving the algorithm and expanding its reference cache.
Results on it are not a blind estimate of accuracy on unfamiliar recordings.

See the [roadmap](roadmap.md) for independent evaluation and verification improvements.
