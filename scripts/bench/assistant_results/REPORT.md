# Assistant bench

Model `google/gemma-4-31b-it` at `https://nano-gpt.com/api/v1`, commit `0faa359148` with uncommitted backend changes. 420 scored runs; 0 unscored (provider or harness error, or an arm the endpoint cannot run).

Accurate: the first tool chosen was in the request's acceptable set, and every argument call was usable and valid on its first attempt. Success: the request's own outcome check passed.

| Arm | Mode | Runs | Accurate | Right tool first | Args first try | Success | Hit a run limit | Retries / run | Invalid args / run | Model calls / run | Prompt tokens / run | Median wall s |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| two-default | native,structured | 210 | 88.1% | 93.8% | 92.9% | 90.0% | 0.0% | 0.09 | 0.00 | 4.16 | 15,307 | 13.3 |
| single-native | native | 210 | 99.5% | 99.5% | 100.0% | 97.6% | 0.0% | 0.01 | 0.00 | 2.52 | 21,314 | 9.1 |

## Single-call against two-call

Paired over requests (each request's rate over its repeats); bootstrap 95% interval.

| Against | Metric | Single − two-call | 95% interval |
| --- | --- | ---: | --- |
| two-default | accurate | +11.4 pts | +6.2 to +17.1 |
| two-default | success | +7.6 pts | +2.4 to +13.3 |

## By kind of request

| Kind | two-default accurate / success | single-native accurate / success |
| --- | ---: | ---: |
| read | 81.5% / 87.7% | 98.5% / 93.8% |
| write | 90.0% / 88.6% | 100.0% / 98.6% |
| edit | 80.0% / 88.0% | 100.0% / 100.0% |
| edit3 | 100.0% / 100.0% | 100.0% / 100.0% |
| edit_long | 100.0% / 80.0% | 100.0% / 100.0% |
| media | 100.0% / 100.0% | 100.0% / 100.0% |
| handoff | 95.0% / 100.0% | 100.0% / 100.0% |

## By request (successes / runs)

| Request | two-default | single-native |
| --- | ---: | ---: |
| read_characters | 4/5 (no_tool_call 1) | 5/5 |
| read_lore | 5/5 | 5/5 |
| read_last_reply | 5/5 | 5/5 |
| read_persona | 5/5 | 5/5 |
| read_document | 4/5 (no_tool_call 1) | 4/5 (no_tool_call 1) |
| read_settings | 5/5 | 2/5 (reply 3) |
| read_count | 5/5 | 5/5 |
| read_chat_world | 5/5 | 5/5 |
| read_personality | 5/5 | 5/5 |
| read_group | 1/5 (reply 4) | 5/5 |
| read_earlier | 5/5 | 5/5 |
| read_active_lore | 4/5 (no_tool_call 1) | 5/5 |
| read_state | 4/5 (no_tool_call 1) | 5/5 |
| write_director_off | 4/5 (no_tool_call 1) | 5/5 |
| write_workflows_off | 5/5 | 5/5 |
| write_world | 5/5 | 5/5 |
| write_entry | 4/5 (no_tool_call 1) | 5/5 |
| write_persona | 4/5 (no_tool_call 1) | 5/5 |
| write_character | 4/5 (no_tool_call 1) | 5/5 |
| write_chat_world | 5/5 | 5/5 |
| write_checkpoint | 5/5 | 5/5 |
| write_entry_off | 5/5 | 5/5 |
| write_start_chat | 5/5 | 5/5 |
| write_rename | 4/5 (no_tool_call 1) | 5/5 |
| write_length | 4/5 (no_tool_call 1) | 5/5 |
| write_phrase | 3/5 (no_tool_call 2) | 4/5 (no_tool_call 1) |
| write_world_global | 5/5 | 5/5 |
| edit_entry_name | 4/5 (no_tool_call 1) | 5/5 |
| edit_persona | 4/5 (no_tool_call 1) | 5/5 |
| edit_scenario | 5/5 | 5/5 |
| edit_tags | 5/5 | 5/5 |
| edit_word | 4/5 (reply 1) | 5/5 |
| edit_typos | 5/5 | 5/5 |
| edit_three_names | 5/5 | 5/5 |
| edit_long_word | 5/5 | 5/5 |
| edit_long_cut | 3/5 (reply 2) | 5/5 |
| media_image | 5/5 | 5/5 |
| media_speech | 5/5 | 5/5 |
| handoff_regenerate | 5/5 | 5/5 |
| handoff_rewrite | 5/5 | 5/5 |
| handoff_open | 5/5 | 5/5 |
| handoff_continue | 5/5 | 5/5 |

## Failures by tool

**two-default**

- Wrong or unreadable first choice: `list_active_lore` 5, `read_messages` 3, `list_lorebook_entries` 3, `None` 2
- Unusable replies (retried): `step` 3, `list_active_lore` 2, `read_state` 2, `list_lorebook_entries` 2, `list_personas` 1, `read_document` 1, `set_chat` 1, `add_banned_phrase` 1, `list_worlds` 1, `set_pipeline_toggle` 1, `list_characters` 1, `set_length_guard` 1, `create_persona` 1, `create_character` 1
- Invalid arguments (refused): none
- Tool refusals (target, find text, limits): `edit_character` 13, `edit_message` 2, `list_lorebook_entries` 2
- Demotions learned mid-run: structured_ignored:google/gemma-4-31b-it 202

**single-native**

- Wrong or unreadable first choice: `list_active_lore` 1
- Unusable replies (retried): `step` 2
- Invalid arguments (refused): none
- Tool refusals (target, find text, limits): `edit_character` 10

## Library

Kestrel's description: 7,577 characters. Switches at start: `{"main_workflows_enabled": true, "tools": {"direct_scene": true, "editor_apply_patch": false, "editor_rewrite": false}, "length_guard": {"enabled": false, "enforce": false, "max_words": 240, "max_paragraphs": 4}, "agentic_lorebook": false, "director_individual_fragments": false, "prevent_prompt_overrides": false, "editor_audit_checks": {"banned_phrases": {"stored": true, "effective": false}, "repe`
