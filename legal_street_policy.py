"""Shared instructions for reading NYC street names in legal descriptions."""

NYC_STREET_INSTRUCTION = (
    'The work is in New York City: Staten Island, Brooklyn, Manhattan, Queens, and the Bronx. '
    'Determine the borough from the supplied legal description if it is stated; do not invent one. '
    'Street names can contain typos or inconsistent spellings. Compare repeated mentions and any '
    'street-reference evidence actually supplied with the request, in the correct borough. '
    'If a current authoritative street reference is actually provided and it unambiguously confirms '
    'the correct name at the relevant location, use that confirmed spelling. Otherwise preserve the '
    'readable name as printed, even if it looks like a typo; do not pretend to have consulted a '
    'directory or the internet, and do not invent a correction from memory or from another borough. '
    'Do not change the legal boundary calls, distances, bearings, or historical street names.'
)
