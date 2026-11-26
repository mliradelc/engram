import { describe, it, expect } from "vitest";
import {
  applyMoveProgress,
  clearMoveOnStateChange,
  recordMoveCompleted,
} from "../useJobManagement";
import type { MoveProgress, MoveProgressMessage } from "../../../types";

const TOTAL = 39_531_675_648;

function msg(overrides: Partial<Omit<MoveProgressMessage, "type">> = {}): Omit<MoveProgressMessage, "type"> {
  return {
    job_id: 15,
    title_index: 1,
    source_name: "The Matrix_t01.mkv",
    current_bytes: 0,
    total_bytes: TOTAL,
    speed: null,
    eta: null,
    done: false,
    ...overrides,
  };
}

describe("applyMoveProgress", () => {
  it("folds bytes into a percentage keyed by job", () => {
    const next = applyMoveProgress(new Map(), msg({ current_bytes: TOTAL / 4 }), 1000);
    const entry = next.get(15)!;
    expect(entry.percent).toBeCloseTo(25);
    expect(entry.current_bytes).toBe(TOTAL / 4);
    expect(entry.total_bytes).toBe(TOTAL);
    expect(entry.updated_at).toBe(1000);
  });

  it("renders 0% (not NaN) for the zero-byte first sample and a zero total", () => {
    expect(applyMoveProgress(new Map(), msg()).get(15)!.percent).toBe(0);
    expect(applyMoveProgress(new Map(), msg({ total_bytes: 0 })).get(15)!.percent).toBe(0);
  });

  it("coalesces a null speed/eta against what is already shown", () => {
    let moves = applyMoveProgress(new Map(), msg({ current_bytes: 1e9, speed: "42.1 MB/s", eta: 912 }));
    moves = applyMoveProgress(moves, msg({ current_bytes: 2e9, speed: null, eta: null }));
    const entry = moves.get(15)!;
    expect(entry.speed).toBe("42.1 MB/s");
    expect(entry.eta).toBe(912);
    expect(entry.current_bytes).toBe(2e9);
  });

  it("does not carry one title's speed into the next title's first sample", () => {
    let moves = applyMoveProgress(new Map(), msg({ current_bytes: 1e9, speed: "42.1 MB/s", eta: 912 }));
    moves = applyMoveProgress(moves, msg({ title_index: 2, source_name: "x_t02.mkv" }));
    expect(moves.get(15)!.speed).toBeNull();
    expect(moves.get(15)!.eta).toBeNull();
  });

  it("removes the entry on done, and returns the same map when there is nothing to remove", () => {
    const moves = applyMoveProgress(new Map(), msg({ current_bytes: 1e9 }));
    const after = applyMoveProgress(moves, msg({ current_bytes: TOTAL, done: true }));
    expect(after.has(15)).toBe(false);
    expect(after).not.toBe(moves);

    const empty = new Map<number, MoveProgress>();
    expect(applyMoveProgress(empty, msg({ done: true }))).toBe(empty);
  });

  it("leaves other jobs alone", () => {
    let moves = applyMoveProgress(new Map(), msg({ job_id: 3, current_bytes: 5 }));
    moves = applyMoveProgress(moves, msg({ current_bytes: TOTAL, done: true }));
    expect(moves.get(3)!.current_bytes).toBe(5);
  });
});

describe("recordMoveCompleted", () => {
  it("records the verified total on done only", () => {
    const empty = new Map<number, MoveProgress>();
    expect(recordMoveCompleted(empty, msg({ current_bytes: 5 }))).toBe(empty);

    const done = recordMoveCompleted(empty, msg({ current_bytes: TOTAL, done: true }), 2000);
    expect(done.get(15)).toMatchObject({ current_bytes: TOTAL, total_bytes: TOTAL, percent: 100, updated_at: 2000 });
  });
});

describe("clearMoveOnStateChange", () => {
  it("drops an in-flight move once the job leaves organizing", () => {
    const moves = applyMoveProgress(new Map(), msg({ current_bytes: 5 }));
    expect(clearMoveOnStateChange(moves, 15, "organizing")).toBe(moves);
    expect(clearMoveOnStateChange(moves, 15, undefined)).toBe(moves);
    expect(clearMoveOnStateChange(moves, 15, "failed").has(15)).toBe(false);
    expect(clearMoveOnStateChange(moves, 15, "review_needed").has(15)).toBe(false);
  });
});
