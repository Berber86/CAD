"""Prompt describes NYC but does not claim access to a street directory."""
import llm_parser
import multiple_description_parse as md
from legal_street_policy import NYC_STREET_INSTRUCTION


def test_policy_is_in_both_parser_instructions_without_changing_boundary_rules():
    assert NYC_STREET_INSTRUCTION in llm_parser.SYSTEM_PROMPT
    assert NYC_STREET_INSTRUCTION in md.system_instruction()
    assert 'main_street_corrected' in NYC_STREET_INSTRUCTION
    assert 'cross_street_corrected' in NYC_STREET_INSTRUCTION
    assert 'original name' in NYC_STREET_INSTRUCTION
    assert 'actually supplied' in NYC_STREET_INSTRUCTION
    assert 'Do not alter legal boundary calls' in NYC_STREET_INSTRUCTION
    assert 'Staten Island' not in NYC_STREET_INSTRUCTION
