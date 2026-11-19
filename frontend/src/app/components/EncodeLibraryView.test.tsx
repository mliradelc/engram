import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { EncodeLibraryView } from './EncodeLibraryView';
import {
    apiErrorDetail,
    bySeason,
    filterLibrary,
    overlayLive,
    savedPercent,
    seasonLabel,
    splitForMiddleTruncation,
} from './encodeLibrary';
import type { EncodeLibrary, EncodeLibraryEntry, EncodeProgress } from '../../types';

const TV = '/media/_staging/tv';
const SHOW = `${TV}/Stargate SG-1 (1997) [tmdbid-4629]`;
const FILM = '/media/_staging/movies/Inception (2010)';

const entry = (over: Partial<EncodeLibraryEntry> = {}): EncodeLibraryEntry => ({
    source: `${SHOW}/Season 03/Stargate SG-1 - S03E09 - Rules of Engagement.mkv`,
    file: 'Stargate SG-1 - S03E09 - Rules of Engagement.mkv',
    folder: `${SHOW}/Season 03`,
    season: 3,
    episode: 9,
    output: '/staging/encode-out/Stargate SG-1 - S03E09 - Rules of Engagement.mkv',
    codec_class: 'live_action_grainy',
    forced: false,
    state: 'encoded',
    note: null,
    encoded_at: 1_794_639_863,
    source_bytes: 1_000_000_000,
    output_bytes: 250_000_000,
    progress: null,
    failure: null,
    ...over,
});

const library = (): EncodeLibrary => ({
    root_tv: TV,
    root_movies: '/media/_staging/movies',
    groups: [
        {
            kind: 'series',
            title: 'Stargate SG-1 (1997) [tmdbid-4629]',
            folder: SHOW,
            entries: [
                entry({
                    source: `${SHOW}/Specials/Stargate SG-1 - S00E01 - Behind the Gate.mkv`,
                    file: 'Stargate SG-1 - S00E01 - Behind the Gate.mkv',
                    folder: `${SHOW}/Specials`,
                    season: 0,
                    episode: 1,
                    output: null,
                    codec_class: 'passthrough',
                    state: 'passthrough',
                    output_bytes: null,
                }),
                entry(),
                entry({
                    source: `${SHOW}/Season 03/Stargate SG-1 - S03E10 - Window of Opportunity.mkv`,
                    file: 'Stargate SG-1 - S03E10 - Window of Opportunity.mkv',
                    episode: 10,
                    state: 'stale',
                    note: 'The source changed after this encode',
                }),
            ],
        },
        {
            kind: 'film',
            title: 'Inception (2010)',
            folder: FILM,
            entries: [
                entry({
                    source: `${FILM}/Inception (2010).mkv`,
                    file: 'Inception (2010).mkv',
                    folder: FILM,
                    season: null,
                    episode: null,
                    output: '/staging/av1-out/Inception (2010).mkv',
                    codec_class: 'live_action_clean',
                    state: 'output_missing',
                    output_bytes: null,
                }),
            ],
        },
    ],
    totals: { groups: 2, entries: 4 },
});

const expandAll = () => {
    for (const t of screen.getAllByTestId('encode-library-group-toggle')) {
        if (t.getAttribute('aria-expanded') === 'false') fireEvent.click(t);
    }
};

const rowFor = (file: string) =>
    screen.getAllByTestId('encode-library-row').find((r) => within(r).queryByText(file) !== null)!;

describe('EncodeLibraryView', () => {
    it('renders one section per series / film with its folder path', () => {
        render(<EncodeLibraryView library={library()} />);
        const groups = screen.getAllByTestId('encode-library-group');
        expect(groups).toHaveLength(2);
        expect(groups[0]).toHaveAttribute('data-kind', 'series');
        expect(groups[1]).toHaveAttribute('data-kind', 'film');
        expect(screen.getAllByTestId('encode-library-group-title').map((t) => t.textContent)).toEqual([
            'Stargate SG-1 (1997) [tmdbid-4629]',
            'Inception (2010)',
        ]);
        const folders = screen.getAllByTestId('encode-library-group-folder');
        expect(folders[0]).toHaveAttribute('title', SHOW);
        // Middle-truncated visually, but the whole path is in the DOM (copyable).
        expect(folders[0].textContent).toBe(SHOW);
        expect(folders[1]).toHaveAttribute('title', FILM);
        expect(screen.getAllByTestId('encode-library-group-count')[0]).toHaveTextContent('3 eps');
    });

    it('collapses groups by default and expands on click, nesting series by season', () => {
        render(<EncodeLibraryView library={library()} />);
        expect(screen.queryByTestId('encode-library-row')).not.toBeInTheDocument();
        const [seriesToggle] = screen.getAllByTestId('encode-library-group-toggle');
        fireEvent.click(seriesToggle);
        expect(seriesToggle).toHaveAttribute('aria-expanded', 'true');
        const seasons = screen.getAllByTestId('encode-library-season');
        expect(seasons.map((s) => s.textContent)).toEqual([
            expect.stringContaining('Specials'),
            expect.stringContaining('Season 03'),
        ]);
        expect(screen.getAllByTestId('encode-library-row')).toHaveLength(3);
    });

    it('shows sizes, % saved, encoded-at and a distinct pill for stale and missing rows', () => {
        render(<EncodeLibraryView library={library()} />);
        expandAll();
        const encoded = rowFor('Stargate SG-1 - S03E09 - Rules of Engagement.mkv');
        expect(within(encoded).getByTestId('encode-library-sizes')).toHaveTextContent('954 MB → 238 MB (−75%)');
        expect(within(encoded).getByTestId('encode-library-encoded-at')).toHaveAttribute(
            'title',
            expect.stringContaining("marker file's modification time"),
        );
        const pills = screen.getAllByTestId('encode-library-state').map((p) => p.getAttribute('data-state'));
        expect(pills).toEqual(['passthrough', 'encoded', 'stale', 'output_missing']);
        const stale = within(rowFor('Stargate SG-1 - S03E10 - Window of Opportunity.mkv')).getByTestId('encode-library-state');
        const missing = within(rowFor('Inception (2010).mkv')).getByTestId('encode-library-state');
        expect(stale.style.borderStyle).toBe('dashed');
        expect(missing.style.borderStyle).toBe('dashed');
        const encodedPill = within(encoded).getByTestId('encode-library-state');
        expect(encodedPill.style.borderStyle).toBe('solid');
        expect(stale.style.color).not.toBe(missing.style.color);
    });

    it('fires onReencode with the chosen class after the replace warning', async () => {
        const onReencode = vi.fn();
        render(<EncodeLibraryView library={library()} onReencode={onReencode} />);
        expandAll();
        const row = rowFor('Stargate SG-1 - S03E09 - Rules of Engagement.mkv');
        fireEvent.change(within(row).getByTestId('encode-class-select'), { target: { value: 'animation' } });
        fireEvent.click(within(row).getByTestId('encode-library-reencode'));
        // Warned, not fired yet.
        expect(onReencode).not.toHaveBeenCalled();
        expect(within(row).getByTestId('encode-library-confirm')).toHaveTextContent('replaces the existing encode');
        fireEvent.click(within(row).getByTestId('encode-library-confirm-yes'));
        await waitFor(() => expect(onReencode).toHaveBeenCalledTimes(1));
        const [sent, cls] = onReencode.mock.calls[0];
        expect(sent.source).toBe(`${SHOW}/Season 03/Stargate SG-1 - S03E09 - Rules of Engagement.mkv`);
        expect(cls).toBe('animation');
    });

    it('Auto calls onReencode without a class; cancel fires nothing', async () => {
        const onReencode = vi.fn();
        render(<EncodeLibraryView library={library()} onReencode={onReencode} />);
        expandAll();
        const row = rowFor('Stargate SG-1 - S03E09 - Rules of Engagement.mkv');
        fireEvent.click(within(row).getByTestId('encode-library-reencode'));
        fireEvent.click(within(row).getByTestId('encode-library-confirm-no'));
        expect(onReencode).not.toHaveBeenCalled();
        fireEvent.click(within(row).getByTestId('encode-library-reencode'));
        fireEvent.click(within(row).getByTestId('encode-library-confirm-yes'));
        await waitFor(() => expect(onReencode).toHaveBeenCalledTimes(1));
        expect(onReencode.mock.calls[0]).toHaveLength(1);
    });

    it('a passthrough row re-encodes without a replace warning', async () => {
        const onReencode = vi.fn();
        render(<EncodeLibraryView library={library()} onReencode={onReencode} />);
        expandAll();
        const row = rowFor('Stargate SG-1 - S00E01 - Behind the Gate.mkv');
        fireEvent.change(within(row).getByTestId('encode-class-select'), { target: { value: 'live_action_clean' } });
        fireEvent.click(within(row).getByTestId('encode-library-reencode'));
        await waitFor(() => expect(onReencode).toHaveBeenCalledTimes(1));
        expect(onReencode.mock.calls[0][1]).toBe('live_action_clean');
    });

    it('offers only Auto plus the four forceable classes', () => {
        render(<EncodeLibraryView library={library()} onReencode={vi.fn()} />);
        expandAll();
        const [select] = screen.getAllByTestId('encode-class-select');
        const values = Array.from(select.querySelectorAll('option')).map((o) => o.getAttribute('value'));
        expect(values).toEqual(['', 'passthrough', 'animation', 'live_action_grainy', 'live_action_clean']);
    });

    it('respects the state filter and collapses groups to match', () => {
        render(<EncodeLibraryView library={library()} />);
        fireEvent.click(screen.getByTestId('encode-library-filter-stale'));
        let groups = screen.getAllByTestId('encode-library-group');
        expect(groups).toHaveLength(1);
        expect(within(groups[0]).getByTestId('encode-library-group-toggle')).toHaveAttribute('aria-expanded', 'true');
        expect(screen.getAllByTestId('encode-library-row').map((r) => r.getAttribute('data-source'))).toEqual([
            `${SHOW}/Season 03/Stargate SG-1 - S03E10 - Window of Opportunity.mkv`,
        ]);
        expect(within(groups[0]).getByTestId('encode-library-group-count')).toHaveTextContent('1/3');

        fireEvent.click(screen.getByTestId('encode-library-filter-output_missing'));
        groups = screen.getAllByTestId('encode-library-group');
        expect(groups).toHaveLength(1);
        expect(groups[0]).toHaveAttribute('data-kind', 'film');

        fireEvent.click(screen.getByTestId('encode-library-filter-failed'));
        expect(screen.queryByTestId('encode-library-group')).not.toBeInTheDocument();
        expect(screen.getByTestId('encode-library-no-match')).toBeInTheDocument();

        fireEvent.click(screen.getByTestId('encode-library-filter-all'));
        expect(screen.getAllByTestId('encode-library-group')).toHaveLength(2);
    });

    it('searches title, file name and folder path', () => {
        render(<EncodeLibraryView library={library()} />);
        const search = screen.getByTestId('encode-library-search');
        fireEvent.change(search, { target: { value: 'inception' } });
        expect(screen.getAllByTestId('encode-library-group')).toHaveLength(1);
        fireEvent.change(search, { target: { value: 'window of opp' } });
        expect(screen.getAllByTestId('encode-library-row')).toHaveLength(1);
        fireEvent.change(search, { target: { value: '/specials' } });
        expect(screen.getAllByTestId('encode-library-row')).toHaveLength(1);
        fireEvent.change(search, { target: { value: 'tmdbid-4629' } });
        expect(screen.getAllByTestId('encode-library-row')).toHaveLength(3);
    });

    it('renders a real empty state for an empty library', () => {
        render(<EncodeLibraryView library={{ root_tv: TV, root_movies: null, groups: [], totals: {} }} />);
        expect(screen.getByTestId('encode-library-empty')).toHaveTextContent('No encodes recorded yet');
        expect(screen.queryByTestId('encode-library-loading')).not.toBeInTheDocument();
    });

    it('shows the live section from the encode_progress map and marks the row encoding', () => {
        const live: EncodeProgress = {
            file: 'Inception (2010).mkv',
            destination_folder: '/staging/encode-out',
            percent: 37.25,
            eta: 3720,
            speed: '0.31x',
            codec: 'av1',
            codec_class: 'live_action_clean',
            started_at: 1,
        };
        render(<EncodeLibraryView library={library()} encodes={new Map([[live.file, live]])} onReencode={vi.fn()} />);
        const liveRow = screen.getByTestId('encode-library-live-row');
        expect(liveRow).toHaveTextContent('37.3%');
        expect(liveRow).toHaveTextContent('ETA 1h 02m');
        expect(liveRow).toHaveTextContent('0.31x');
        expandAll();
        const row = rowFor('Inception (2010).mkv');
        expect(within(row).getByTestId('encode-library-state')).toHaveAttribute('data-state', 'encoding');
        expect(within(row).getByTestId('encode-library-reencode')).toBeDisabled();
    });

    it('shows a queued row as queued and blocks a second re-encode', () => {
        const lib = library();
        render(<EncodeLibraryView library={lib} queued={new Set([lib.groups[1].entries[0].source])} onReencode={vi.fn()} />);
        expandAll();
        const row = rowFor('Inception (2010).mkv');
        expect(within(row).getByTestId('encode-library-state')).toHaveAttribute('data-state', 'queued');
        expect(within(row).getByTestId('encode-library-reencode')).toBeDisabled();
    });
});

describe('encodeLibrary helpers', () => {
    it('filterLibrary keeps a whole group on a title hit', () => {
        expect(filterLibrary(library().groups, 'stargate', 'all')[0].entries).toHaveLength(3);
        expect(filterLibrary(library().groups, '', 'encoded').map((g) => g.title)).toEqual([
            'Stargate SG-1 (1997) [tmdbid-4629]',
        ]);
    });

    it('bySeason orders specials first and unknown seasons last', () => {
        const groups = bySeason([
            entry({ season: null }),
            entry({ season: 2 }),
            entry({ season: 0 }),
        ]);
        expect(groups.map((g) => g.season)).toEqual([0, 2, null]);
        expect(groups.map((g) => seasonLabel(g.season))).toEqual(['Specials', 'Season 02', 'Other files']);
    });

    it('savedPercent', () => {
        expect(savedPercent(1000, 250)).toBe(75);
        expect(savedPercent(1000, null)).toBeNull();
        expect(savedPercent(null, 10)).toBeNull();
    });

    it('splitForMiddleTruncation keeps the full path across both halves', () => {
        const [head, tail] = splitForMiddleTruncation(`${SHOW}/Season 03`);
        expect(head + tail).toBe(`${SHOW}/Season 03`);
        expect(tail.length).toBeGreaterThan(0);
        expect(splitForMiddleTruncation('/short')).toEqual(['/short', '']);
    });

    it('apiErrorDetail returns the FastAPI detail verbatim', () => {
        expect(apiErrorDetail('{"detail":"Source is HDR; only passthrough may be forced on it"}')).toBe(
            'Source is HDR; only passthrough may be forced on it',
        );
        expect(apiErrorDetail('plain text')).toBe('plain text');
        expect(apiErrorDetail('')).toBe('');
    });

    it('overlayLive returns the same object when nothing is live', () => {
        const lib = library();
        expect(overlayLive(lib, new Map())).toBe(lib);
    });
});
