import assert from "node:assert/strict";
import test from "node:test";

import { filterModelChoices, mergeModelChoices } from "../../frontend/model_catalog.js";

test("merges saved configs with discovered models without duplicate choices", () => {
  assert.deepEqual(
    mergeModelChoices(
      [
        { id: 4, model_name: "saved/model" },
        { id: 5, model_name: "saved/model" },
      ],
      ["remote/model", "saved/model", "remote/model", null],
    ),
    [
      { value: "saved/model", id: 4, type: "model" },
      { value: "remote/model", type: "available" },
    ],
  );
});

test("model search is case-insensitive and matches substrings", () => {
  const choices = mergeModelChoices([], ["openai/gpt-5", "google/Gemma-3", "deepseek-chat"]);
  assert.deepEqual(
    filterModelChoices(choices, "GEMMA").map((item) => item.value),
    ["google/Gemma-3"],
  );
  assert.deepEqual(
    filterModelChoices(choices, "chat").map((item) => item.value),
    ["deepseek-chat"],
  );
});

test("model search ignores separators in names and queries", () => {
  const choices = mergeModelChoices([], [
    "google/gemma-4-31b-it",
    "google/gemma_4.2_flash",
    "google/gemma-3-27b-it",
    "openai/gpt-5-mini",
  ]);

  assert.deepEqual(
    filterModelChoices(choices, "gemma 4").map((item) => item.value),
    ["google/gemma-4-31b-it", "google/gemma_4.2_flash"],
  );
  assert.deepEqual(
    filterModelChoices(choices, "gpt 5/mini").map((item) => item.value),
    ["openai/gpt-5-mini"],
  );
});

test("model search matches every term anywhere in the name, regardless of order", () => {
  const choices = mergeModelChoices([{ id: 4, model_name: "google/gemma-4-31b-it" }], [
    "google/gemma-3-27b-it",
    "other/gemma-4-31b-it",
    "openai/gpt-5-mini",
  ]);

  for (const query of ["31b gemma google", "31B\tGOOGLE  gem", "31b/gemma-google"]) {
    assert.deepEqual(
      filterModelChoices(choices, query).map((item) => item.value),
      ["google/gemma-4-31b-it"],
      query,
    );
  }
  assert.deepEqual(filterModelChoices(choices, "gemma mini"), []);
  assert.deepEqual(
    filterModelChoices(choices, "31b gemma").map((item) => item.value),
    ["google/gemma-4-31b-it", "other/gemma-4-31b-it"],
  );
});

test("empty model searches keep all choices", () => {
  const choices = mergeModelChoices([], ["google/gemma-4", "openai/gpt-5-mini"]);

  for (const query of ["", " \t\n ", " /-_ ", null]) {
    assert.deepEqual(filterModelChoices(choices, query), choices);
  }
});

test("model search normalizes compatibility characters", () => {
  const choices = mergeModelChoices([], ["google/Gemma-4"]);

  assert.deepEqual(
    filterModelChoices(choices, "４ ＧＥＭＭＡ").map((item) => item.value),
    ["google/Gemma-4"],
  );
});
