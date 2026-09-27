import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { JevDecisions } from "@/components/jev-log";
import { createDemo } from "./demo";
import {
  filterJev,
  jevAnswer,
  jevCost,
  jevResult,
  parseJevEntry,
  parseJevLog,
  parseJevRoom,
  parseJevStage,
  parseJevZone,
  type JevEntry,
} from "./jev";
import { buildRoom, discoverRooms } from "./model";
import type { Controller, EntityState, States } from "./types";

const entity = (
  entity_id: string,
  state: string,
  attributes: Record<string, unknown> = {},
): EntityState => ({ entity_id, state, attributes });
const decision = {
  t: "2026-09-28T10:05:12",
  zone: 1,
  judge: "salt",
  title: "Pore EC",
  kind: "decision",
  verdict: "below band vegetative",
  p: 0.72,
  agreed: true,
  action: "hold the EC steer",
  result: "acted",
  reason: "the EC steer stays inside its own clamp",
};

describe("the Jev log sensor", () => {
  it("reads every field of a published decision, in the controller's naive local time", () => {
    const entry = parseJevEntry(decision)!;
    expect(entry).toMatchObject({
      zone: 1,
      judge: "salt",
      title: "Pore EC",
      kind: "decision",
      verdict: "below band vegetative",
      p: 0.72,
      agreed: true,
      action: "hold the EC steer",
      result: "acted",
      reason: "the EC steer stays inside its own clamp",
    });
    expect(entry.time).toBe(new Date(2026, 8, 28, 10, 5, 12).getTime());
  });
  it("is null without the sensor: a room without Jev, or a controller from before the log", () => {
    expect(parseJevLog(undefined)).toBeNull();
    expect(parseJevLog(entity("sensor.crop_steering_jev_log", "no decisions yet"))).toEqual({
      headline: "no decisions yet",
      entries: [],
    });
  });
  it("keeps what it can of malformed entries and drops what is not an entry at all", () => {
    const log = parseJevLog(
      entity("sensor.crop_steering_jev_log", "unavailable", {
        entries: [
          null,
          "a line of text",
          42,
          [1, 2],
          { judge: "dawn" },
          { ...decision, zone: "2", p: "72", agreed: "false", t: "not a time" },
          { ...decision, zone: null, judge: "alerts", title: "", p: 1.7e9, agreed: "maybe" },
        ],
      }),
    )!;
    expect(log.headline).toBe("");
    expect(log.entries).toHaveLength(3);
    const [alert, bare, odd] = log.entries;
    // Readable times first, newest first; the unreadable time goes last.
    expect(alert).toMatchObject({ zone: null, title: "Alert triage", p: null, agreed: null });
    expect(bare).toMatchObject({
      title: "Morning start",
      verdict: "",
      p: null,
      agreed: null,
      action: "",
      result: "unknown",
      reason: "",
      time: null,
    });
    expect(odd).toMatchObject({ zone: 2, p: 0.72, agreed: false, time: null });
    expect(new Set(log.entries.map((entry) => entry.key)).size).toBe(3);
  });
  it("is not an array: no entries, never an error", () => {
    expect(
      parseJevLog(entity("sensor.crop_steering_jev_log", "x", { entries: { 0: decision } }))!
        .entries,
    ).toEqual([]);
  });
  it('accepts `note` for `reason`, and splits a legacy "refused: why" result', () => {
    const { reason, ...rest } = decision;
    expect(parseJevEntry({ ...rest, note: reason })!.reason).toBe(reason);
    const legacy = parseJevEntry({
      ...rest,
      result: "refused: VWC 58.2 more than 3 points under the ramp ceiling 64.0",
    })!;
    expect(legacy.result).toBe("refused");
    expect(legacy.reason).toBe("VWC 58.2 more than 3 points under the ramp ceiling 64.0");
    // A reason given on its own wins over the one in the result.
    expect(parseJevEntry({ ...decision, result: "refused: old words" })!.reason).toBe(reason);
    expect(parseJevEntry({ ...decision, result: "no_action" })!.result).toBe("no action");
  });
  it("sorts newest first whatever order it was published in", () => {
    const log = parseJevLog(
      entity("sensor.crop_steering_jev_log", "x", {
        entries: [
          { ...decision, t: "2026-09-28T09:00:00" },
          { ...decision, t: "2026-09-28T11:30:00" },
          { ...decision, t: "2026-09-28T10:15:00" },
        ],
      }),
    )!;
    expect(log.entries.map((entry) => new Date(entry.time!).getHours())).toEqual([11, 10, 9]);
  });
  it("tells an outcome from a decision, and a judge's alert (advice) from code acting", () => {
    const outcome = parseJevEntry({
      ...decision,
      kind: "outcome",
      verdict: "slab_full",
      p: null,
      agreed: null,
      action: "",
      result: "did not work",
    })!;
    expect(outcome).toMatchObject({ kind: "outcome", verdict: "slab full" });
    expect(jevResult(outcome)).toEqual({ label: "Didn’t work", tone: "off", acted: false });
    const advice = parseJevEntry({
      ...decision,
      judge: "stage",
      action: "CS-705: this zone is off the stage's arc",
    })!;
    expect(advice.result).toBe("advice");
    expect(jevResult(advice)).toMatchObject({ label: "Advice", acted: false });
    // The alert-triage judge's own call (push or card) is code acting on its answer.
    const triage = parseJevEntry({ ...decision, judge: "alerts", action: "CS-705: card only" })!;
    expect(jevResult(triage)).toMatchObject({ label: "Acted", acted: true });
    expect(jevResult(parseJevEntry({ ...decision, result: "refused" })!)).toMatchObject({
      label: "Refused",
      tone: "warn",
    });
    expect(jevResult(parseJevEntry({ ...decision, result: "sleeping" })!)).toMatchObject({
      label: "Sleeping",
      tone: "neutral",
    });
  });
  it("says Jev's answer with how sure it was", () => {
    expect(jevAnswer(parseJevEntry(decision)!)).toBe("below band vegetative, 72%");
    expect(jevAnswer({ verdict: "", p: null })).toBe("no answer");
  });
});

describe("filtering the log", () => {
  const entries = [
    { ...decision, zone: 1, result: "acted" },
    { ...decision, zone: 2, result: "no action", action: "" },
    { ...decision, zone: null, judge: "alerts", result: "acted" },
    { ...decision, zone: 2, result: "refused" },
    { ...decision, zone: 1, kind: "outcome", result: "worked" },
    { ...decision, zone: 3, result: "waiting" },
  ].map((raw, index) => parseJevEntry(raw, index)!);
  const zones = (list: JevEntry[]) => list.map((entry) => entry.zone);
  it("by zone, or the room only", () => {
    expect(zones(filterJev(entries, "all", false))).toEqual([1, 2, null, 2, 1, 3]);
    expect(zones(filterJev(entries, 2, false))).toEqual([2, 2]);
    expect(zones(filterJev(entries, "room", false))).toEqual([null]);
  });
  it("actions only: what did something or tried to, and how it turned out", () => {
    expect(filterJev(entries, "all", true).map((entry) => entry.result)).toEqual([
      "acted",
      "acted",
      "refused",
      "worked",
    ]);
    expect(filterJev(entries, 3, true)).toEqual([]);
  });
});

describe("the room's Jev sensor", () => {
  const stage = {
    day: 37,
    days: 56,
    name: "flower bulk",
    steering: "vegetative",
    stage_days: [22, 42],
    peak: "at or above field capacity",
    pore_ec: [3.5, 6.0],
    dryback_points: [10, 15],
    runoff_pct: [8, 16],
  };
  it("reads usage and today's stage", () => {
    const room = parseJevRoom(
      entity("sensor.crop_steering_jev", "on", {
        calls_today: 46,
        input_tokens_today: 131_400,
        errors_today: 1,
        last_error: "TimeoutError",
        judges: ["salt", "dawn"],
        judge_errors: { shot: "KeyError: 'rise'", dawn: "" },
        stage,
      }),
    )!;
    expect(room).toMatchObject({
      state: "on",
      calls: 46,
      tokens: 131_400,
      errors: 1,
      lastError: "TimeoutError",
      judges: ["salt", "dawn"],
      judgeErrors: { shot: "KeyError: 'rise'" },
    });
    expect(room.stage).toEqual({
      day: 37,
      days: 56,
      name: "flower bulk",
      steering: "vegetative",
      stageDays: [22, 42],
      peak: "at or above field capacity",
      poreEc: [3.5, 6],
      drybackPoints: [10, 15],
      runoffPct: [8, 16],
    });
  });
  it("an older controller without a stage, or an unknown flower day, has none", () => {
    expect(parseJevRoom(entity("sensor.crop_steering_jev", "on", {}))).toMatchObject({
      calls: null,
      tokens: null,
      stage: null,
      judges: [],
      judgeErrors: {},
    });
    expect(parseJevRoom(entity("sensor.crop_steering_jev", "on", { stage: null }))!.stage).toBe(
      null,
    );
    expect(parseJevRoom(undefined)).toBeNull();
  });
  it("a stage with odd numbers keeps what is sound", () => {
    expect(
      parseJevStage({
        ...stage,
        day: "x",
        pore_ec: [6, 3.5],
        dryback_points: [10],
        runoff_pct: ["8", "16"],
      }),
    ).toMatchObject({ day: null, poreEc: [3.5, 6], drybackPoints: null, runoffPct: [8, 16] });
    expect(parseJevStage({ day: 3 })).toBeNull();
    expect(parseJevStage("flower bulk")).toBeNull();
  });
  it("costs input tokens at $0.042 a million", () => {
    expect(jevCost(null)).toBeNull();
    expect(jevCost(131_400)).toBe("under $0.01");
    expect(jevCost(3_000_000)).toBe("≈ $0.13");
    expect(jevCost(40_000_000)).toBe("≈ $1.7");
  });
});

describe("a zone's Jev sensor", () => {
  it("names the judges acting on it, or none while it watches", () => {
    const acting = parseJevZone(
      entity("sensor.crop_steering_zone_2_jev", "salt, stage", {
        judges: {
          salt: {
            verdicts: { salt_cause: { answer: "below_band_vegetative", p: 0.72, agreed: true } },
            directive: "ec_mode hold",
            why: "the EC steer stays inside its own clamp",
            streak: 2,
          },
          stage: "not an object",
        },
      }),
    )!;
    expect(acting.acting).toEqual(["salt", "stage"]);
    expect(acting.judges).toEqual([
      {
        judge: "salt",
        title: "Pore EC",
        answer: "below band vegetative",
        p: 0.72,
        agreed: true,
        directive: "ec mode hold",
        why: "the EC steer stays inside its own clamp",
      },
    ]);
    expect(parseJevZone(entity("sensor.crop_steering_zone_1_jev", "watching"))).toEqual({
      acting: [],
      judges: [],
    });
    expect(parseJevZone(undefined)).toBeNull();
  });
});

describe("the Jev decisions panel", () => {
  const controller = (states: States, prefix: string) =>
    ({
      states,
      room: buildRoom(
        states,
        discoverRooms(states).find((room) => room.prefix === prefix)!,
      ),
    }) as Controller;
  const now = new Date(2026, 8, 28, 16, 0).getTime();
  it("draws nothing for a room without the log", () => {
    const states = createDemo(now);
    const f1 = controller(states, "f1_");
    expect(renderToStaticMarkup(createElement(JevDecisions, { controller: f1 }))).toBe("");
    expect(
      renderToStaticMarkup(createElement(JevDecisions, { controller: f1, compact: true })),
    ).toBe("");
  });
  it("lists the demo room's decisions: the latest five beside the grow day, all of them on Activity", () => {
    const states = createDemo(now);
    const f2 = controller(states, "");
    const compact = renderToStaticMarkup(
      createElement(JevDecisions, { controller: f2, compact: true, onViewAll: () => {} }),
    );
    expect(compact.match(/class="jev-row"/g)).toHaveLength(5);
    expect(compact).toContain("View all");
    const full = renderToStaticMarkup(createElement(JevDecisions, { controller: f2 }));
    expect(full.match(/class="jev-row"/g)).toHaveLength(
      (states["sensor.crop_steering_jev_log"].attributes.entries as unknown[]).length,
    );
    for (const text of [
      "Actions only",
      "Refused",
      "Advice",
      "Didn’t work",
      "Worked",
      "input tokens",
    ])
      expect(full).toContain(text);
    expect(full).toContain("both phrasings agreed");
  });
});
