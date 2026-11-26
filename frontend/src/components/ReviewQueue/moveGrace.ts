/**
 * A review save blocks for the whole organize move, which for a Blu-ray going
 * onto a NAS share can run for tens of minutes. A reverse proxy in front of
 * Engram may give up first (504 Gateway Time-out) while the backend carries on
 * and verifies the copy byte for byte before deleting the staging file. So a
 * failed request is NOT a failed save when move progress for the job is still
 * arriving, or arrived very recently: say so instead of showing an error.
 */
import type { MoveProgress } from '../../types';

/** How long after a verified move a failed request is still assumed to be it. */
export const MOVE_GRACE_MS = 30_000;

export const STILL_WORKING_MESSAGE = 'Still working — check the progress bar';

/** A non-2xx review response, carrying its status so the gateway case can be told apart. */
export class ReviewHttpError extends Error {
    readonly status: number;
    constructor(status: number, message: string) {
        super(message);
        this.name = 'ReviewHttpError';
        this.status = status;
    }
}

/** Statuses a proxy or gateway produces while the backend is still working. */
const GATEWAY_STATUSES = new Set([502, 503, 504]);

/**
 * True when the request died in transport (fetch's TypeError: the connection
 * dropped) or a gateway answered for the backend. A real 4xx/500 from Engram
 * itself is a real failure and is never masked.
 */
export function isTransportFailure(err: unknown): boolean {
    if (err instanceof TypeError) return true;
    const status = (err as { status?: unknown } | null)?.status;
    return typeof status === 'number' && GATEWAY_STATUSES.has(status);
}

/** True while a move is in flight, or within the grace window after it was verified. */
export function moveStillWorking(
    active: MoveProgress | null,
    completedAt: number | null,
    now: number = Date.now(),
): boolean {
    if (active) return true;
    return completedAt !== null && now - completedAt <= MOVE_GRACE_MS;
}
