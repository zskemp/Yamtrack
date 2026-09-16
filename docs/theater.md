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
Imports accept only manual or Wikidata Theater sources. Wikidata IDs must be
syntactically valid QIDs within the catalog's 36-character limit; this offline
check does not certify that an entity exists or establish alias equivalence.
Season and episode columns must be blank for Theater works. Invalid identities
are reported and skipped before overwrite bookkeeping, so later valid rows can
still restore without changing existing attendance for the rejected work.
Artwork metadata is also JSON in CSV exports. Provider images are restored only
with matching work identity, HTTPS Wikimedia URLs and an allowed license credit.
Older or malformed credits fall back to a missing image without losing attendance.

## Provider Access

Wikidata's public Action API supplies work metadata under CC0 without credentials.
Search uses supported label/alias and statement filters, explicit Play, Musical,
Opera and Ballet classification, and batched creator labels. Unknown forms are
not guessed as Other. Manual creation remains available for unresolved works.
Reviewed specific types include Italian opera, revue and pantomime. Named genre
evidence is accepted only alongside recognized stage-work types. Production-only,
performance, radio/audio drama and television-play classifications override form
claims. Broad work types alone never imply a named form or Other. Specific types
can follow up to three P279 parent links, with at most 50 distinct classes shared
across the search. Reviewed work/form/medium anchors stop traversal: some valid
musical-work classes have production ancestors that must not override work-level
identity. Cycles, missing/deprecated ancestry and over-budget paths remain
unresolved; production or broadcast conflicts generally win. Explicit work types
remain usable when an unrelated secondary ancestry path is unresolved.
One direct-type exception permits a theatrical-production P31 statement alongside
a direct reviewed stage-work P31 statement. Literary work alone and subtype-only
work evidence do not qualify for this exception. Production subclasses, production
claims in form/genre fields, performing-arts productions, performances and screen
media remain excluded. A producer-plus-cast-plus-director fingerprint excludes
the record even with direct stage-work evidence. This admits mixed records such
as Dear Evan Hansen without title-specific exceptions or merging identities.
It favors discoverability but can admit incorrectly classified productions whose
staging metadata is incomplete; it is not proof that every accepted entity is a
work. Classification version 5 and search cache version 10 refresh older results.
Class parents and source revisions are cached for one hour. Search and details use
the same classification version. When statement-filtered discovery is exhausted
with fewer than 20 eligible works and request budget remains, one literal
title/alias fallback batch uses the same classifier within the existing
150-candidate ceiling. Filtered works retain their order; newly verified fallback
works append without duplicating identities. A full page or three spent batches
prevents backfill. An optional fallback lookup failure preserves already-found
works with the limited-search notice and does not cache the partial result.
Identity conflicts still fail, and failed searches without usable results retain
normal error behavior. The search
limit notice also appears when the class-lookup budget is exhausted.

Search inspects at most 150 provider candidates, preserves provider order and
deduplicates actual redirect targets before pagination. A visible notice identifies
truncated searches. Search results are cached for 15 minutes; work metadata for
one hour. Provider failures are shown through the existing error page. Rate-limit
responses terminate the request rather than retry indefinitely. Saved attendance
status and notes can be edited without an external metadata lookup.
Theater works do not generate release-calendar events.

When the English/fallback response has no label, eligible works and their related
creators/languages share one optional label-only request of at most 50 IDs. Work
titles take priority over related labels. The response may supply any language;
English wins if present, otherwise the lowest language code is chosen for stable
display. Source titles are not translated or inferred. Each label result is cached
for one hour, including confirmed absence. Remaining works keep their QID and stay
visible; a later request can use cached labels to reach further missing labels.
Failures and malformed/mismatched entity records do not block tracking, and
incomplete label enrichment prevents caching the final search/detail response.
Only labels are read from this fallback; it cannot change claims or establish
redirects. Label metadata version 1 and search cache version 11 refresh old
QID-only results.

## Artwork

### Wikipedia Article Thumbnails

Theater now prefers the representative image selected by the exact work's
Wikipedia article, including non-free posters under the operator-selected policy.
Wikidata sitelinks identify articles; the returned article must independently
report the same QID and not be a disambiguation page. An article association is
source evidence, not proof that every lead image is the ideal poster.

Displayed works are batched by site, trying English then up to two other available
sites from French, German, Spanish, Italian and Dutch. One article request and one
file request per site obtain PageImages (`pilicense=any`) and current imageinfo.
At most twelve calls are scheduled within twenty seconds, each waiting at most
eight seconds. Commons fallback retains its own existing page budget. These are
scheduling/network-wait bounds, not a guarantee against transport throttling.
Only returned Wikimedia still-image thumbnails up to 300 pixels wide are used;
no higher-resolution image or older upload is fetched. Files actually hosted by
Commons are identified through the originating wiki's shared-repository metadata,
not substituted solely because a Commons filename matches.

Complete selections and confirmed absence cache for one hour, keyed by work
revision and sitelinks. Incomplete per-work responses remain retryable without
discarding successfully processed neighbors. Missing articles/images or rejected
media fall back to the existing Commons resolver. Temporary Wikipedia failures
preserve saved images/credits and prevent failed refreshes from persisting.
Explicit metadata sync invalidates the Wikipedia selection cache.

Credits retain the article, file, source rights fields, license link, retrieval
time and file hash. Non-free images are explicitly labeled "Non-free; Wikipedia
rationale", with a notice that Wikipedia's use-specific rationale is not a
transferable license and does not establish fair use in another context or
jurisdiction. API availability, attribution and reduced size do not by themselves
establish a right to reuse. Operators remain responsible for their display policy;
this implementation does not certify legal clearance or change image ownership.

Own-data CSV exports retain source assertions and non-free status, not image bytes
or a transferable fair-use license. Offline restore validates consistent source
metadata and notices; altered records omit imagery without losing attendance.
Known canonical redirects retain the original artwork evidence subject. Restore
does not establish new global equivalence or independently authenticate an export.

### Commons Fallback

Commons images are eligible through a work's direct P18 image/P154 logo claim or verified
exact-work P180 depiction statements. Enrichment runs only for the displayed
page, not every candidate. Each work examines up to five direct files and, if
none qualify, up to twelve exact-depiction candidates. After this initial coverage
pass across the displayed page, remaining budget can inspect that same depiction
window for works with a qualified direct image but no poster. Only a qualified
depiction poster can replace an existing direct non-poster image. Direct posters
skip this optional lookup. Qualified direct images are reused without fetching
their metadata again; each selection retains its own matching evidence and credit.
Requests are serial and reuse the
provider transport. Successful artwork selections are cached for one hour;
Commons rate-limit responses impose a shared retry cooldown.
File metadata is fetched in groups of three with at most one continuation request
per group. Rights checks run only after that group's metadata is complete; a
revision change or unfinished continuation is unavailable data, not a confirmed
rejection. A later optional batch failure does not discard an image already
qualified from a completed batch. Partial scans are not cached, allowing a later
retry to select a preferred image. No recursive image search is performed.
Image-history continuation is not rights metadata: only the current upload is
used. Template/category continuation retains current file information rather than
following older uploads.

When direct images and exact depictions yield no usable candidate, the first
work-supplied Commons category may provide up to ten direct files. The category
must link back to the exact Wikidata work. An accepted file must independently
describe an illustration, photograph, poster, scene or performance of the exact
work title and theater form by a known creator. This initial description grammar
supports English labels/aliases; short creator names and fuzzy matching are not
used. Wrong forms, negated descriptions, unrelated portraits and rights failures
are rejected. Category ID/revision and the matching evidence are retained with
the image and survive own-data export/restore. Category membership alone never
proves a match. This fallback shares the existing page budget below.
Malformed category responses are unavailable data, not a reason to fail the work
search. A failed exact-depiction lookup can fall back to the category while budget
remains. Restoring category artwork requires its category identity/revision and
internally consistent description, creator, form and original work evidence.
Displayed-page enrichment shares a 24-request Commons budget and stops scheduling
new artwork calls after 20 seconds. Each HTTP wait is limited to at most eight
seconds or the remaining budget, whichever is smaller. Cached qualified artwork
does not consume calls. These are scheduling and network-wait limits, not a hard
wall-clock guarantee covering DNS, shared throttling, retries or Wikidata discovery.
Budget exhaustion retains eligible results and any already-qualified artwork;
later detail requests get a fresh budget. Other providers retain their default
HTTP timeout.
Transient failures preserve already-saved artwork and credit rather than replacing
them with a placeholder. Explicit metadata sync invalidates the work's Commons
selection and refuses to overwrite artwork when that lookup is unavailable.

Accepted grants are CC0 1.0, CC BY 2.0/4.0 and CC BY-SA 3.0/4.0, with matching
license-template evidence and supplied artist attribution. Explicit PD-textlogo
and PD-old-auto/70/100-expired bases are supported when Commons also identifies
the file as not copyrighted; the named basis and jurisdiction caveats are retained.
No generic "Public domain" string is treated as sufficient evidence.
The explicit Commons PD-US tag is also accepted with a named creator and a
publication-source credit containing a year. This is a metadata-completeness
screen, not a determination of publication date or copyright expiration. It does
not use upload timestamps or creation dates to infer public-domain status. The
source's general US assessment does not identify which legal basis applies; its
notice warns that copyright may remain outside the United States, especially
where the rule of the shorter term does not apply. Creator and publication credit
remain visible and portable, along with this notice. Missing context, generic
PD-old alone and actual copyright/permission warnings remain insufficient.
Restore validates the grant recorded in an export rather than replacing it with
today's preferred basis. A previously supported CC grant remains valid when its
source also lists PD-US, provided its own grant evidence and full notices remain
intact. Merely retaining a CC label without its source template is insufficient.
See the [Commons PD-US documentation](https://commons.wikimedia.org/wiki/Template:PD-US).
The specifically asserted PD-US-dust-jacket basis is also supported: it concerns
the jacket's US notice-formality status, not the book text or worldwide rights.
Supplied permission text and PD-Art reproduction caveats remain visible/exported.
Valid older PD-Art exports gain the new reproduction caveat during offline restore
after their previously expected notice is checked; current notices cannot be
silently omitted. This migration does not change the source grant or work identity.
Generic PD-old files without explicit publication evidence are not silently
promoted to a US-expired basis from creation dates, EXIF or upload timestamps.

The catalog-reuse policy favors coverage using published source assessments.
Standard personality, trademark and costume notices accompany the image rather
than automatically disqualifying it; they do not authorize endorsement, commercial
promotion or isolated design reuse. Actual missing-permission, deletion, failed
license-review and copyright-dispute warnings still exclude images. Unknown
substantive restrictions are withheld; ordinary layout/language templates do not
override an explicit grant. Migrated-license disclaimer references and supplied
notice URLs are preserved through display and export.
Expiry claims with missing-date warnings are rejected. Migrated-license files
require the actual rendered file-specific disclaimer, not just a generic license
template link. Older CC-only exports retain their original grant/author checks;
new public-domain exports require complete, consistent basis and notice evidence.
Exact depiction candidates also require affirmative performance/illustration descriptions;
advertisements not explicitly described as work posters, audiences, adaptations
and isolated set designs are excluded.

Within each discovered candidate set, affirmative English "poster for/of"
descriptions take priority over file/object-title poster hints; title hints apply
only when a description is absent. Remaining ties prefer a 2:3 portrait ratio,
then stable file ID. Incidental poster mentions do not establish poster preference.
An explicitly described work poster may mention its advertisement purpose, but
wrong-medium/adaptation and rights checks still apply. Poster upgrades share the
same page-wide budget and never take calls ahead of initial image coverage.
Upgrade failures preserve the qualified direct image and leave the upgrade
retryable; complete negative scans cache the original selection for one hour.
Malformed search hits or MediaInfo statement envelopes are unavailable evidence,
not completed negative scans; they preserve the direct image and permit retry.
Category discovery remains an absence-only fallback, not an upgrade source.
Selection caches refresh with policy version 11; valid version-8/9/10 exports remain
restorable without a provider request.

Images use returned thumbnails without cropping. Image credit controls accompany
search, details, library, home, history and list covers, preserving title, artist,
source, license and supplied attribution/permission notices. Credits are escaped
text, not provider HTML. Failures retain a stable frame and missing-image state.
Compact notification-settings rows omit licensed thumbnails rather than display
them without attribution. Offline restore requires a complete, consistent rights
record; it cannot independently certify the truth of user-supplied export data.
No image uploads, user-specific overrides or manual-title image matches are added.

This is a source-assessed catalog reuse policy, not a guarantee that contributed
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

Provider classification uses reviewed anchors and bounded subclass resolution,
not an unrestricted ontology traversal. Some valid works with incomplete classifications
will require manual entry. Production/granularity conflicts require further review.

For ballet, ordinary versions and re-choreographies should share a work when the
underlying relationship is verified; clearly authored reinventions remain distinct
adaptations. Derivation alone does not prove equivalence. Current source data does
not provide a reliable general mapping for these non-redirect grouping cases.
In particular, a version/edition property is not by itself a choreography mapping:
observed P629 ballet-parent records include libretto editions. The named Raisin
staging conflict and the Swan Lake/Nutcracker variants have request regressions
that retain their supported granularity rather than infer equivalence.

## Sources

- [Wikidata access](https://www.wikidata.org/wiki/Wikidata:Data_access)
- [Supported search filters](https://www.mediawiki.org/wiki/Help:Extension:WikibaseCirrusSearch)
- [Entity API](https://www.wikidata.org/wiki/Special:ApiHelp/wbgetentities)
- [Wikidata licensing](https://www.wikidata.org/wiki/Wikidata:Licensing)
- [Wikimedia API etiquette](https://www.mediawiki.org/wiki/API:Etiquette)
- [Commons reuse obligations](https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia)