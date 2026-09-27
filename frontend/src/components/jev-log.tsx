import { useState } from "react";
import { ArrowRight, Check, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { time as clock } from "@/components/dashboard";
import { Pill } from "@/components/mini-visuals";
import {
  filterJev,
  jevCost,
  jevIds,
  jevResult,
  JEV_TITLES,
  parseJevZone,
  readJev,
  type JevEntry,
  type JevRoom,
  type JevZoneFilter,
} from "@/lib/jev";
import type { Controller } from "@/lib/types";
import "./jev-log.css";

/** Jev's decisions for the selected room, live from the controller's log sensor: the latest few beside
 * the grow day (`compact`), or all of them with filters on the Activity page. Nothing at all for a room
 * without the log (no Jev, or a controller from before it). */
export function JevDecisions({
  controller,
  compact = false,
  onViewAll,
}: {
  controller: Controller;
  compact?: boolean;
  onViewAll?: () => void;
}) {
  const [zone, setZone] = useState<JevZoneFilter>("all");
  const [actionsOnly, setActionsOnly] = useState(false);
  const { room, states } = controller;
  const { log, room: jev } = readJev(states, room.room.prefix);
  if (!log) return null;
  const names = new Map(room.zones.map((item) => [item.id, item.name]));
  const shown = compact ? log.entries.slice(0, 5) : filterJev(log.entries, zone, actionsOnly);
  if (compact)
    return (
      <div className="jev-compact" data-jev-log="compact">
        <div className="jev-compact-head">
          <h3>Jev decisions</h3>
          <span className="jev-usage">{usage(jev, true)}</span>
          {onViewAll && (
            <Button variant="ghost" size="sm" onClick={onViewAll}>
              View all <ArrowRight size={15} />
            </Button>
          )}
        </div>
        <Trouble jev={jev} compact />
        {shown.length ? (
          <JevList entries={shown} names={names} compact />
        ) : (
          <p className="jev-none">
            No decisions yet: Jev is asked when there is something to decide.
          </p>
        )}
      </div>
    );
  const ids = jevIds(room.room.prefix);
  const now = room.zones.flatMap((item) => {
    const sensor = parseJevZone(states[ids.zone(item.id)]);
    return sensor ? [{ zone: item, acting: sensor.acting }] : [];
  });
  return (
    <section
      className="panel jev-log"
      id="jev-decisions"
      tabIndex={-1}
      aria-labelledby="jev-log-title"
      data-jev-log="full"
    >
      <div className="panel-heading">
        <div>
          <h2 id="jev-log-title">Jev decisions</h2>
          <p className="jev-usage">{usage(jev, false)}</p>
        </div>
        <div className="jev-filters">
          <select
            aria-label="Show Jev decisions for"
            value={String(zone)}
            onChange={(event) => {
              const value = event.target.value;
              setZone(value === "all" || value === "room" ? value : Number(value));
            }}
          >
            <option value="all">All zones and the room</option>
            <option value="room">The room only</option>
            {room.zones.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
          <label className="jev-actions-only">
            <input
              type="checkbox"
              checked={actionsOnly}
              onChange={(event) => setActionsOnly(event.target.checked)}
            />
            Actions only
          </label>
        </div>
      </div>
      <Trouble jev={jev} />
      {now.length > 0 && (
        <p className="jev-now" aria-label="What Jev is doing now">
          <span className="jev-now-label">Now</span>
          {now.map(({ zone: item, acting }) => (
            <span key={item.id} className="jev-now-zone">
              <strong>{item.name}</strong>{" "}
              {acting.length
                ? acting.map((judge) => JEV_TITLES[judge] ?? judge).join(", ")
                : "watching"}
            </span>
          ))}
        </p>
      )}
      {shown.length ? (
        <JevList entries={shown} names={names} />
      ) : log.entries.length ? (
        <div className="jev-empty">
          <p>No Jev decision matches this filter.</p>
          <Button
            variant="outline"
            onClick={() => {
              setZone("all");
              setActionsOnly(false);
            }}
          >
            Show every decision
          </Button>
        </div>
      ) : (
        <div className="jev-empty">
          <p>No decisions yet: Jev is asked when there is something to decide.</p>
        </div>
      )}
      <p className="jev-foot">
        {filterNote(log.entries.length, shown.length)} Jev answers; code decides what it may do and
        refuses what it may not.
      </p>
    </section>
  );
}

function filterNote(all: number, shown: number) {
  return shown === all
    ? `The latest ${all} decision${all === 1 ? "" : "s"}.`
    : `${shown} of the latest ${all} decisions.`;
}

/** Today's calls, tokens, errors and what they cost. */
function usage(jev: JevRoom | null, short: boolean): string {
  if (!jev) return short ? "" : "The controller does not report Jev’s usage.";
  const calls =
    jev.calls === null
      ? null
      : `${jev.calls.toLocaleString()} call${jev.calls === 1 ? "" : "s"} today`;
  const cost = jevCost(jev.tokens);
  if (short) return [calls, cost].filter(Boolean).join(" · ");
  return [
    calls,
    jev.tokens === null ? null : `${jev.tokens.toLocaleString()} input tokens`,
    cost,
    jev.errors === null ? null : `${jev.errors} error${jev.errors === 1 ? "" : "s"}`,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** Jev not answering, or a judge failing: said once, above the list. The full log also names the day's
 * last error; beside the grow day only what is still wrong. */
function Trouble({ jev, compact = false }: { jev: JevRoom | null; compact?: boolean }) {
  if (!jev) return null;
  const failing = Object.keys(jev.judgeErrors);
  const down = jev.state === "error";
  if (!down && !failing.length && (compact || !(jev.errors && jev.lastError))) return null;
  const said = (text: string) => (/[.!?)]$/.test(text) ? text : `${text}.`);
  const why = jev.lastError ? `: ${said(jev.lastError)}` : ".";
  const judges = failing
    .map((judge) => `${JEV_TITLES[judge] ?? judge} (${jev.judgeErrors[judge]})`)
    .join("; ");
  return (
    <p className="jev-trouble" role="note">
      {down
        ? `Jev is not answering${why} The controller carries on without it.`
        : failing.length
          ? `A judge failed: ${said(judges)} The others carry on.`
          : `Last error today${why}`}
    </p>
  );
}

function Agreed({ agreed }: { agreed: boolean }) {
  return (
    <span
      className="jev-agreed"
      data-agreed={agreed}
      title={agreed ? "Both phrasings of the question agreed" : "The two phrasings disagreed"}
    >
      {agreed ? <Check size={13} aria-hidden="true" /> : <X size={13} aria-hidden="true" />}
      <span className="sr-only">
        {agreed ? "both phrasings agreed" : "the two phrasings disagreed"}
      </span>
    </span>
  );
}

/** The clock time, and the date under it for an earlier day (the full log only: beside the grow day\n * a row stays one line). */
function when(entry: JevEntry, compact: boolean) {
  if (entry.time === null) return { text: "Time not recorded", date: null };
  const at = new Date(entry.time);
  const today = new Date();
  const sameDay =
    at.getFullYear() === today.getFullYear() &&
    at.getMonth() === today.getMonth() &&
    at.getDate() === today.getDate();
  return {
    text: clock(entry.time),
    date: sameDay || compact ? null : at.toLocaleDateString([], { month: "short", day: "numeric" }),
  };
}

function JevList({
  entries,
  names,
  compact = false,
}: {
  entries: JevEntry[];
  names: Map<number, string>;
  compact?: boolean;
}) {
  return (
    <ol className="jev-list" data-compact={compact || undefined}>
      {entries.map((entry) => {
        const result = jevResult(entry);
        const at = when(entry, compact);
        const zone = entry.zone === null ? "Room" : (names.get(entry.zone) ?? `Zone ${entry.zone}`);
        return (
          <li
            key={entry.key}
            className="jev-row"
            data-result={entry.result}
            title={compact && entry.reason ? `${result.label}: ${entry.reason}` : undefined}
          >
            <time
              className="jev-time"
              dateTime={entry.time === null ? undefined : new Date(entry.time).toISOString()}
            >
              {at.text}
              {at.date && <span className="jev-date">{at.date}</span>}
            </time>
            <span className="jev-zone">{zone}</span>
            <span className="jev-judge">{entry.title}</span>
            <span className="jev-answer">
              {entry.kind === "outcome" ? (
                <span className="jev-verdict">After “{entry.verdict || "an earlier call"}”</span>
              ) : (
                <span className="jev-verdict">{entry.verdict || "no answer"}</span>
              )}
              {entry.p !== null && (
                <span className="jev-p" title="How sure Jev was">
                  {Math.round(entry.p * 100)}%
                </span>
              )}
              {entry.agreed !== null && <Agreed agreed={entry.agreed} />}
            </span>
            <span className="jev-action">
              {entry.action ? (
                <>
                  <span aria-hidden="true">→ </span>
                  <span className="sr-only">asked to </span>
                  {entry.action}
                </>
              ) : entry.kind === "outcome" ? (
                entry.reason
              ) : (
                <span className="jev-quiet">asked for nothing</span>
              )}
            </span>
            <span className="jev-result">
              <Pill tone={result.tone} data-jev-result={entry.result}>
                {result.label}
              </Pill>
            </span>
            {!compact && entry.reason && entry.kind === "decision" && (
              <span className="jev-reason">{entry.reason}</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}
