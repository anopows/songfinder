# How matching works

Songfinder looks for a recorded performance, not just a composition title. Another
band playing the same arrangement is a particularly difficult negative example.

## Candidate discovery

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

It then checks alignment similarity, separation from wrong offsets, coverage and timing
stability in shorter segments. Timing drift helps distinguish a related arrangement from
the same recording. The CLI can report exact, probable, related or no match; the current
web result formatter exposes exact, probable and no match, with errors handled separately.

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

The release has 25 automated tests and two live public-sample smoke checks: a normal-speed
recording and a pitch-preserving slowed recording. Tests cover matcher behavior, link
handling, upload sessions and ownership, chunk retries, rate budgets, retention and cached
references beyond the online candidate limit.

Development also investigated a small collection of real clips, finding problems with
same-arrangement false positives, artist credits, previews and provider throttling. That
collection was used while improving the algorithm and expanding its reference cache.
Results on it are not a blind estimate of accuracy on unfamiliar recordings.

See the [roadmap](roadmap.md) for independent evaluation and verification improvements.
