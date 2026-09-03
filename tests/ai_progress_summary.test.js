const test = require("node:test");
const assert = require("node:assert/strict");

const {summarize} = require("../web/ai_progress_summary.js");

test("AI progress summary exposes one shared completed repair and review contract", () => {
  const result = summarize({
    phase: "completed",
    units: {
      planned: 30,
      completed: 30,
      repair_planned: 4,
      repair_completed: 4,
    },
    problem_count: 2,
  });

  assert.deepEqual(
    result.items.map((item) => `${item.label} ${item.value}`),
    ["完成 30/30", "修复 4/4", "需人工审核 2"],
  );
  assert.equal(result.items[2].tone, "warning");
  assert.equal(
    require("../web/ai_progress_summary.js").format({
      phase:"completed",
      unit_label:"块",
      units:{planned:30, completed:30, repair_planned:4, repair_completed:4, repair_accepted:4},
      problem_count:2,
    }),
    "完成 30/30 块 · 修复 4/4 块 · 需人工审核 2 条",
  );
});

test("problem cue ids are a fallback when old progress has no problem count", () => {
  const result = summarize(
    {phase:"completed", units:{planned:3, completed:3}},
    {problemCueIds:["cue-1", "cue-2"]},
  );
  assert.equal(result.problemCount, 2);
  assert.equal(result.items[1].value, "0");
});

test("missing AI progress does not invent task counts", () => {
  assert.equal(summarize(null), null);
});

test("quiet tasks only show the active model-processing field", () => {
  assert.equal(
    require("../web/ai_progress_summary.js").format({
      phase:"executing", unit_label:"块", units:{planned:53, completed:17}, problem_count:0,
    }),
    "模型处理 17/53 块",
  );
});

test("segmentation translation and calibration use canonical task units", () => {
  const {format} = require("../web/ai_progress_summary.js");
  const units = {planned:8, completed:8, repair_planned:2, repair_completed:2, repair_accepted:1};
  assert.equal(
    format({kind:"segmentation", phase:"completed", unit_label:"旧字段", units, problem_count:3}),
    "完成 8/8 块 · 修复 1/2 块 · 需人工审核 3 条",
  );
  assert.equal(
    format({kind:"translation", phase:"completed", unit_label:"块", units, problem_count:3}),
    "完成 8/8 个意义组 · 修复 1/2 个意义组 · 需人工审核 3 条",
  );
  assert.equal(
    format({kind:"calibration", phase:"completed", unit_label:"块", units, problem_count:3}),
    "完成 8/8 个校准块 · 修复 1/2 个校准块 · 需人工审核 3 条",
  );
});
