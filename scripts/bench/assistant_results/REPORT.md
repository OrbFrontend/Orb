# Assistant bench

Model `google/gemma-4-31b-it` at `https://nano-gpt.com/api/v1`, commit `e91db0f1a8` with uncommitted backend changes. 420 scored runs; 0 unscored (provider or harness error, or an arm the endpoint cannot run).

Accurate: the first tool chosen was in the request's acceptable set, and every argument call was usable and valid on its first attempt. Success: the request's own outcome check passed.

| Arm | Mode | Runs | Accurate | Right tool first | Args first try | Success | Hit a run limit | Retries / run | Invalid args / run | Model calls / run | Prompt tokens / run | Median wall s |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| two-default | native,structured | 210 | 98.1% | 98.1% | 100.0% | 97.6% | 0.0% | 0.00 | 0.00 | 4.04 | 13,285 | 13.5 |
| single-native | native | 210 | 100.0% | 100.0% | 100.0% | 100.0% | 0.0% | 0.00 | 0.00 | 2.45 | 16,689 | 8.4 |

## Single-call against two-call

Paired over requests (each request's rate over its repeats); bootstrap 95% interval.

| Against | Metric | Single − two-call | 95% interval |
| --- | --- | ---: | --- |
| two-default | accurate | +1.9 pts | +0.0 to +4.3 |
| two-default | success | +2.4 pts | +0.0 to +5.7 |

## By kind of request

| Kind | two-default accurate / success | single-native accurate / success |
| --- | ---: | ---: |
| read | 98.5% / 95.4% | 100.0% / 100.0% |
| write | 100.0% / 100.0% | 100.0% / 100.0% |
| edit | 92.0% / 100.0% | 100.0% / 100.0% |
| edit3 | 90.0% / 90.0% | 100.0% / 100.0% |
| edit_long | 100.0% / 90.0% | 100.0% / 100.0% |
| media | 100.0% / 100.0% | 100.0% / 100.0% |
| handoff | 100.0% / 100.0% | 100.0% / 100.0% |

## By request (successes / runs)

| Request | two-default | single-native |
| --- | ---: | ---: |
| read_characters | 5/5 | 5/5 |
| read_lore | 5/5 | 5/5 |
| read_last_reply | 5/5 | 5/5 |
| read_persona | 5/5 | 5/5 |
| read_document | 5/5 | 5/5 |
| read_settings | 5/5 | 5/5 |
| read_count | 5/5 | 5/5 |
| read_chat_world | 5/5 | 5/5 |
| read_personality | 5/5 | 5/5 |
| read_group | 2/5 (reply 3) | 5/5 |
| read_earlier | 5/5 | 5/5 |
| read_active_lore | 5/5 | 5/5 |
| read_state | 5/5 | 5/5 |
| write_director_off | 5/5 | 5/5 |
| write_workflows_off | 5/5 | 5/5 |
| write_world | 5/5 | 5/5 |
| write_entry | 5/5 | 5/5 |
| write_persona | 5/5 | 5/5 |
| write_character | 5/5 | 5/5 |
| write_chat_world | 5/5 | 5/5 |
| write_checkpoint | 5/5 | 5/5 |
| write_entry_off | 5/5 | 5/5 |
| write_start_chat | 5/5 | 5/5 |
| write_rename | 5/5 | 5/5 |
| write_length | 5/5 | 5/5 |
| write_phrase | 5/5 | 5/5 |
| write_world_global | 5/5 | 5/5 |
| edit_entry_name | 5/5 | 5/5 |
| edit_persona | 5/5 | 5/5 |
| edit_scenario | 5/5 | 5/5 |
| edit_tags | 5/5 | 5/5 |
| edit_word | 5/5 | 5/5 |
| edit_typos | 5/5 | 5/5 |
| edit_three_names | 4/5 (reply 1) | 5/5 |
| edit_long_word | 4/5 (reply 1) | 5/5 |
| edit_long_cut | 5/5 | 5/5 |
| media_image | 5/5 | 5/5 |
| media_speech | 5/5 | 5/5 |
| handoff_regenerate | 5/5 | 5/5 |
| handoff_rewrite | 5/5 | 5/5 |
| handoff_open | 5/5 | 5/5 |
| handoff_continue | 5/5 | 5/5 |

## Failures by tool

**two-default**

- Wrong or unreadable first choice: `list_lorebook_entries` 2, `edit_character` 1, `read_messages` 1
- Unusable replies (retried): none
- Invalid arguments (refused): none
- Tool refusals (target, find text, limits): `edit_character` 2, `list_lorebook_entries` 2, `edit_message` 2, `query_library` 1
- Demotions learned mid-run: structured_ignored:google/gemma-4-31b-it 202

**single-native**

- Wrong or unreadable first choice: none
- Unusable replies (retried): none
- Invalid arguments (refused): none
- Tool refusals (target, find text, limits): none

## Library

Kestrel's description: 7,577 characters. Switches at start: `{"agent": true, "director": true, "director_individual_fragments": false, "output_auditor": false, "length_guard": false, "agentic_lorebook": false, "workflows": true, "workflow:format_consistency": true, "workflow:image_gen": true, "workflow:prose_rewriter": true, "workflow:tts": true, "length_guard_limits": {"enforce": false, "max_words": 240, "max_paragraphs": 4}, "prevent_prompt_overrides": fa`
