import { afterEach, describe, expect, it, vi } from "vitest";
import {
  batchSteps,
  batchTime,
  controllerReach,
  deviceDosing,
  doseDeadline,
  doseProgress,
  dosingConfigId,
  dosingError,
  dosingStatusId,
  hardwareLabel,
  newPumpId,
  parseConfig,
  pumpView,
  readConfig,
  readStatus,
  recipeRows,
  recipeTotals,
  requestStage,
  resultTone,
  setupChanges,
  setupDraft,
  setupErrors,
  setupPayload,
  stockFor,
  type DosingDocument,
  type DosingRequestResult,
  type DosingStatus,
  type PumpRuntime,
  type SetupDraft,
} from "./dosing";
import { DosingDemo } from "./dosing-demo";
import { createDemo } from "./demo";
import type { EntityState, States } from "./types";

const NOW = new Date(2026, 8, 28, 16, 0, 0).getTime();
afterEach(() => {
  vi.useRealTimers();
});
const entity = (
  id: string,
  state: string,
  attributes: Record<string, unknown> = {},
): EntityState => ({
  entity_id: id,
  state,
  attributes,
  last_updated: new Date(NOW).toISOString(),
  last_changed: new Date(NOW).toISOString(),
});
const demo = (prefix = "") => {
  const states = createDemo(NOW);
  return {
    states,
    config: readConfig(states[dosingConfigId(prefix)])!,
    status: readStatus(states[dosingStatusId(prefix)])!,
  };
};
const runtime = (change: Partial<PumpRuntime> = {}): PumpRuntime => ({
  state: "idle",
  flow_ml_s: 10,
  target_ml: null,
  started_at: null,
  expected_s: null,
  last: null,
  ...change,
});

describe("reading the configuration and the controller's status", () => {
  it("reads each demo room's pumps, recipe and batch hardware from the integration's sensor", () => {
    const f2 = demo().config;
    expect(f2.revision).toBe(4);
    expect(f2.pumps.map((pump) => pump.name)).toEqual(["Balance", "Bloom", "Core", "Cleanse"]);
    expect(f2.batch.recipe.map((line) => line.ml)).toEqual([300, 1800, 1080, 200]);
    expect(f2.batch).toMatchObject({
      fill_valve: "switch.demo_tank_fill_valve",
      full_entity: "binary_sensor.demo_tank_full",
      mix_valves: ["switch.demo_recirc_valve"],
      premix_min: 2,
      postmix_min: 5,
    });
    const f1 = demo("f1_").config;
    expect(f1.pumps.map((pump) => pump.name)).toEqual([
      "Grow",
      "Cleanse",
      "Balance",
      "Fade",
      "Core",
      "Bloom",
    ]);
    expect(f1.batch.recipe.map((line) => line.ml)).toEqual([1080, 200, 180, 0, 0, 1800]);
    expect(f1.batch.fill_valve).toBeNull();
  });

  it("keeps what it can of garbled attributes and never throws", () => {
    const config = readConfig(
      entity("sensor.crop_steering_dosing_config", "7", {
        pumps: [
          { id: "ok", name: "  Bloom ", flow_entity: "number.flow", max_ml: "900" },
          { id: "ok", name: "Duplicate" },
          { id: "Bad Id!", name: "Refused" },
          "not a pump",
          { id: "bare" },
        ],
        batch: { mix_valves: ["switch.a", 3, "not an id"], recipe: [{ pump: "ok", ml: -5 }, {}] },
        request: { id: "", action: "dose" },
      }),
    )!;
    expect(config.revision).toBe(7);
    expect(config.pumps.map((pump) => [pump.id, pump.name, pump.max_ml])).toEqual([
      ["ok", "Bloom", 900],
      ["bare", "bare", 0],
    ]);
    expect(config.pumps[1]).toMatchObject({ dosing_prefix: "Dosing", restore_volume: true });
    expect(config.batch.mix_valves).toEqual(["switch.a"]);
    expect(config.batch.recipe).toEqual([{ pump: "ok", ml: 0, ml_entity: null }]);
    expect(config.batch).toMatchObject({
      full_state: "on",
      fill_timeout_min: 20,
      fill_valve: null,
    });
    expect(config.request).toBeNull();
    expect(readConfig(entity("sensor.crop_steering_dosing_config", "unavailable"))).toBeNull();
    expect(readConfig(undefined)).toBeNull();
    expect(parseConfig("nothing")).toBeNull();
    expect(parseConfig({})!.pumps).toEqual([]);
  });

  it("reads the controller's report, dropping what it cannot use", () => {
    const status = readStatus(
      entity("sensor.crop_steering_dosing", "weird", {
        pumps: { a: { state: "dosing", target_ml: "25", last: { ml: 10 } }, b: "x" },
        batch: { step: "fill", steps: [{ step: "fill", state: "running" }, { step: "nap" }] },
        history: [{ kind: "dose", doses: { a: 25, b: "x" } }, { kind: "party" }],
      }),
    )!;
    expect(status.state).toBe("unavailable");
    expect(status.pumps.a).toMatchObject({
      state: "dosing",
      target_ml: 25,
      last: { ml: 10, result: "" },
    });
    expect(status.pumps.b.state).toBe("unavailable");
    expect(status.batch.step).toBe("fill");
    expect(status.batch.steps).toEqual([{ step: "fill", state: "running", at: null, note: null }]);
    expect(status.history).toEqual([
      { kind: "dose", at: null, ended_at: null, result: "", doses: { a: 25 }, by: null },
    ]);
    expect(readStatus(undefined)).toBeNull();
  });
});

describe("a pump's card", () => {
  it("is idle, dosing, not calibrated or unavailable, and a running motor always shows", () => {
    const { states, config, status } = demo("f1_");
    const reach = controllerReach(states, "f1_", status, NOW);
    expect(reach.live).toBe(true);
    const view = (id: string, s: States = states, st: DosingStatus | null = status) =>
      pumpView(
        config.pumps.find((pump) => pump.id === id)!,
        st,
        controllerReach(s, "f1_", st, NOW),
        s,
        NOW,
      );
    expect(view("grow").state).toBe("idle");
    expect(view("fade")).toMatchObject({ state: "uncalibrated", flow: 0 });
    expect(view("fade").reason).toMatch(/reads 0 mL\/s/);
    // The pump reports dosing though the controller says nothing of it.
    const pressed = {
      ...states,
      "binary_sensor.demo_f1_doser_grow_dosing": entity(
        "binary_sensor.demo_f1_doser_grow_dosing",
        "on",
      ),
    };
    expect(view("grow", pressed).state).toBe("dosing");
    const offline = {
      ...states,
      "number.demo_f1_doser_core_flow": entity("number.demo_f1_doser_core_flow", "unavailable"),
    };
    expect(view("core", offline)).toMatchObject({ state: "unavailable" });
    expect(view("core", offline).reason).toMatch(/number\.demo_f1_doser_core_flow is unavailable/);
    // No controller report: nothing can dose, so nothing reads idle.
    const silent = { ...states };
    delete silent["sensor.crop_steering_f1_ai_heartbeat"];
    expect(view("grow", silent).state).toBe("unavailable");
    expect(view("grow", states, null).reason).toMatch(/update the controller app/);
  });

  it("reads a sensor dosing entity by the state it starts with", () => {
    const pump = {
      ...demo().config.pumps[0],
      dosing_entity: "sensor.doser",
      dosing_prefix: "Dosing",
    };
    expect(deviceDosing(pump, { "sensor.doser": entity("sensor.doser", "Dosing 12 mL") })).toBe(
      true,
    );
    expect(deviceDosing(pump, { "sensor.doser": entity("sensor.doser", "Idle") })).toBe(false);
    expect(deviceDosing(pump, { "sensor.doser": entity("sensor.doser", "unknown") })).toBeNull();
  });

  it("shows a dose's progress as elapsed against expected, with the mL so far", () => {
    const started = new Date(NOW - 10_000).toISOString();
    const running = runtime({
      state: "dosing",
      target_ml: 250,
      expected_s: 25,
      started_at: started,
    });
    expect(doseProgress(running, NOW)).toEqual({
      target: 250,
      elapsed: 10,
      expected: 25,
      share: 0.4,
      ml: 100,
      overdue: false,
    });
    // Past its expected time: full, and overdue; the controller cuts it at 1.25 × + 20 s.
    expect(doseProgress(running, NOW + 20_000)).toMatchObject({ share: 1, ml: 250, overdue: true });
    expect(doseDeadline(25)).toBe(51.25);
    // Expected from the flow when the controller leaves it out; nothing without a target.
    expect(doseProgress({ ...running, expected_s: null }, NOW)!.expected).toBe(25);
    expect(doseProgress({ ...running, target_ml: null }, NOW)).toBeNull();
    expect(doseProgress(runtime(), NOW)).toBeNull();
  });

  it("names the pump's device, and its stock tank's level by the same name", () => {
    const pump = demo().config.pumps[0];
    expect(hardwareLabel(pump)).toBe("demo_doser_balance");
    expect(hardwareLabel({ ...pump, volume_entity: "number.other" })).toBe(
      "button.demo_doser_balance_start",
    );
    const states = {
      "sensor.crop_steering_stock_low": entity("sensor.crop_steering_stock_low", "0", {
        tanks: [{ name: "balance ", level_l: 12.5, percent: 62.5, low: false }],
      }),
    };
    expect(stockFor("Balance", states, "")).toEqual({ level_l: 12.5, percent: 62.5, low: false });
    expect(stockFor("Bloom", states, "")).toBeNull();
    expect(stockFor("Balance", {}, "")).toBeNull();
  });
});

describe("the batch", () => {
  it("totals the recipe and its expected time, passing by pumps at 0 mL", () => {
    const { states, config } = demo();
    const rows = recipeRows(config, states);
    expect(recipeTotals(rows).ml).toBe(3380);
    expect(recipeTotals(rows).seconds).toBeCloseTo(
      300 / 11.06 + 1800 / 10.52 + 1080 / 10.88 + 200 / 9.74,
    );
    const time = batchTime(config, rows);
    expect(time.seconds).toBeCloseTo(recipeTotals(rows).seconds! + 7 * 60);
    expect(time.fillMax).toBe(20 * 60);
    const f1 = demo("f1_");
    const f1Rows = recipeRows(f1.config, f1.states);
    expect(f1Rows.filter((row) => row.skipped).map((row) => row.name)).toEqual(["Fade", "Core"]);
    expect(recipeTotals(f1Rows)).toMatchObject({ pumps: 4, ml: 3260 });
    expect(batchTime(f1.config, f1Rows).fillMax).toBe(0);
  });

  it("reads an amount entity at batch start, over the fixed amount", () => {
    const { states, config } = demo();
    config.batch.recipe[0].ml_entity = "number.bloom_today";
    const rows = recipeRows(config, {
      ...states,
      "number.bloom_today": entity("number.bloom_today", "320"),
    });
    expect(rows[0]).toMatchObject({ ml: 320, fromEntity: true, fixed: 300 });
    expect(recipeRows(config, states)[0]).toMatchObject({ ml: 300, fromEntity: false });
  });

  it("lists the steps to come, passing by what the room does not have", () => {
    const { states, config } = demo("f1_");
    const steps = batchSteps(config, readStatus(states[dosingStatusId("f1_")]), states);
    expect(steps.map((step) => [step.step, step.state])).toEqual([
      ["hold", "waiting"],
      ["close", "waiting"],
      ["fill", "skipped"],
      ["mix", "waiting"],
      ["premix", "waiting"],
      ["dose", "waiting"],
      ["postmix", "waiting"],
      ["finish", "waiting"],
    ]);
    expect(steps[2].detail).toMatch(/no fill valve/);
    expect(steps[5].detail).toBe("4 pumps in order");
  });

  it("follows a running batch as the controller reports it", () => {
    const { states, config, status } = demo();
    status.batch = {
      ...status.batch,
      step: "dose",
      pump: "bloom",
      steps: [
        { step: "hold", state: "done", at: null, note: null },
        { step: "close", state: "done", at: null, note: null },
        { step: "fill", state: "skipped", at: null, note: "the float read full" },
        { step: "mix", state: "done", at: null, note: null },
        { step: "premix", state: "done", at: null, note: null },
        { step: "dose", state: "running", at: null, note: null },
      ],
    };
    // The batch found the float full: now full says nothing of the plan.
    states["binary_sensor.demo_tank_full"] = entity("binary_sensor.demo_tank_full", "on");
    expect(batchSteps(config, null, states)[2]).toMatchObject({
      state: "skipped",
      detail: "Passed by while the float reads full",
    });
    const steps = batchSteps(config, status, states);
    expect(steps[2].detail).toBe("Until the float reads full, 20 min at most");
    expect(steps.map((step) => step.state)).toEqual([
      "done",
      "done",
      "skipped",
      "done",
      "done",
      "running",
      "waiting",
      "waiting",
    ]);
    expect(steps[5].detail).toBe("Dosing Bloom");
    expect(steps[2].note).toBe("the float read full");
  });
});

describe("requests", () => {
  const request = (at: number) => ({
    id: "abc",
    action: "dose" as const,
    pump: "bloom",
    ml: 25,
    at: new Date(at).toISOString(),
    by: "Ben",
  });
  it("wait for the controller, are taken with its word, or expire after two minutes", () => {
    const { config, status } = demo();
    expect(requestStage(config, status, NOW)).toBeNull();
    config.request = request(NOW - 5_000);
    expect(requestStage(config, status, NOW)?.stage).toBe("waiting");
    expect(requestStage(config, status, NOW + 130_000)?.stage).toBe("expired");
    status.handled = "abc";
    status.handled_result = "too old to act on";
    expect(requestStage(config, status, NOW)).toMatchObject({
      stage: "taken",
      result: "too old to act on",
    });
  });
  it("say the integration's refusals in words and colour results", () => {
    expect(dosingError("busy")).toMatch(/still waiting/);
    expect(dosingError("busy", true)).toMatch(/Save once/);
    expect(dosingError("revision")).toMatch(/changed elsewhere/);
    expect(dosingError("Bloom is not a pump")).toBe("Bloom is not a pump");
    expect(
      [
        "finished",
        "stopped: stop requested",
        "ran past its time",
        "not confirmed",
        "too old to act on",
      ].map(resultTone),
    ).toEqual(["on", "warn", "off", "off", "neutral"]);
  });
});

describe("the dosing setup", () => {
  const draft = (): SetupDraft => setupDraft(demo().config);
  it("accepts the demo's setup, and says what the integration would refuse", () => {
    expect(setupErrors(draft())).toEqual([]);
    const bad = draft();
    bad.pumps[1].name = "balance";
    bad.pumps[2].start_entity = "switch.not_a_button";
    bad.pumps[3].max_ml = 9000;
    bad.batch.full_entity = null;
    bad.batch.premix_min = 45;
    bad.batch.mix_valves = ["switch.demo_doser_core_power"];
    bad.batch.recipe.push({ pump: "balance", ml: 5, ml_entity: null });
    expect(setupErrors(bad)).toEqual([
      "Two pumps are called balance.",
      "Core: map its start.",
      "Cleanse: the largest dose must be between 1 and 5000 mL.",
      "A fill valve needs the float that says the tank is full.",
      "Mixing before dosing must be between 0 and 30 minutes.",
      "switch.demo_doser_core_power is a dosing pump's power switch; it cannot also be batch hardware.",
      "Balance is in the recipe twice.",
    ]);
  });
  it("gives a new pump an id from its name, and the recipe follows it", () => {
    const next = draft();
    next.pumps.push({ ...next.pumps[0], key: "new-1", id: null, name: "Bloom!" });
    next.batch.recipe.push({ pump: "new-1", ml: 40, ml_entity: null });
    const payload = setupPayload(next);
    expect(payload.pumps.at(-1)!.id).toBe("bloom_2");
    expect(payload.batch.recipe.at(-1)).toEqual({ pump: "bloom_2", ml: 40, ml_entity: null });
    expect(newPumpId("  ", new Set())).toBe("pump");
    expect(
      newPumpId("A very long nutrient name indeed", new Set(["a_very_long_nutrient_nam"])),
    ).toBe("a_very_long_nutrient_n_2");
  });
  it("lists every change for the review", () => {
    const { config } = demo();
    const next = draft();
    next.pumps[0].max_ml = 1500;
    next.batch.postmix_min = 8;
    next.batch.recipe[1].ml = 1700;
    expect(setupChanges(config, setupPayload(next))).toEqual([
      { label: "Balance · Largest dose (mL)", before: "2000", after: "1500" },
      {
        label: "Recipe",
        before: "Balance 300 mL, Bloom 1800 mL, Core 1080 mL, Cleanse 200 mL",
        after: "Balance 300 mL, Bloom 1700 mL, Core 1080 mL, Cleanse 200 mL",
      },
      { label: "Mix after dosing (min)", before: "5", after: "8" },
    ]);
    expect(setupChanges(config, setupPayload(draft()))).toEqual([]);
  });
});

describe("the demo's dosing services and controller", () => {
  function start(prefix = "") {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    let states = createDemo(NOW);
    const dosing = new DosingDemo(
      () => states,
      (next) => (states = next),
    );
    const room = `room:${prefix}`;
    return {
      get states() {
        return states;
      },
      status: () => readStatus(states[dosingStatusId(prefix)])!,
      config: () => readConfig(states[dosingConfigId(prefix)])!,
      get: () => dosing.call("dosing_get", { room_id: room }) as DosingDocument,
      request: (data: Record<string, unknown>) =>
        dosing.call("dosing_request", { room_id: room, ...data }) as DosingRequestResult,
      save: (data: Record<string, unknown>) =>
        dosing.call("dosing_save", { room_id: room, ...data }) as DosingDocument,
    };
  }
  it("answers dosing_get with the room's configuration and the entities it can use", () => {
    const demo = start();
    const doc = demo.get();
    expect(doc).toMatchObject({ schema_version: 1, room_id: "room:", error: null });
    expect(doc.config!.pumps).toHaveLength(4);
    const domains = new Set(doc.candidates.map((candidate) => candidate.domain));
    for (const domain of ["number", "button", "binary_sensor", "switch", "sensor"])
      expect(domains).toContain(domain);
    expect(domains).not.toContain("select");
  });
  it("doses: the request waits, the controller takes it, the pump runs its mL and finishes", () => {
    const demo = start();
    const sent = demo.request({ action: "dose", pump: "balance", ml: 25 });
    expect(sent.error).toBeNull();
    expect(sent.request).toMatchObject({ action: "dose", pump: "balance", ml: 25, by: "Demo" });
    expect(sent.request!.id).toMatch(/^[0-9a-f]{32}$/);
    expect(demo.config().request!.id).toBe(sent.request!.id);
    // A second one is refused while the first waits.
    expect(demo.request({ action: "batch" })).toEqual({ request: null, error: "busy" });
    vi.advanceTimersByTime(1_000);
    expect(demo.status()).toMatchObject({
      state: "dosing",
      handled: sent.request!.id,
      handled_result: "dosing",
    });
    expect(demo.status().pumps.balance).toMatchObject({ state: "dosing", target_ml: 25 });
    expect(demo.states["binary_sensor.demo_doser_balance_dosing"].state).toBe("on");
    expect(demo.states["number.demo_doser_balance_volume"].state).toBe("25");
    vi.advanceTimersByTime(3_000);
    expect(demo.status().state).toBe("idle");
    expect(demo.status().pumps.balance.last).toMatchObject({ ml: 25, result: "finished" });
    expect(demo.status().history[0]).toMatchObject({
      kind: "dose",
      doses: { balance: 25 },
      result: "finished",
    });
    // The firmware's batch recipe is put back in its volume number.
    expect(demo.states["number.demo_doser_balance_volume"].state).toBe("300");
    expect(demo.states["binary_sensor.demo_doser_balance_dosing"].state).toBe("off");
  });
  it("refuses what the integration refuses", () => {
    const demo = start("f1_");
    expect(demo.request({ action: "dose", pump: "nope", ml: 5 }).error).toMatch(/no pump nope/);
    expect(demo.request({ action: "dose", pump: "grow", ml: 2500 }).error).toMatch(
      /at most 2000 mL/,
    );
    expect(demo.request({ action: "rinse" }).error).toMatch(/dose, batch or stop/);
    // The controller refuses a pump that is not calibrated.
    demo.request({ action: "dose", pump: "fade", ml: 10 });
    vi.advanceTimersByTime(1_000);
    expect(demo.status().handled_result).toBe("refused: Fade is not calibrated");
    expect(demo.status().state).toBe("idle");
  });
  it("makes a batch: fills, mixes, doses the recipe in order and mixes again", () => {
    const demo = start();
    demo.request({ action: "batch" });
    vi.advanceTimersByTime(1_000);
    expect(demo.status().state).toBe("batch");
    vi.advanceTimersByTime(1_500);
    expect(demo.status().batch.step).toBe("fill");
    expect(demo.states["switch.demo_tank_fill_valve"].state).toBe("on");
    vi.advanceTimersByTime(4_500);
    expect(demo.states["binary_sensor.demo_tank_full"].state).toBe("on");
    expect(demo.states["switch.demo_tank_fill_valve"].state).toBe("off");
    expect(demo.states["switch.demo_mix_pump"].state).toBe("on");
    vi.advanceTimersByTime(40_000);
    const status = demo.status();
    expect(status.state).toBe("idle");
    expect(status.batch).toMatchObject({ step: "idle", result: "finished" });
    expect(status.batch.steps.map((step) => [step.step, step.state])).toEqual([
      ["hold", "done"],
      ["close", "done"],
      ["fill", "done"],
      ["mix", "done"],
      ["premix", "done"],
      ["dose", "done"],
      ["postmix", "done"],
      ["finish", "done"],
    ]);
    expect(status.history[0]).toMatchObject({
      kind: "batch",
      result: "finished",
      doses: { balance: 300, bloom: 1800, core: 1080, cleanse: 200 },
    });
    expect(demo.states["switch.demo_mix_pump"].state).toBe("off");
    expect(demo.states["switch.demo_recirc_valve"].state).toBe("off");
  });
  it("stops a batch where it is, and says in which step", () => {
    const demo = start();
    demo.request({ action: "batch" });
    vi.advanceTimersByTime(3_000);
    expect(demo.status().batch.step).toBe("fill");
    // Stop replaces nothing pending here, and is never refused as busy.
    expect(demo.request({ action: "stop" }).error).toBeNull();
    vi.advanceTimersByTime(1_000);
    const status = demo.status();
    expect(status.batch).toMatchObject({
      step: "idle",
      result: "stopped: stop requested during fill the tank",
    });
    expect(status.batch.steps.at(-1)).toMatchObject({
      step: "fill",
      state: "failed",
      note: "stopped",
    });
    expect(demo.states["switch.demo_tank_fill_valve"].state).toBe("off");
    expect(status.history[0]).toMatchObject({ kind: "batch", result: /^stopped/ });
  });
  it("saves a setup against its revision, and refuses a stale one", () => {
    const demo = start();
    const doc = demo.get();
    const next = setupDraft(doc.config);
    next.batch.postmix_min = 8;
    expect(demo.save({ expected_revision: 3, ...setupPayload(next) }).error).toBe("revision");
    const saved = demo.save({ expected_revision: 4, ...setupPayload(next) });
    expect(saved.error).toBeNull();
    expect(saved.config!.revision).toBe(5);
    expect(demo.config()).toMatchObject({ revision: 5, batch: { postmix_min: 8 } });
    next.pumps[0].flow_entity = "number.no_such_flow";
    expect(demo.save({ expected_revision: 5, ...setupPayload(next) }).error).toMatch(
      /number\.no_such_flow does not exist/,
    );
  });
});
