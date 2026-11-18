import '@testing-library/jest-dom';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { EncodeProgressPanel } from './EncodeProgressPanel';
import { formatEta } from './encodeClasses';
import { EncodeClassBadge } from './EncodeClassBadge';
import { EncodeClassSelect } from './EncodeClassSelect';
import {
    applyEncodeProgress,
    applyRetryOptimistic,
    encodesFromSnapshot,
    failuresFromSnapshot,
} from '../hooks/useJobManagement';
import type { EncodeFailure, EncodeOverrideStatus, EncodeProgress } from '../../types';

const failure = (over: Partial<EncodeFailure> = {}): EncodeFailure => ({
    file: 'Show - S01E02.mkv',
    source: '/library/tv/Show/Season 01/Show - S01E02.mkv',
    destination_folder: '/staging/encode-out',
    reason: 'ffmpeg exited 1: boom',
    failed_at: 1_700_000_000,
    ...over,
});

const row = (over: Partial<EncodeProgress> = {}): EncodeProgress => ({
    file: 'Show - S01E01.mkv',
    destination_folder: '/staging/encode-out',
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
        expect(screen.getByTestId('encode-progress-destination')).toHaveTextContent('/staging/encode-out');
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

    // A long name used to clamp to one line and read 'The Fifth Rac…'. CSS clamping does
    // not change textContent, so assert the whole name is there and the clamp is applied;
    // never a rendered pixel width (jsdom has no layout).
    const LONG_NAME = 'The Fifth Racecourse of the Midnight Archipelago (1987) - Director\'s Cut Remastered 4K.mkv';
    const LONG_SOURCE = `/library/movies/The Fifth Racecourse (1987)/${LONG_NAME}`;

    const expectTwoLineClamp = (el: HTMLElement) => {
        expect(el.style.display).toBe('-webkit-box');
        expect(el.style.getPropertyValue('-webkit-line-clamp')).toBe('2');
        expect(el.style.getPropertyValue('-webkit-box-orient')).toBe('vertical');
        expect(el.style.overflow).toBe('hidden');
        expect(el.style.whiteSpace).not.toBe('nowrap');
    };

    it('keeps a long filename whole, clamped to two lines, full path on hover', () => {
        expect(LONG_NAME.length).toBeGreaterThanOrEqual(80);
        render(
            <EncodeProgressPanel
                encodes={[]}
                failures={[failure({ file: LONG_NAME, source: LONG_SOURCE })]}
                onRetry={vi.fn()}
            />,
        );
        const name = screen.getByTestId('encode-failed-name');
        expect(name.textContent).toBe(LONG_NAME);
        expect(name).toHaveAttribute('title', LONG_SOURCE);
        expectTwoLineClamp(name);
        // Controls sit top-aligned when the name wraps.
        expect(screen.getByTestId('encode-failed-row').style.alignItems).toBe('flex-start');
    });

    it('gives the active-encode name the same two-line clamp', () => {
        render(<EncodeProgressPanel encodes={[row({ file: LONG_NAME })]} />);
        const name = screen.getByTestId('encode-progress-name');
        expect(name.textContent).toBe(LONG_NAME);
        expectTwoLineClamp(name);
    });
});

describe('EncodeClassSelect', () => {
    it('offers Auto plus exactly the four forceable classes, never hdr or interlaced', () => {
        render(<EncodeClassSelect value={undefined} onChange={vi.fn()} label="Codec class" />);
        const select = screen.getByTestId('encode-class-select') as HTMLSelectElement;
        const options = Array.from(select.options).map((o) => [o.value, o.textContent]);
        expect(options).toEqual([
            ['', 'Auto (classifier)'],
            ['passthrough', 'passthrough'],
            ['animation', 'animation'],
            ['live_action_grainy', 'live_action_grainy'],
            ['live_action_clean', 'live_action_clean'],
        ]);
        expect(select.value).toBe('');
        expect(select).not.toHaveTextContent(/hdr|interlaced/i);
    });

    it('reports undefined for Auto and the class otherwise', () => {
        const onChange = vi.fn();
        render(<EncodeClassSelect value="animation" onChange={onChange} label="Codec class" />);
        const select = screen.getByTestId('encode-class-select');
        fireEvent.change(select, { target: { value: 'live_action_grainy' } });
        expect(onChange).toHaveBeenLastCalledWith('live_action_grainy');
        fireEvent.change(select, { target: { value: '' } });
        expect(onChange).toHaveBeenLastCalledWith(undefined);
    });
});

describe('EncodeClassBadge', () => {
    it('shows the class and a FORCED chip only when forced', () => {
        const { rerender } = render(<EncodeClassBadge codecClass="animation" forced />);
        expect(screen.getByTestId('encode-class-badge')).toHaveTextContent('animation');
        expect(screen.getByTestId('encode-class-forced')).toHaveTextContent('FORCED');
        rerender(<EncodeClassBadge codecClass="live_action_clean" forced={false} />);
        expect(screen.getByTestId('encode-class-badge')).toHaveTextContent('live_action_clean');
        expect(screen.queryByTestId('encode-class-forced')).not.toBeInTheDocument();
    });

    it('renders a placeholder when the class is unknown', () => {
        render(<EncodeClassBadge codecClass={null} />);
        expect(screen.getByTestId('encode-class-badge')).toHaveTextContent('—');
    });
});

const overrideStatus = (over: Partial<EncodeOverrideStatus> = {}): EncodeOverrideStatus => ({
    file: failure().source,
    in_domain: true,
    done: false,
    applied_class: null,
    forced: false,
    override: null,
    classifier_enabled: true,
    classes: ['passthrough', 'animation', 'live_action_grainy', 'live_action_clean'],
    ...over,
});

describe('EncodeProgressPanel failed row controls', () => {
    it('contains the badge, the selector, Retry and Dismiss', () => {
        render(
            <EncodeProgressPanel
                encodes={[]}
                failures={[failure({ codec_class: 'animation', forced: true })]}
                onRetry={vi.fn()}
                onDismiss={vi.fn()}
            />,
        );
        const r = within(screen.getByTestId('encode-failed-row'));
        expect(r.getByTestId('encode-class-badge')).toHaveTextContent('animation');
        expect(r.getByTestId('encode-class-forced')).toBeInTheDocument();
        expect(r.getByTestId('encode-class-select')).toBeInTheDocument();
        expect(r.getByTestId('encode-retry')).toHaveTextContent('Retry');
        expect(r.getByTestId('encode-dismiss')).toHaveTextContent('Dismiss');
    });

    it('Retry omits the class for Auto', async () => {
        const onRetry = vi.fn();
        const f = failure();
        render(<EncodeProgressPanel encodes={[]} failures={[f]} onRetry={onRetry} />);
        fireEvent.click(screen.getByTestId('encode-retry'));
        await waitFor(() => expect(onRetry).toHaveBeenCalledTimes(1));
        expect(onRetry.mock.calls[0]).toEqual([f]);
    });

    it('Retry passes the chosen class', async () => {
        const onRetry = vi.fn();
        const f = failure();
        render(<EncodeProgressPanel encodes={[]} failures={[f]} onRetry={onRetry} />);
        fireEvent.change(screen.getByTestId('encode-class-select'), {
            target: { value: 'live_action_grainy' },
        });
        fireEvent.click(screen.getByTestId('encode-retry'));
        await waitFor(() => expect(onRetry).toHaveBeenCalledWith(f, 'live_action_grainy'));
    });

    it('Dismiss calls onDismiss with the failure', () => {
        const onDismiss = vi.fn();
        const f = failure();
        render(<EncodeProgressPanel encodes={[]} failures={[f]} onDismiss={onDismiss} />);
        fireEvent.click(screen.getByTestId('encode-dismiss'));
        expect(onDismiss).toHaveBeenCalledWith(f);
    });

    it('preselects a fresh pin read from GET /encoding/override', async () => {
        const loadOverride = vi.fn(async () =>
            overrideStatus({
                override: { codec_class: 'animation', set_at: 1, size: 3, mtime: 2, stale: false },
            }),
        );
        const onRetry = vi.fn();
        const f = failure();
        render(
            <EncodeProgressPanel encodes={[]} failures={[f]} onRetry={onRetry} loadOverride={loadOverride} />,
        );
        expect(loadOverride).toHaveBeenCalledWith(f.source);
        const select = screen.getByTestId('encode-class-select') as HTMLSelectElement;
        await waitFor(() => expect(select.value).toBe('animation'));
        fireEvent.click(screen.getByTestId('encode-retry'));
        await waitFor(() => expect(onRetry).toHaveBeenCalledWith(f, 'animation'));
    });

    it('ignores a stale pin (stale lives inside the override object)', async () => {
        const loadOverride = vi.fn(async () =>
            overrideStatus({
                override: { codec_class: 'animation', set_at: 1, size: 3, mtime: 2, stale: true },
            }),
        );
        render(
            <EncodeProgressPanel encodes={[]} failures={[failure()]} onRetry={vi.fn()} loadOverride={loadOverride} />,
        );
        await waitFor(() =>
            expect(screen.getByTestId('encode-failed-row')).toHaveTextContent('stale pin ignored'),
        );
        expect((screen.getByTestId('encode-class-select') as HTMLSelectElement).value).toBe('');
    });

    it('locks the selector to Auto when the classifier is off', async () => {
        const loadOverride = vi.fn(async () => overrideStatus({ classifier_enabled: false }));
        render(
            <EncodeProgressPanel encodes={[]} failures={[failure()]} onRetry={vi.fn()} loadOverride={loadOverride} />,
        );
        await waitFor(() => expect(screen.getByTestId('encode-class-select')).toBeDisabled());
    });

    it('a manual choice is not overwritten by a late pin load', async () => {
        let resolve!: (s: EncodeOverrideStatus) => void;
        const loadOverride = vi.fn(() => new Promise<EncodeOverrideStatus>((r) => { resolve = r; }));
        render(
            <EncodeProgressPanel encodes={[]} failures={[failure()]} onRetry={vi.fn()} loadOverride={loadOverride} />,
        );
        const select = screen.getByTestId('encode-class-select') as HTMLSelectElement;
        fireEvent.change(select, { target: { value: 'passthrough' } });
        await act(async () => {
            resolve(overrideStatus({
                override: { codec_class: 'animation', set_at: 1, size: 3, mtime: 2, stale: false },
            }));
        });
        expect(select.value).toBe('passthrough');
    });

    it('shows the class badge on an active row', () => {
        render(<EncodeProgressPanel encodes={[row({ codec_class: 'live_action_grainy', forced: true })]} />);
        const r = within(screen.getByTestId('encode-progress-row'));
        expect(r.getByTestId('encode-class-badge')).toHaveTextContent('live_action_grainy');
        expect(r.getByTestId('encode-class-forced')).toBeInTheDocument();
    });
});

describe('applyEncodeProgress keeps the class', () => {
    it('preserves codec_class and forced across WebSocket samples, which do not carry them', () => {
        const seeded = encodesFromSnapshot({
            active: [row({ file: 'a.mkv', codec_class: 'animation', forced: true, percent: 5 })],
            failed: [],
        });
        const msg = { file: 'a.mkv', destination_folder: '/out', percent: 10, eta: 90, speed: '1x', codec: 'av1' };
        let m = applyEncodeProgress(seeded, msg);
        m = applyEncodeProgress(m, { ...msg, percent: 20, eta: null });
        expect(m.get('a.mkv')).toMatchObject({ percent: 20, codec_class: 'animation', forced: true });
    });

    it('defaults to an unknown, unforced class for a file first seen over WS', () => {
        const m = applyEncodeProgress(new Map(), {
            file: 'b.mkv', destination_folder: '/out', percent: 1, eta: null, speed: null, codec: 'av1',
        });
        expect(m.get('b.mkv')).toMatchObject({ codec_class: null, forced: false });
    });

    it('applyRetryOptimistic records the forced class, or none for Auto', () => {
        const f = failure();
        expect(applyRetryOptimistic(new Map(), [f], f, 'animation').encodes.get(f.file))
            .toMatchObject({ codec_class: 'animation', forced: true });
        expect(applyRetryOptimistic(new Map(), [f], f).encodes.get(f.file))
            .toMatchObject({ codec_class: null, forced: false });
    });
});
