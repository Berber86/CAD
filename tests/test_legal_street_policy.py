"""Prompt describes NYC but does not claim access to a street directory."""
import llm_parser
import multiple_description_parse as md
from legal_street_policy import NYC_STREET_INSTRUCTION


def test_policy_is_in_both_parser_instructions_without_changing_boundary_rules():
    assert NYC_STREET_INSTRUCTION in llm_parser.SYSTEM_PROMPT
    assert NYC_STREET_INSTRUCTION in md.system_instruction()
    for borough in ('Staten Island', 'Brooklyn', 'Manhattan', 'Queens', 'the Bronx'):
        assert borough in NYC_STREET_INSTRUCTION
    assert 'actually provided' in NYC_STREET_INSTRUCTION
    assert 'do not pretend' in NYC_STREET_INSTRUCTION
    assert 'preserve the readable name as printed' in NYC_STREET_INSTRUCTION
    assert 'Do not change the legal boundary calls' in NYC_STREET_INSTRUCTION
