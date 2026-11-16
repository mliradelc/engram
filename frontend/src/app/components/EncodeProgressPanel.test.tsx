import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { EncodeProgressPanel, formatEta } from './EncodeProgressPanel';
import {
    applyEncodeProgress,
    applyRetryOptimistic,
    encodesFromSnapshot,
    failuresFromSnapshot,
} from '../hooks/useJobManagement';
import type { EncodeFailure, EncodeProgress } from '../../types';

const failure = (over: Partial<EncodeFailure> = {}): EncodeFailure => ({
    file: 'Show - S01E02.mkv',
    source: '/library/tv/Show/Season 01/Show - S01E02.mkv',
    destination_folder: '/staging/av1-out',
    reason: 'ffmpeg exited 1: boom',
    failed_at: 1_700_000_000,
    ...over,
});

const row = (over: Partial<EncodeProgress> = {}): EncodeProgress => ({
    file: 'Show - S01E01.mkv',
    destination_folder: '/staging/av1-out',
    percent: 42.5,
    eta: 1830,
    speed: '0.25x',
    codec: 'av1',
    started_at: 1,
    ...over,
});

describe('EncodeProgressPanel', () => {
    it('shows the empty state when nothing is encoding', () => {
        render(<EncodeProgressPanel encodes={new Map()} />);
        expect(screen.getByTestId('encode-progress-empty')).toHaveTextContent('Nothing encoding');
        expect(screen.queryByTestId('encode-progress-row')).not.toBeInTheDocument();
    });

    it('shows name, progress bar with percent, ETA and destination folder', () => {
        render(<EncodeProgressPanel encodes={[row()]} />);
        const r = screen.getByTestId('encode-progress-row');
        expect(r).toHaveTextContent('Show - S01E01.mkv');
        expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '43');
        expect(screen.getByTestId('encode-progress-percent')).toHaveTextContent('42.5%');
        expect(screen.getByTestId('encode-progress-eta')).toHaveTextContent('ETA 30m 30s');
        expect(screen.getByTestId('encode-progress-destination')).toHaveTextContent('/staging/av1-out');
    });

    it('shows a dash when the ETA is unknown', () => {
        render(<EncodeProgressPanel encodes={[row({ eta: null })]} />);
        expect(screen.getByTestId('encode-progress-eta')).toHaveTextContent('ETA —');
    });

    it('lists every file in flight, oldest first', () => {
        render(
            <EncodeProgressPanel
                encodes={[row({ file: 'b.mkv', started_at: 2 }), row({ file: 'a.mkv', started_at: 1 })]}
            />,
        );
        const rows = screen.getAllByTestId('encode-progress-row');
        expect(rows.map(r => r.textContent?.slice(0, 5))).toEqual(['a.mkv', 'b.mkv']);
    });
});

describe('formatEta', () => {
    it.each([
        [null, '—'],
        [-1, '—'],
        [12, '12s'],
        [250, '4m 10s'],
        [3900, '1h 05m'],
    ])('%s -> %s', (input, out) => {
        expect(formatEta(input as number | null)).toBe(out);
    });
});

describe('applyEncodeProgress', () => {
    const msg = { file: 'a.mkv', destination_folder: '/out', percent: 10, eta: 90, speed: '1x', codec: 'av1' };

    it('adds and updates an entry keyed by file', () => {
        let m = applyEncodeProgress(new Map(), msg);
        m = applyEncodeProgress(m, { ...msg, percent: 20, eta: 80 });
        expect(m.size).toBe(1);
        expect(m.get('a.mkv')).toMatchObject({ percent: 20, eta: 80 });
    });

    it('keeps the last known values when a sample reports null', () => {
        let m = applyEncodeProgress(new Map(), msg);
        m = applyEncodeProgress(m, { ...msg, percent: null, eta: null, speed: null });
        expect(m.get('a.mkv')).toMatchObject({ percent: 10, eta: 90, speed: '1x' });
    });

    it('removes the entry at 100% or on the terminal message', () => {
        const m = applyEncodeProgress(new Map(), msg);
        expect(applyEncodeProgress(m, { ...msg, percent: 100 }).size).toBe(0);
        expect(applyEncodeProgress(m, { ...msg, percent: 100, done: true }).size).toBe(0);
    });

    it('returns the same map when removing an unknown file', () => {
        const m = new Map();
        expect(applyEncodeProgress(m, { ...msg, done: true, percent: 100 })).toBe(m);
    });
});

describe('encodesFromSnapshot', () => {
    it('seeds the map from the active rows of GET /api/encoding', () => {
        const m = encodesFromSnapshot({
            active: [row(), row({ file: 'done.mkv', percent: 100 })],
            failed: [failure()],
        });
        expect([...m.keys()]).toEqual(['Show - S01E01.mkv']);
    });

    it('still accepts the legacy bare-array payload', () => {
        const m = encodesFromSnapshot([row()]);
        expect([...m.keys()]).toEqual(['Show - S01E01.mkv']);
    });

    it('tolerates an empty or missing payload', () => {
        expect(encodesFromSnapshot(null).size).toBe(0);
        expect(encodesFromSnapshot({ active: [], failed: [] }).size).toBe(0);
    });
});

describe('failuresFromSnapshot', () => {
    it('returns the failed rows, and none for a legacy array', () => {
        const f = failure();
        expect(failuresFromSnapshot({ active: [], failed: [f] })).toEqual([f]);
        expect(failuresFromSnapshot([row()])).toEqual([]);
        expect(failuresFromSnapshot(undefined)).toEqual([]);
    });
});

describe('applyRetryOptimistic', () => {
    it('moves the failed row to active at 0%', () => {
        const f = failure();
        const other = failure({ file: 'b.mkv', source: '/library/tv/b.mkv' });
        const out = applyRetryOptimistic(new Map(), [f, other], f);
        expect(out.failures).toEqual([other]);
        expect(out.encodes.get(f.file)).toMatchObject({
            file: f.file,
            destination_folder: f.destination_folder,
            percent: 0,
            eta: null,
        });
    });

    it('does not clobber a row already encoding', () => {
        const f = failure();
        const live = new Map([[f.file, row({ file: f.file, percent: 37 })]]);
        const out = applyRetryOptimistic(live, [f], f);
        expect(out.encodes.get(f.file)?.percent).toBe(37);
        expect(out.encodes).not.toBe(live);
    });
});

describe('EncodeProgressPanel card treatment', () => {
    it('renders as a titled card with an idle status when nothing is encoding', () => {
        render(<EncodeProgressPanel encodes={[]} />);
        expect(screen.getByTestId('encode-progress-card')).toBeInTheDocument();
        expect(screen.getByRole('heading', { name: 'Encoding' })).toBeInTheDocument();
        expect(screen.getByTestId('encode-progress-status')).toHaveTextContent('Idle');
        expect(screen.getByTestId('encode-progress-empty')).toBeVisible();
    });

    it('counts active encodes in the status chip', () => {
        render(<EncodeProgressPanel encodes={[row(), row({ file: 'b.mkv' })]} />);
        expect(screen.getByTestId('encode-progress-status')).toHaveTextContent('2 Active');
    });
});

describe('EncodeProgressPanel failed rows', () => {
    it('shows each failure with its reason and a Retry button', () => {
        render(<EncodeProgressPanel encodes={[]} failures={[failure()]} onRetry={vi.fn()} />);
        const r = screen.getByTestId('encode-failed-row');
        expect(r).toHaveTextContent('Show - S01E02.mkv');
        expect(screen.getByTestId('encode-failed-reason')).toHaveTextContent('ffmpeg exited 1');
        expect(screen.getByRole('button', { name: 'Retry encoding Show - S01E02.mkv' })).toBeInTheDocument();
        expect(screen.getByTestId('encode-progress-status')).toHaveTextContent('1 Failed');
        // The empty state stays: nothing is encoding right now.
        expect(screen.getByTestId('encode-progress-empty')).toBeInTheDocument();
    });

    it('calls onRetry with the failure and disables the button while it runs', async () => {
        let resolve!: () => void;
        const onRetry = vi.fn(() => new Promise<void>((r) => { resolve = r; }));
        const f = failure();
        render(<EncodeProgressPanel encodes={[]} failures={[f]} onRetry={onRetry} />);
        const btn = screen.getByRole('button', { name: /Retry encoding/ });
        fireEvent.click(btn);
        expect(onRetry).toHaveBeenCalledWith(f);
        await waitFor(() => expect(btn).toBeDisabled());
        fireEvent.click(btn);
        expect(onRetry).toHaveBeenCalledTimes(1);
        resolve();
        await waitFor(() => expect(btn).not.toBeDisabled());
    });

    it('hides Retry when no handler is wired', () => {
        render(<EncodeProgressPanel encodes={[]} failures={[failure()]} />);
        expect(screen.queryByRole('button', { name: /Retry encoding/ })).not.toBeInTheDocument();
    });
});
