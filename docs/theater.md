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

## Release Limitations

This is not the completed image-rich Theater feature. Provider artwork is withheld
pending work matching, per-file reuse and attribution verification. Manual image
URLs remain supported. There is no guaranteed illustrated-provider coverage.

Only actual Wikidata redirects establish canonical identity. Same-title works,
adaptations and ambiguous ballet variants remain separate; derivative-work claims
do not establish equivalence. Existing saved identities that subsequently redirect
are not destructively merged. Verified non-redirect duplicate/grouping mappings,
their provenance, and alias-aware offline portability remain open acceptance gates.

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