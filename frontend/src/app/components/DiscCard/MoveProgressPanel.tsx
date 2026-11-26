import { CheckCircle2, Folder } from "lucide-react";
import { sv, SvProgressBar } from "../synapse";
import { moveReadout, verifiedReadout } from "./moveReadout";
import type { MoveProgress } from "../../../types";

/**
 * The organize move's progress: a green VERIFIED MOVE bar with a GB readout
 * while bytes are in flight, then the verified line once the backend has
 * checked the destination's size against the source's. Renders nothing when
 * neither is known (a same-device rename is instant and reports nothing).
 */
export function MoveProgressPanel({
  move,
  completed,
}: {
  move?: MoveProgress | null;
  completed?: MoveProgress | null;
}) {
  if (move) {
    return (
      <div data-testid="move-progress" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            fontFamily: sv.mono,
            fontSize: 12,
            color: sv.green,
            letterSpacing: "0.2em",
            textTransform: "uppercase",
          }}
        >
          <Folder size={14} />
          <span>› MOVING {move.source_name}…</span>
        </div>
        <SvProgressBar progress={move.percent} color="green" label="VERIFIED MOVE" />
        <span
          data-testid="move-progress-readout"
          className="sv-tnum"
          style={{ fontFamily: sv.mono, fontSize: 12, color: sv.inkDim }}
        >
          {moveReadout(move)}
        </span>
      </div>
    );
  }
  if (completed) {
    return (
      <div
        data-testid="move-verified"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          fontFamily: sv.mono,
          fontSize: 12,
          color: sv.green,
        }}
      >
        <CheckCircle2 size={14} />
        <span>{verifiedReadout(completed)}</span>
      </div>
    );
  }
  return null;
}
