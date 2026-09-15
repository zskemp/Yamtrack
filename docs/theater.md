# Theater Tracking

Theater catalog entries describe works, independently of a particular production.
Manual creation requires one or more forms: Play, Musical, Opera, Ballet or Other.
Image URLs are optional. Forms belong to the shared work; ratings, notes, venue,
city/location and production/company belong to each user's attendance.

Repeat attendance uses the existing Add new entry control. Date Seen maps to
the existing end date and accepts a date without a performance time. Blank dates
remain unknown. There is no runtime or incremental progress estimate.

Own-data CSV exports encode theater forms as JSON and preserve separate attendance
rows and their personal fields. Imports do not replace shared Theater metadata.
Invalid classifications are reported without overwriting existing attendance.
Theater imports require a title; absent titles are rejected before overwrite
bookkeeping, without attempting a provider lookup.
Artwork metadata is also JSON in CSV exports. Provider images are restored only
with matching work identity, HTTPS Wikimedia URLs and an allowed license credit.
Older or malformed credits fall back to a missing image without losing attendance.

## Provider Access

Wikidata's public Action API supplies work metadata under CC0 without credentials.
Search uses supported label/alias and statement filters, explicit Play, Musical,
Opera and Ballet classification, and batched creator labels. Unknown forms are
not guessed as Other. Manual creation remains available for unresolved works.

Search inspects at most 150 provider candidates, preserves provider order and
deduplicates actual redirect targets before pagination. A visible notice identifies
truncated searches. Search results are cached for 15 minutes; work metadata for
one hour. Provider failures are shown through the existing error page. Rate-limit
responses terminate the request rather than retry indefinitely. Saved attendance
status and notes can be edited without an external metadata lookup.
Theater works do not generate release-calendar events.

## Artwork

Commons images are eligible through a work's direct P18 image claim or verified
exact-work P180 depiction statements. Enrichment runs only for the displayed
page, not every candidate. Each work examines up to three direct files and, if
none qualify, three depiction candidates. Requests are serial and reuse the
provider transport. Successful artwork selections are cached for one hour;
Commons rate-limit responses impose a shared retry cooldown.
Transient failures preserve already-saved artwork and credit rather than replacing
them with a placeholder. Explicit metadata sync invalidates the work's Commons
selection and refuses to overwrite artwork when that lookup is unavailable.

Accepted grants are CC0 1.0, CC BY 2.0/4.0 and CC BY-SA 3.0/4.0, with matching
license-template evidence and meaningful artist attribution. Files with known
permission, deletion, personality-rights, trademark or costume warnings are
withheld. Other public-domain claims are not guessed safe.
Unknown file templates, including unhandled disclaimer templates, require review
and are withheld. Supplied notice links are retained as printable URLs.
Exact depiction candidates also require affirmative performance/illustration descriptions;
advertisements, audiences, adaptations and isolated set designs are excluded.

Images use returned thumbnails without cropping. Image credit controls accompany
search, details, library, home, history and list covers, preserving title, artist,
source, license and supplied attribution/permission notices. Credits are escaped
text, not provider HTML. Failures retain a stable frame and missing-image state.
Compact notification-settings rows omit licensed thumbnails rather than display
them without attribution. Offline restore requires a complete, consistent rights
record; it cannot independently certify the truth of user-supplied export data.
No image uploads, user-specific overrides or manual-title image matches are added.

This is a conservative automated reuse policy, not a guarantee that contributed
metadata is correct or that every third-party right has been cleared.

## Verified Redirects

Wikidata redirect responses with an explicit source/target pair and target revision
are retained as read-only identity evidence. Resolving a saved alias reconciles it
with the canonical work in a transaction. Each user's attendance ID, personal
fields and history remain distinct. List memberships and notification exclusions
move with the work; duplicate list membership retains its earlier addition date.
Unexpected references or contradictory evidence abort the transaction.

Original redirect edges remain recorded when a later redirect forms a chain.
Old URLs, cached searches, list/history lookup and explicit sync resolve that
chain consistently. Qualified artwork can move to an imageless canonical work,
with its original evidence subject retained separately from its current work ID.
Redirect transactions are serialized on PostgreSQL and SQLite; concurrent target
creation during rename is retried as reconciliation rather than data deletion.

Own-data imports resolve aliases already verified by this installation without
provider access. Re-export uses canonical IDs. Uploaded files cannot assert new
global equivalences: an old export imported into a fresh installation retains its
unverified provider ID until that installation observes a redirect. Keep a full
database backup when the locally observed redirect evidence itself must move.

## Release Limitations

This is not the completed image-rich Theater feature. Representative live grids
still contain many missing images; the mostly-illustrated acceptance target is
not met. Manual image URLs remain supported. Broader matching, warning coverage
and visual review remain necessary before release.

Only actual Wikidata redirects establish canonical identity. Same-title works,
adaptations and ambiguous ballet variants remain separate; derivative-work claims
do not establish equivalence. Saved identities are reconciled only for observed
redirects, never by title, composer or image availability. Verified non-redirect
duplicate/grouping mappings and fresh-install legacy-alias resolution remain open
acceptance gates.

Provider classification currently uses explicit supported forms rather than an
unbounded ontology traversal. Some valid works with incomplete classifications
will require manual entry. Production/granularity conflicts require further review.

## Sources

- [Wikidata access](https://www.wikidata.org/wiki/Wikidata:Data_access)
- [Supported search filters](https://www.mediawiki.org/wiki/Help:Extension:WikibaseCirrusSearch)
- [Entity API](https://www.wikidata.org/wiki/Special:ApiHelp/wbgetentities)
- [Wikidata licensing](https://www.wikidata.org/wiki/Wikidata:Licensing)
- [Wikimedia API etiquette](https://www.mediawiki.org/wiki/API:Etiquette)
- [Commons reuse obligations](https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia)