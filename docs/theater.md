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
media remain excluded. Producer, cast and director credits together no longer
exclude an independently identified stage work: that requires direct stage-work
P31, or literary-work P31 plus a direct recognized stage form in P7937, with no
explicit excluded P31. Without that independent evidence, the credit fingerprint
still excludes the record. Explicit production types combined with these credits
also remain excluded. This recovers works whose source records include premiere
personnel, such as The History Boys and Frozen, without title-specific exceptions.
It deliberately tolerates some additional staging or duplicate-looking records
whose source evidence is indistinguishable from a work; these remain separate
identities unless a verified redirect establishes equivalence.
It favors discoverability but can admit incorrectly classified productions whose
staging metadata is incomplete; it is not proof that every accepted entity is a
work. Explicit English descriptions of venues or staged performances and a combined
director/translator/based-on staging pattern also exclude records. These checks
do not infer identity from titles or guarantee accurate classification when source
evidence is incomplete. Classification version 8 and search cache version 16
refresh older results.

Genre ancestry is not always work-medium evidence: jukebox musical, for example,
can descend from both stage musical and musical film in the source ontology.
An inherited film or TV-series conflict in P136 genre is ignored only alongside
independent direct stage-work P31 evidence, or literary-work P31 plus a direct
recognized stage form in P7937. For P7937 form ancestry, the same exception also
requires that the ancestry resolves to a recognized stage form. Genre evidence
alone never supplies that independent identity. Explicit excluded type statements,
P31 medium conflicts, production/performance conflicts and the remaining staging
guards still reject the record. This preserves genuine stage works without admitting
their film or television adaptations or merging distinct authored versions.

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

If the literal search is exhausted with no eligible work, a query beginning with
the English word "The" can use one remaining batch for the title without that
leading word. Matching is case-insensitive for "The" only; normal whitespace
normalization and literal escaping still apply. The same classifier, shared class
budget and three-batch/150-candidate ceiling apply. Existing useful results are
never replaced, unfinished searches are not rewritten, and no further words or
articles are stripped. A failed variant lookup returns a limited, uncached result
so a later request can retry. This is query fallback, not evidence that two work
records are equivalent; provider titles and canonical identities remain unchanged.

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
Classification fetches request only claims and revision information; creator and
language fetches request labels and aliases. Full work records retain the claims,
descriptions and sitelinks needed for discovery and artwork.
Only labels are read from this fallback; it cannot change claims or establish
redirects. Label metadata version 1 retains compatibility with source-language
labels recovered by earlier searches.

## Artwork

### Wikipedia Article Thumbnails

Theater now prefers the representative image selected by the exact work's
Wikipedia article, including non-free posters under the operator-selected policy.
Wikidata sitelinks identify articles; the returned article must independently
report the same QID and not be a disambiguation page. An article association is
source evidence, not proof that every lead image is the ideal poster.

Displayed works are batched by site, trying up to three available articles in the
order English, French, German, Spanish, Italian and Dutch. Each site uses one
article request and, only when filenames are found, one file request to obtain
PageImages (`pilicense=any`) and current imageinfo.
At most twelve calls are scheduled within twenty seconds, each waiting at most
eight seconds. Commons fallback has a separate page budget described below. These are
scheduling/network-wait bounds, not a guarantee against transport throttling.
Only returned Wikimedia still-image thumbnails up to 300 pixels wide are used;
no higher-resolution image or older upload is fetched. Files actually hosted by
Commons are identified through the originating wiki's shared-repository metadata,
not substituted solely because a Commons filename matches.

Complete selections and confirmed absence cache for one hour, keyed by work
revision and sitelinks. Incomplete per-work responses remain retryable without
discarding successfully processed neighbors. Missing articles/images or rejected
media fall back to directly Wikidata-linked Commons files. Temporary Wikipedia failures
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

### Direct-File Commons Fallback

When Wikipedia supplies no accepted image, Commons resolves filenames already
identified by the work's direct P18 image/P154 logo claims, at most five per work.
Only displayed results receive artwork enrichment. There is no exploratory P180
depiction search, P373 category discovery, or additional search to upgrade a photo
to a poster. Missing filenames cause no Commons request.

Uncached filenames are deduplicated across the displayed page and fetched lazily
in groups of at most three. API-provided normalization and redirects associate
metadata with the requested file; selection and work identity remain independent
for each work. Wikipedia successes and valid cached Commons selections do not
enter these batches. A page with only one uncached work retains per-work lookup.
Successful selections are cached for one hour.

File metadata permits one continuation request per group. Rights checks run only
after complete category/template metadata has arrived; changed revisions or an
unfinished continuation are unavailable data, not confirmed rejection. Only the
current upload is used; image-history continuation is not followed.

A failed shared batch is retried individually, once per file. Recovered candidates
remain usable even when another candidate in that work fails; the result is
marked partial and not cached as complete. Commons 429/503 responses impose a
shared cooldown and stop further network recovery. Requests reuse the existing
provider transport and its low-level behavior.

Search pages have a shared **48-call / 30-second Commons scheduling budget**.
Each request timeout is at most eight seconds and bounded by remaining time and
the configured timeout. Shared batches require at least eight calls and sixteen
seconds of headroom; otherwise the current file is fetched independently. This
reduces risk from unrelated files near exhaustion but does not guarantee recovery
through outages. Detail lookups retain a fresh **24-call / 20-second** budget.
These are not hard end-to-end response-time guarantees covering all transport,
parsing, Wikidata and Wikipedia work. Budget exhaustion never hides work results.

Source failures preserve saved images and credits. A refresh with no direct file
does not erase saved artwork. Earlier discovery-derived artwork remains preserved
against a direct-file replacement, while a valid Wikipedia image may replace it.
Explicit metadata sync invalidates source selection caches and refuses unavailable
refreshes. Offline restore continues to validate older depiction/category evidence;
retiring live discovery does not remove its export compatibility. Category records
still require internally consistent work, creator, form and category evidence.

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
For direct files, advertisements not explicitly described as work posters,
audiences, adaptations and isolated set designs are excluded.

Within each direct candidate set, affirmative English "poster for/of"
descriptions take priority over file/object-title poster hints; title hints apply
only when a description is absent. Remaining ties prefer a 2:3 portrait ratio,
then stable file ID. Incidental poster mentions do not establish poster preference.
An explicitly described work poster may mention its advertisement purpose, but
wrong-medium/adaptation and rights checks still apply. No separate poster search
is made. Selection caches use policy version 12; earlier supported policies,
including version 11, remain restorable without a provider request.

Both source paths reject explicit creator headshots and person-only photographs
corroborated by a matching person-role category, object name and location-style
description. Explicit performance context and named-role portraits remain usable.
These are bounded metadata checks, not exhaustive image understanding or new
remote lookups. A rejected image leaves the work discoverable.

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
If a provider response points to an intermediate ID that already redirects locally,
detail and save requests resolve terminal-work metadata before returning it. This
prevents a retired intermediate identity from being recreated by a new attendance.

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
duplicate/grouping mappings and automatic fresh-install alias resolution during
offline restore are explicitly deferred from the approved redirects-only release.

Provider classification uses reviewed anchors and bounded subclass resolution,
not an unrestricted ontology traversal. Some valid works with incomplete classifications
will require manual entry. Production/granularity conflicts require further review.

Broader grouping of ordinary ballet versions and re-choreographies is deferred;
clearly authored reinventions remain distinct adaptations. Derivation alone does
not prove equivalence. Current source data does not provide a reliable general
mapping for these non-redirect grouping cases.
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
