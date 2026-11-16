import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { EncodeProgressPanel, formatEta } from './EncodeProgressPanel';
import { applyEncodeProgress, encodesFromSnapshot } from '../hooks/useJobManagement';
import type { EncodeProgress } from '../../types';

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
    it('seeds the map from GET /api/encoding rows', () => {
        const m = encodesFromSnapshot([row(), row({ file: 'done.mkv', percent: 100 })]);
        expect([...m.keys()]).toEqual(['Show - S01E01.mkv']);
    });
});
