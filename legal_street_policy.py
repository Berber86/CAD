"""Shared instructions for reading and correcting street names."""

NYC_STREET_INSTRUCTION = (
    'The work concerns New York City legal descriptions. Street names may contain typos or inconsistent '
    'spellings. Use the supplied descriptions and context to decide whether a street name needs correction; '
    'correct it when sufficiently confident. Do not claim to have consulted a directory or the internet '
    'unless that source was actually supplied. For every main and cross street, return the original name '
    'exactly as read in main_street or cross_street, and return a corrected name separately in '
    'main_street_corrected or cross_street_corrected only when you change the spelling. When no correction '
    'is made, return null for the corrected field. Never replace or omit the original name when correcting. '
    'If the original cannot be read, do not invent it. Do not alter legal boundary calls, measurements, '
    'bearings, or historical street names to make a correction.'
)
