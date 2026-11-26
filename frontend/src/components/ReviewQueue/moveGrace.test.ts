import { describe, it, expect } from 'vitest';
import { isTransportFailure, moveStillWorking, MOVE_GRACE_MS, ReviewHttpError } from './moveGrace';
import { ApiError } from '../../api/client';
import type { MoveProgress } from '../../types';

const active: MoveProgress = {
    job_id: 15,
    title_index: 1,
    source_name: 'a.mkv',
    current_bytes: 1,
    total_bytes: 2,
    percent: 50,
    speed: null,
    eta: null,
    updated_at: 0,
};

describe('isTransportFailure', () => {
    it('treats a dropped connection and gateway statuses as transport failures', () => {
        expect(isTransportFailure(new TypeError('Failed to fetch'))).toBe(true);
        expect(isTransportFailure(new ReviewHttpError(504, 'Gateway Time-out'))).toBe(true);
        expect(isTransportFailure(new ReviewHttpError(502, 'Bad Gateway'))).toBe(true);
        expect(isTransportFailure(new ApiError(504, 'Gateway Time-out', ''))).toBe(true);
    });

    it('never masks a real answer from Engram itself', () => {
        expect(isTransportFailure(new ReviewHttpError(400, 'bad'))).toBe(false);
        expect(isTransportFailure(new ReviewHttpError(500, 'boom'))).toBe(false);
        expect(isTransportFailure(new Error('x'))).toBe(false);
        expect(isTransportFailure(null)).toBe(false);
    });
});

describe('moveStillWorking', () => {
    it('is true while a move is in flight', () => {
        expect(moveStillWorking(active, null, 0)).toBe(true);
    });

    it('is true within 30 s of a verified move, false after', () => {
        expect(moveStillWorking(null, 1000, 1000 + MOVE_GRACE_MS)).toBe(true);
        expect(moveStillWorking(null, 1000, 1000 + MOVE_GRACE_MS + 1)).toBe(false);
    });

    it('is false when no move was ever seen', () => {
        expect(moveStillWorking(null, null, 0)).toBe(false);
    });
});
