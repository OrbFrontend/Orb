# Assistant bench

Model `google/gemma-4-31b-it` at `https://nano-gpt.com/api/v1`, commit `0788671b20` with uncommitted backend changes. 450 scored runs; 0 unscored (provider or harness error, or an arm the endpoint cannot run).

Accurate: the first tool chosen was in the request's acceptable set, and every argument call was usable and valid on its first attempt. Success: the request's own outcome check passed.

| Arm | Mode | Runs | Accurate | Right tool first | Args first try | Success | Hit a run limit | Retries / run | Invalid args / run | Model calls / run | Prompt tokens / run | Median wall s |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| two-default | native,structured | 150 | 94.0% | 100.0% | 94.0% | 92.0% | 0.7% | 0.03 | 0.03 | 4.24 | 13,212 | 14.3 |
| two-native | native | 150 | 90.7% | 98.0% | 92.7% | 92.0% | 1.3% | 0.06 | 0.03 | 4.17 | 12,846 | 13.4 |
| single-native | native | 150 | 99.3% | 99.3% | 100.0% | 97.3% | 6.0% | 0.00 | 0.00 | 3.20 | 20,958 | 8.7 |

## Single-call against two-call

Paired over requests (each request's rate over its repeats); bootstrap 95% interval.

| Against | Metric | Single − two-call | 95% interval |
| --- | --- | ---: | --- |
| two-default | accurate | +5.3 pts | +0.7 to +11.3 |
| two-default | success | +5.3 pts | -3.3 to +14.7 |
| two-native | accurate | +8.7 pts | +2.0 to +17.3 |
| two-native | success | +5.3 pts | -2.7 to +14.7 |

## By kind of request

| Kind | two-default accurate / success | two-native accurate / success | single-native accurate / success |
| --- | ---: | ---: | ---: |
| read | 98.0% / 90.0% | 92.0% / 86.0% | 100.0% / 92.0% |
| write | 85.0% / 92.5% | 87.5% / 97.5% | 100.0% / 100.0% |
| edit | 95.0% / 90.0% | 85.0% / 90.0% | 100.0% / 100.0% |
| edit3 | 100.0% / 100.0% | 90.0% / 100.0% | 90.0% / 100.0% |
| edit_long | 100.0% / 90.0% | 90.0% / 80.0% | 100.0% / 100.0% |
| media | 90.0% / 90.0% | 100.0% / 100.0% | 100.0% / 100.0% |
| handoff | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% |

## By request (successes / runs)

| Request | two-default | two-native | single-native |
| --- | ---: | ---: | ---: |
| read_characters | 5/5 | 5/5 | 5/5 |
| read_lore | 5/5 | 5/5 | 5/5 |
| read_last_reply | 5/5 | 5/5 | 5/5 |
| read_persona | 5/5 | 5/5 | 5/5 |
| read_document | 5/5 | 4/5 (no_tool_call 1) | 5/5 |
| read_settings | 5/5 | 4/5 (reply 1) | 1/5 (reply 4) |
| read_count | 5/5 | 5/5 | 5/5 |
| read_chat_world | 5/5 | 5/5 | 5/5 |
| read_personality | 5/5 | 5/5 | 5/5 |
| read_group | 0/5 (reply 5) | 0/5 (no_tool_call 1, reply 4) | 5/5 |
| write_director_off | 5/5 | 5/5 | 5/5 |
| write_workflows_off | 5/5 | 5/5 | 5/5 |
| write_world | 5/5 | 5/5 | 5/5 |
| write_entry | 5/5 | 5/5 | 5/5 |
| write_persona | 4/5 (no_tool_call 1) | 5/5 | 5/5 |
| write_character | 4/5 (limit 1) | 4/5 (limit 1) | 5/5 |
| write_chat_world | 4/5 (no_tool_call 1) | 5/5 | 5/5 |
| write_checkpoint | 5/5 | 5/5 | 5/5 |
| edit_entry_name | 5/5 | 5/5 | 5/5 |
| edit_persona | 4/5 (reply 1) | 3/5 (no_tool_call 2) | 5/5 |
| edit_scenario | 4/5 (no_tool_call 1) | 5/5 | 5/5 |
| edit_word | 5/5 | 5/5 | 5/5 |
| edit_typos | 5/5 | 5/5 | 5/5 |
| edit_three_names | 5/5 | 5/5 | 5/5 |
| edit_long_word | 5/5 | 5/5 | 5/5 |
| edit_long_cut | 4/5 (reply 1) | 3/5 (no_tool_call 1, reply 1) | 5/5 |
| media_image | 4/5 (no_tool_call 1) | 5/5 | 5/5 |
| media_speech | 5/5 | 5/5 | 5/5 |
| handoff_regenerate | 5/5 | 5/5 | 5/5 |
| handoff_rewrite | 5/5 | 5/5 | 5/5 |

## Failures by tool

**two-default**

- Wrong or unreadable first choice: none
- Unusable replies (retried): `read_messages` 1, `query_library` 1, `edit_character` 1, `list_worlds` 1, `create_persona` 1
- Invalid arguments (refused): `create_character` 4
- Tool refusals (target, find text, limits): `edit_character` 15, `edit_persona` 1
- Demotions learned mid-run: structured_ignored:google/gemma-4-31b-it 145

**two-native**

- Wrong or unreadable first choice: `None` 3
- Unusable replies (retried): `step` 3, `list_personas` 3, `get_character` 1, `read_document` 1, `query_library` 1
- Invalid arguments (refused): `create_character` 5
- Tool refusals (target, find text, limits): `edit_character` 9, `edit_message` 1

**single-native**

- Wrong or unreadable first choice: `edit_character` 1
- Unusable replies (retried): none
- Invalid arguments (refused): none
- Tool refusals (target, find text, limits): `edit_character` 52

## Library

Kestrel's description: 7,577 characters. Switches at start: `{"main_workflows_enabled": true, "tools": {"direct_scene": true, "editor_apply_patch": false, "editor_rewrite": false}, "length_guard": {"enabled": false, "enforce": false, "max_words": 240, "max_paragraphs": 4}, "agentic_lorebook": false, "director_individual_fragments": false, "prevent_prompt_overrides": false, "editor_audit_checks": {"banned_phrases": {"stored": true, "effective": false}, "repe`
